"""Install TeX Live packages from a shared archive store, like uv does for Python.

A package container (``<repository>/archive/<name>.tar.xz``, runfiles only) is
downloaded once into a global store::

    ~/.cache/texi/archive/<ID>/<PACKAGE>/        extracted container
    ~/.cache/texi/archive/<ID>/texi-archive.json metadata and file list

``<ID>`` is ``<name>-<revision>-<first 7 characters of the container checksum>``
(``kastrup-15878-d7cbd22``), so a new revision or a changed container gets its own entry.
Project trees (``.texenv/``) then get copy-on-write clones of the files (never
hard links, so editing a file in a project cannot change the store). Where cloning
is not possible (other volume, plain filesystem) the files are copied.

The package is recorded in ``.texenv/tlpkg/texlive.tlpdb`` and
``.texenv/tlpkg/tlpobj/<name>.tlpobj`` exactly as ``tlmgr`` would, so ``tlmgr``
and texi's other features keep working. Which packages came from the store is
tracked in ``.texenv/tlpkg/texi-archive.json``.

Which packages to install, and in which order, comes from :mod:`texi.resolver`.
``tlmgr`` is never called. Packages with ``execute`` lines (maps, formats,
hyphenation) get their configuration written and their tools run once per batch
by :mod:`texi.tlactions`; binaries of the tree's architecture are cloned into
``bin/<arch>/``. Packages that cannot be installed are reported in
:attr:`Result.failed` or :attr:`Result.skipped`, never handed to another tool.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import textwrap
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx2

from texi import resolver, tlactions, tlpdb

SCHEMA_VERSION = 1
META_NAME = "texi-archive.json"
MANIFEST_NAME = "texi-archive.json"  # in .texenv/tlpkg/
_CHUNK = 1 << 16

#: ``(label, done, total)``; ``total`` is 0 when unknown.
Progress = Callable[[str, int, int], None]
#: Packages (from a list of names) that the system TeX Live tree already has.
SystemInstalled = Callable[[list[str]], set[str]]

#: Fields tlmgr does not keep in the tlpobj of an installed (relocated) package.
_DROPPED_FIELDS = ("relocated",)


class ArchiveError(Exception):
    """A package could not be fetched, verified or installed from the store."""


# --- store -----------------------------------------------------------------


def store_dir() -> Path:
    """``$XDG_CACHE_HOME/texi/archive`` (default ``~/.cache/texi/archive``)."""
    return tlpdb.cache_dir().parent / "archive"


def archive_id(name: str, revision: str, checksum: str) -> str:
    """Directory name of one package build in the store: ``name-revision-checksum7``.

    The container checksum identifies the content, so the same build from two
    repositories shares one entry and a changed container gets its own.

    Parameters
    ----------
    name
        Package name.
    revision
        TeX Live revision.
    checksum
        sha512 of the container, as in the tlpdb.

    Returns
    -------
    str
        For example ``kastrup-15878-d7cbd22``: the first 7 characters of the checksum.
    """
    return f"{name}-{revision}-{checksum[:7]}"


def container_checksum(pkg: tlpdb.TlPackage) -> str | None:
    """The sha512 of the runfiles container, or None."""
    return pkg.first("containerchecksum")


@dataclass
class Entry:
    """A package in the store."""

    id: str
    name: str
    revision: str
    repository: str
    checksum: str
    path: Path
    files: list[str] = field(default_factory=list)

    @property
    def package_dir(self) -> Path:
        """Directory holding the extracted container."""
        return self.path / self.name


def _read_entry(path: Path, name: str) -> Entry | None:
    """The store entry in ``path`` if complete and readable."""
    try:
        meta = json.loads((path / META_NAME).read_text(encoding="utf-8"))
        if meta["schema"] != SCHEMA_VERSION or meta["name"] != name:
            return None
        if not (path / name).is_dir():
            return None
        return Entry(
            path.name,
            name,
            meta["revision"],
            meta["repository"],
            meta["checksum"],
            path,
            list(meta["files"]),
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def lookup(repository: str, pkg: tlpdb.TlPackage) -> Entry | None:
    """The store entry for this exact package build, or None (no network needed)."""
    checksum = container_checksum(pkg)
    if not checksum:
        return None
    ident = archive_id(pkg.name, pkg.revision, checksum)
    return _read_entry(store_dir() / ident, pkg.name)


def _download(
    url: str,
    dest: Path,
    expected: str,
    client: httpx2.Client | None,
    progress: Callable[[int, int], None] | None,
    size_hint: int,
    verify: bool = True,
) -> str:
    """Stream ``url`` to ``dest`` and return its sha512.

    Raises ArchiveError for a failed download, or for a sha512 other than
    ``expected`` when ``verify`` is set. Without ``verify`` the caller compares.
    """
    digest = hashlib.sha512()
    done = 0
    try:
        with dest.open("wb") as out:
            if url.startswith("file://"):
                src = Path(unquote(urlparse(url).path))
                total = src.stat().st_size
                with src.open("rb") as fh:
                    while chunk := fh.read(_CHUNK):
                        digest.update(chunk)
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            else:
                own = client is None
                cl = client or httpx2.Client(
                    follow_redirects=True,
                    timeout=httpx2.Timeout(30.0, read=120.0),
                    headers={"User-Agent": tlpdb.USER_AGENT},
                )
                try:
                    with cl.stream("GET", url) as r:
                        r.raise_for_status()
                        total = int(r.headers.get("content-length") or size_hint)
                        for chunk in r.iter_bytes(_CHUNK):
                            digest.update(chunk)
                            out.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
                finally:
                    if own:
                        cl.close()
    except (httpx2.HTTPError, OSError) as e:
        dest.unlink(missing_ok=True)
        msg = f"download of {url} failed: {e}"
        raise ArchiveError(msg) from e
    if verify and digest.hexdigest() != expected.lower():
        dest.unlink(missing_ok=True)
        msg = f"sha512 mismatch for {url}"
        raise ArchiveError(msg)
    return digest.hexdigest()


#: Seconds to wait before each retry of a failed download (so ``len + 1`` attempts).
#: mirror.ctan.org redirects to a different mirror per request, so a retry often
#: succeeds after a mirror with a broken certificate, a stale file or a timeout.
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)


def pinned_mirror(repository: str, index_source: str | None) -> str:
    """The mirror the package index came from, or ``repository`` if that is unknown.

    ``mirror.ctan.org`` sends each request to another mirror, and mirrors sync at
    different times, so an index from one mirror and a container from another can
    disagree on the sha512. Downloading from the index's own mirror avoids that.

    Parameters
    ----------
    repository
        The configured repository (possibly the ``mirror.ctan.org`` redirector).
    index_source
        Final URL the index was downloaded from, as in :attr:`texi.tlpdb.Tlpdb.source`.
    """
    tail = "/tlpkg/texlive.tlpdb.xz"
    if index_source and index_source.startswith("http") and index_source.endswith(tail):
        return index_source[: -len(tail)]
    return repository


def fetch(
    repository: str,
    pkg: tlpdb.TlPackage,
    *,
    client: httpx2.Client | None = None,
    progress: Progress | None = None,
    notice: Callable[[str], None] | None = None,
    verify: bool = True,
    mirror: str | None = None,
) -> tuple[Entry, bool]:
    """Make sure ``pkg`` is in the store, downloading it if needed.

    Parameters
    ----------
    repository
        Repository URL (container at ``<repository>/archive/<name>.tar.xz``).
    pkg
        Package from the tlpdb index (revision and container checksum are used).
    client
        httpx2 client to use; default is a fresh one.
    progress
        Called with ``("Downloading <name>", bytes_done, bytes_total)``.
    notice
        Called with a message before each retry. A failed download is retried
        after :data:`RETRY_DELAYS`; when the last attempt fails too, the error is raised.
    mirror
        Concrete mirror of the index (see :func:`pinned_mirror`). The first
        attempt uses it; later attempts go through ``repository`` so another
        mirror may answer.
    verify
        Refuse a download whose sha512 differs from the index. Without it a
        mismatch is only reported to ``notice`` (usually a stale mirror) and the
        package is kept in the store under the sha512 it really has, so the
        wrong file never takes the place of the indexed build.

    Returns
    -------
    tuple[Entry, bool]
        The entry and whether it was downloaded now (False: already stored).

    Raises
    ------
    ArchiveError
        No checksum in the index, download failure, checksum mismatch or a bad
        archive. Nothing is left in the store in that case.
    """
    checksum = container_checksum(pkg)
    if not checksum:
        msg = "no container checksum in the tlpdb"
        raise ArchiveError(msg)
    ident = archive_id(pkg.name, pkg.revision, checksum)
    store = store_dir()
    final = store / ident
    if (hit := _read_entry(final, pkg.name)) is not None:
        return hit, False
    store.mkdir(parents=True, exist_ok=True)
    if final.exists():  # incomplete or foreign: replace it
        shutil.rmtree(final, ignore_errors=True)

    label = f"Downloading {pkg.name}"
    part = Path(tempfile.mkstemp(prefix=".dl-", suffix=".partial", dir=store)[1])
    staging = Path(tempfile.mkdtemp(prefix=".tmp-", dir=store))
    try:
        size = int(pkg.first("containersize") or 0)

        def url_for(attempt: int) -> str:
            base = mirror if mirror and attempt == 0 else repository
            return f"{base.rstrip('/')}/archive/{pkg.name}.tar.xz"

        actual = checksum
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                actual = _download(
                    url_for(attempt),
                    part,
                    checksum,
                    client,
                    (lambda d, t: progress(label, d, t)) if progress else None,
                    size,
                    verify,
                )
                break
            except ArchiveError as e:
                if attempt == len(RETRY_DELAYS):
                    hint = (
                        "; the mirrors look out of date. Pin one with "
                        'repository = "https://<mirror>/CTAN/systems/texlive/tlnet" '
                        "in texproject.toml, or set verify_checksums = false"
                        if "sha512 mismatch" in str(e)
                        else ""
                    )
                    msg = f"{e} (gave up after {attempt + 1} attempts{hint})"
                    raise ArchiveError(msg) from e
                delay = RETRY_DELAYS[attempt]
                if notice:
                    notice(
                        f"{pkg.name}: {e}; retrying in {delay:g}s "
                        f"(attempt {attempt + 2} of {len(RETRY_DELAYS) + 1})"
                    )
                time.sleep(delay)
        if actual.lower() != checksum.lower():
            if notice:
                notice(
                    f"{pkg.name}: package hash mismatched (expected {checksum[:7]}, got "
                    f"{actual[:7]}), likely a stale mirror; installing it anyway"
                )
            checksum = actual  # what the store entry and its metadata describe
            ident = archive_id(pkg.name, pkg.revision, actual)
            final = store / ident
            if (hit := _read_entry(final, pkg.name)) is not None:
                return hit, False
            shutil.rmtree(final, ignore_errors=True)
        target = staging / pkg.name
        target.mkdir()
        try:
            with tarfile.open(part) as tf:
                tf.extractall(target, filter="data")
        except (tarfile.TarError, OSError, EOFError) as e:
            msg = f"cannot extract {pkg.name}: {e}"
            raise ArchiveError(msg) from e
        files = sorted(
            p.relative_to(target).as_posix()
            for p in target.rglob("*")
            if not p.is_dir() or p.is_symlink()
        )
        (staging / META_NAME).write_text(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "name": pkg.name,
                    "revision": pkg.revision,
                    "repository": repository,
                    "checksum": checksum,
                    "files": files,
                    "created": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            Path(staging).replace(final)  # atomic; fails if someone else was first
        except OSError:
            if (hit := _read_entry(final, pkg.name)) is not None:
                return hit, False
            raise
        return Entry(
            ident, pkg.name, pkg.revision, repository, checksum, final, files
        ), True
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


# --- cloning ---------------------------------------------------------------

_FICLONE = 0x40049409
_UNSUPPORTED = {errno.EXDEV, errno.ENOTSUP, errno.ENOSYS, errno.EINVAL, errno.EPERM}
_native_disabled = False


def _clone_native(src: str, dst: str) -> None:
    """Copy-on-write clone of one file; raises OSError if the platform/fs cannot."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e), dst)
    elif sys.platform.startswith("linux"):
        import fcntl

        with Path(src).open("rb") as s, Path(dst).open("wb") as d:
            fcntl.ioctl(d.fileno(), _FICLONE, s.fileno())
        shutil.copymode(src, dst)
    else:
        raise OSError(errno.ENOTSUP, "no clone support", dst)


def clone_file(
    src: Path, dst: Path, relink: Callable[[str], str] | None = None
) -> bool:
    """Put a copy of ``src`` at ``dst``, replacing it; True if it is a CoW clone.

    Symlinks are recreated as symlinks (their target passed through ``relink``).
    File modes, including the exec bit, are kept. If cloning fails (other volume, a
    filesystem without reflinks) the file is copied. Never hard links.
    """
    global _native_disabled
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    if src.is_symlink():
        target = str(Path(src).readlink())
        Path(dst).symlink_to(relink(target) if relink else target)
        return False
    if not _native_disabled:
        try:
            _clone_native(str(src), str(dst))
            return True
        except OSError as e:
            dst.unlink(missing_ok=True)
            if e.errno in _UNSUPPORTED:
                _native_disabled = True  # don't retry for every file
    shutil.copy2(src, dst)
    return False


# --- mapping and bookkeeping -----------------------------------------------


def member_path(path: str) -> str:
    """Path of a tlpdb file inside its container (``RELOC/`` is the container root)."""
    return path.removeprefix("RELOC/")


def target_path(path: str, standalone: bool) -> str:
    """Path of a tlpdb file relative to ``.texenv/``.

    A usertree keeps ``tex/latex/x/x.sty``; a standalone tree is a full TeX Live
    tree where relocatable files live under ``texmf-dist/``.
    """
    if path.startswith("RELOC/"):
        rest = path[len("RELOC/") :]
        return rest if not standalone else f"texmf-dist/{rest}"
    return path


def _checked(env_dir: Path, rel: str) -> Path:
    """``env_dir / rel`` if it stays inside ``env_dir``."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not rel:
        msg = f"unsafe path in package: {rel!r}"
        raise ArchiveError(msg)
    return env_dir / p


def _attr(value: str) -> str:
    return f'"{value}"' if any(c.isspace() for c in value) else value


def render_stanza(
    pkg: tlpdb.TlPackage, standalone: bool, arch: str | None = None
) -> str:
    """The tlpdb/tlpobj text of an installed package, as tlmgr writes it.

    ``execute`` and ``postaction`` lines are kept. Runfiles come first, then the
    ``binfiles`` section of ``arch`` (other architectures are not installed).
    """
    out = [
        f"name {pkg.name}",
        f"category {pkg.category}",
        f"revision {pkg.revision}",
    ]
    if pkg.shortdesc:
        out.append(f"shortdesc {pkg.shortdesc}")
    out += [f"longdesc {line}" for line in textwrap.wrap(pkg.longdesc, 70)]
    out += [f"depend {d}" for d in pkg.depends]
    tail: list[str] = []
    for key, values in pkg.fields.items():
        if key in _DROPPED_FIELDS:
            continue
        lines = [f"{key} {v}" for v in values]
        (tail if key.startswith("catalogue") else out).extend(lines)
    files = pkg.files.get("runfiles", [])
    size = pkg.sizes.get("runfiles")
    if files or pkg.category not in resolver.META_CATEGORIES:
        out.append(f"runfiles size={size}" if size is not None else "runfiles")
    for f in files:
        extra = "".join(f" {k}={_attr(v)}" for k, v in f.attrs.items())
        out.append(f" {target_path(f.path, standalone)}{extra}")
    for sec in sorted(pkg.files):
        kind, _, sec_arch = sec.partition(":")
        if kind != "binfiles" or sec_arch != arch or not pkg.files[sec]:
            continue
        size = pkg.sizes.get(sec)
        out.append(
            f"binfiles arch={sec_arch}" + (f" size={size}" if size is not None else "")
        )
        out += [f" {f.path}" for f in pkg.files[sec]]
    if pkg.catalogue_key:
        out.append(f"catalogue {pkg.catalogue_key}")
    out += tail
    return "\n".join(out) + "\n"


def skip_reason(pkg: tlpdb.TlPackage, *, standalone: bool) -> str | None:
    """Why ``pkg`` cannot be installed into this kind of tree, or None.

    ``tlmgr --usermode`` refuses packages that are not relocatable; so does texi.
    """
    runfiles = pkg.files.get("runfiles", [])
    if not standalone and any(not f.path.startswith("RELOC/") for f in runfiles):
        return "not relocatable (tlmgr refuses these in a usertree too)"
    return None


def _manifest_path(env_dir: Path) -> Path:
    return env_dir / "tlpkg" / MANIFEST_NAME


def managed(env_dir: Path) -> dict[str, dict[str, str]]:
    """Packages installed from the store into ``env_dir``: name -> ``{id, revision}``."""
    try:
        data = json.loads(_manifest_path(env_dir).read_text(encoding="utf-8"))
        return dict(data["packages"])
    except OSError, ValueError, KeyError, TypeError:
        return {}


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stanzas(text: str) -> list[tuple[str, str]]:
    """``(name, text)`` of each blank-line-separated tlpdb entry."""
    out: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if block.startswith("name "):
            out.append((block.split("\n", 1)[0][5:].strip(), block))
    return out


def installed_revision(env_dir: Path, name: str) -> int | None:
    """Revision of ``name`` per its tlpobj in ``env_dir``, or None if not installed."""
    obj = env_dir / "tlpkg" / "tlpobj" / f"{name}.tlpobj"
    try:
        for line in obj.read_text(encoding="utf-8").splitlines():
            if line.startswith("revision "):
                return int(line[9:])
    except OSError, ValueError:
        pass
    return None


class Tree:
    """A ``.texenv/`` that packages are cloned into.

    Changes to ``tlpkg/texlive.tlpdb`` and the manifest are collected and
    written once by :meth:`flush`.
    """

    def __init__(
        self, env_dir: Path, *, standalone: bool, arch: str | None = None
    ) -> None:
        self.env_dir = env_dir
        self.standalone = standalone
        self.arch = arch
        #: Execute and postaction lines of what was installed or removed.
        self.batch = tlactions.Batch()
        self._add: dict[str, str] = {}
        self._drop: set[str] = set()
        self._manifest = managed(env_dir)

    def install(self, pkg: tlpdb.TlPackage, entry: Entry) -> int:
        """Clone ``pkg`` from ``entry`` into the tree, replacing a previous install.

        Returns
        -------
        int
            Number of files placed.

        Raises
        ------
        ArchiveError
            A file the tlpdb lists is not in the container, or has an unsafe path.
        """
        pairs: list[tuple[Path, Path]] = []
        listed = [
            (f.path, target_path(f.path, self.standalone))
            for f in pkg.files.get("runfiles", [])
        ]
        for sec, files in pkg.files.items():
            kind, _, sec_arch = sec.partition(":")
            if kind == "binfiles" and files:
                if self.arch is None:
                    msg = f"{pkg.name} ships binaries but the architecture is unknown"
                    raise ArchiveError(msg)
                if sec_arch == self.arch:
                    listed += [(f.path, f.path) for f in files]
        for member, rel in listed:
            src = _checked(entry.package_dir, member_path(member))
            dst = _checked(self.env_dir, rel)
            if not (src.is_symlink() or src.exists()):
                msg = f"{member} is missing from the {pkg.name} archive"
                raise ArchiveError(msg)
            pairs.append((src, dst))
        self.uninstall(pkg.name)
        for src, dst in pairs:
            clone_file(src, dst, relink=self._relink)
        self.batch.note(
            pkg.name,
            "install",
            pkg.fields.get("execute", []),
            pkg.fields.get("postaction", []),
        )
        stanza = render_stanza(pkg, self.standalone, self.arch)
        obj = self.env_dir / "tlpkg" / "tlpobj" / f"{pkg.name}.tlpobj"
        _write_atomic(obj, stanza)
        self._add[pkg.name] = stanza
        self._drop.discard(pkg.name)
        self._manifest[pkg.name] = {"id": entry.id, "revision": pkg.revision}
        return len(pairs)

    def record(self, pkg: tlpdb.TlPackage) -> None:
        """Record a collection or scheme as installed, without files.

        Its members are installed separately; the record lets ``tlmgr`` and
        :func:`managed` see the collection as installed.
        """
        self.uninstall(pkg.name)
        stanza = render_stanza(pkg, self.standalone, self.arch)
        obj = self.env_dir / "tlpkg" / "tlpobj" / f"{pkg.name}.tlpobj"
        _write_atomic(obj, stanza)
        self._add[pkg.name] = stanza
        self._drop.discard(pkg.name)
        self._manifest[pkg.name] = {"id": "", "revision": pkg.revision}

    def _relink(self, target: str) -> str:
        """Symlink target for a tree: ``texmf-dist`` is the tree root in a usertree.

        Binary packages link ``bin/<arch>/x`` to ``../../texmf-dist/scripts/...``;
        a usertree has those scripts at ``scripts/...``.
        """
        prefix = "../../texmf-dist/"
        if not self.standalone and target.startswith(prefix):
            return "../../" + target[len(prefix) :]
        return target

    def uninstall(self, name: str) -> list[Path]:
        """Delete exactly the files recorded for ``name`` (and its record).

        Returns
        -------
        list[Path]
            The files that were removed.
        """
        obj = self.env_dir / "tlpkg" / "tlpobj" / f"{name}.tlpobj"
        removed: list[Path] = []
        recorded: list[str] = []
        executes: list[str] = []
        postactions: list[str] = []
        if obj.exists():
            in_files = False
            for line in obj.read_text(encoding="utf-8").splitlines():
                if line[:1] in " \t":
                    if in_files and line.strip():
                        recorded.append(_split_file_line(line))
                else:
                    in_files = line.startswith(("runfiles", "binfiles"))
                    if line.startswith("execute "):
                        executes.append(line[8:].strip())
                    elif line.startswith("postaction "):
                        postactions.append(line[11:].strip())
            self.batch.note(name, "remove", executes, postactions)
        dirs: set[Path] = set()
        for rel in recorded:
            try:
                path = _checked(self.env_dir, tlpdb.expand_reloc(rel))
            except ArchiveError:
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed.append(path)
                dirs.add(path.parent)
        for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            self._prune(d)
        obj.unlink(missing_ok=True)
        self._add.pop(name, None)
        self._manifest.pop(name, None)
        self._drop.add(name)
        return removed

    def _prune(self, d: Path) -> None:
        """Remove ``d`` and its parents while empty, never ``tlpkg`` or the root."""
        keep = {self.env_dir, self.env_dir / "tlpkg"}
        while d not in keep and d != d.parent and self.env_dir in d.parents:
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent

    def flush(self) -> None:
        """Write the tlpdb and manifest changes."""
        db = self.env_dir / "tlpkg" / "texlive.tlpdb"
        if self._add or self._drop:
            old = db.read_text(encoding="utf-8") if db.exists() else ""
            gone = self._drop | set(self._add)
            keep = [t for n, t in _stanzas(old) if n not in gone]
            keep += self._add.values()
            _write_atomic(db, "\n\n".join(s.strip("\n") for s in keep) + "\n\n")
            self._add.clear()
            self._drop.clear()
        _write_atomic(
            _manifest_path(self.env_dir),
            json.dumps({"schema": SCHEMA_VERSION, "packages": self._manifest}, indent=2)
            + "\n",
        )


def _split_file_line(line: str) -> str:
    """Path of a `` path key=value`` file line."""
    return tlpdb._parse_file_line(line).path


def uninstall(
    env_dir: Path,
    name: str,
    *,
    standalone: bool = False,
    arch: str | None = None,
    notice: Callable[[str], None] | None = None,
) -> list[Path]:
    """Remove a package installed by :func:`install_packages` (or tlmgr's record of it).

    Deletes exactly the files in its tlpobj, empty directories left behind, its
    tlpobj, its tlpdb stanza and its manifest entry.

    Returns
    -------
    list[Path]
        The files that were removed.
    """
    tree = Tree(env_dir, standalone=standalone, arch=arch)
    removed = tree.uninstall(name)
    tree.flush()
    tlactions.run(
        env_dir, standalone=standalone, batch=tree.batch, arch=arch, notice=notice
    )
    return removed


# --- planning and orchestration ---------------------------------------------


@dataclass
class Result:
    """Outcome of :func:`install_packages`."""

    #: Packages cloned into the tree (new or updated), in install order.
    installed: list[str] = field(default_factory=list)
    #: Of those, packages that had to be downloaded now.
    downloaded: list[str] = field(default_factory=list)
    #: Packages that were reinstalled because the index has a newer revision.
    updated: list[str] = field(default_factory=list)
    #: Packages already in the tree and current.
    present: list[str] = field(default_factory=list)
    #: Always empty: texi never hands a package to tlmgr. Kept for callers.
    fallback: dict[str, str] = field(default_factory=dict)
    #: Packages that could not be fetched, verified or cloned (a yellow notice has the reason).
    failed: list[str] = field(default_factory=list)
    #: ``name -> reason`` for packages left out on purpose (foreign architecture, not relocatable).
    skipped: dict[str, str] = field(default_factory=dict)
    #: What the post-install actions did (maps, formats, hyphenation, scripts); None if none ran.
    actions: tlactions.Outcome | None = None
    #: Requested collections and schemes recorded in the tree (their members are in ``installed``).
    recorded: list[str] = field(default_factory=list)
    #: Dependencies the index does not know, with the packages that need them.
    missing: dict[str, set[str]] = field(default_factory=dict)
    #: The dependency resolution the install followed.
    resolution: resolver.Resolution = field(default_factory=resolver.Resolution)

    @property
    def cached(self) -> int:
        """Installed packages that came from the store without a download."""
        return len(self.installed) - len(self.downloaded)


@dataclass
class Plan:
    """What :func:`install_packages` will do, from :func:`plan`."""

    #: The dependency resolution.
    resolution: resolver.Resolution
    #: Names to install from the store, in dependency order. A collection or
    #: scheme among them is only recorded.
    install: list[str] = field(default_factory=list)
    #: Always empty (kept for callers): nothing is left to tlmgr.
    fallback: dict[str, str] = field(default_factory=dict)
    #: ``name -> reason`` for packages that are not installed on purpose.
    skipped: dict[str, str] = field(default_factory=dict)
    #: The subset of ``install`` that replaces an older revision.
    updates: list[str] = field(default_factory=list)
    #: Names already in the tree and current.
    present: list[str] = field(default_factory=list)

    @property
    def missing(self) -> dict[str, set[str]]:
        """Names the index does not know (requested ones included) with their requirers."""
        return dict(self.resolution.missing)


def plan(
    db: tlpdb.Tlpdb,
    roots: Iterable[str],
    env_dir: Path,
    *,
    standalone: bool,
    system_installed: SystemInstalled | None = None,
    update: bool = True,
    arch: str | None = None,
    strict: bool = False,
    infrastructure: bool = False,
) -> Plan:
    """Resolve ``roots`` and their dependencies with :func:`texi.resolver.resolve_packages`.

    Infrastructure and packages the system tree already has are not installed.
    Requested collections and schemes are expanded and recorded. Packages that
    are in the tree are kept unless ``update`` is set, they came from the store
    and the index has a newer revision. The dependencies of packages left to
    tlmgr, and of those already in the tree, are resolved too.
    Packages that cannot go into this tree (a usertree needs relocatable
    packages, as ``tlmgr --usermode`` does) or foreign-architecture roots are
    listed in :attr:`Plan.skipped`; :attr:`Plan.fallback` stays empty.

    Parameters
    ----------
    db
        Package index of the repository.
    roots
        Requested package names.
    env_dir
        The ``.texenv/`` directory.
    standalone
        True for a full tree, False for a usertree.
    system_installed
        Reports which of some names the system tree has.
    update
        Plan a reinstall of store packages whose index revision is newer.
    arch
        Target architecture for ``name.ARCH`` dependencies.
    strict
        Raise :class:`texi.resolver.UnknownPackageError` for an unknown dependency.
    infrastructure
        Install infrastructure packages too (standalone clone trees).

    Returns
    -------
    Plan
        Names to install in order, those skipped with the reason, updates and
        packages already current.
    """
    res = resolver.resolve_packages(
        db,
        roots,
        arch=arch,
        architectures=db.available_architectures(),
        system_installed=system_installed,
        strict=strict,
        infrastructure=infrastructure,
    )
    manifest = managed(env_dir)
    out = Plan(res)
    for root in res.roots:
        if (
            root not in res.missing
            and (why := res.skipped.get(root))
            and why
            not in {
                resolver.REASON_SYSTEM,
                resolver.REASON_METADATA,
            }
        ):
            out.skipped[root] = why
    for n in res.order:
        pkg = db.get(n)
        assert pkg is not None
        rev = installed_revision(env_dir, n)
        if rev is not None:
            if update and n in manifest and _newer(pkg.revision, rev):
                out.updates.append(n)
                out.install.append(n)
            else:
                out.present.append(n)
            continue
        if n in res.meta:
            out.install.append(n)
        elif reason := skip_reason(pkg, standalone=standalone):
            out.skipped[n] = reason
        else:
            out.install.append(n)
    return out


def _newer(index_rev: str, installed: int) -> bool:
    try:
        return int(index_rev) > installed
    except ValueError:
        return False


def install_packages(
    db: tlpdb.Tlpdb,
    repository: str,
    roots: Iterable[str],
    env_dir: Path,
    *,
    standalone: bool = False,
    system_installed: SystemInstalled | None = None,
    update: bool = True,
    arch: str | None = None,
    infrastructure: bool = False,
    verify: bool = True,
    client: httpx2.Client | None = None,
    progress: Progress | None = None,
    log: Callable[[str], None] | None = None,
    notice: Callable[[str], None] | None = None,
) -> Result:
    """Install ``roots`` and their dependencies from the store into ``env_dir``.

    ``tlmgr`` is never called. A package whose download, checksum or cloning
    fails is listed in :attr:`Result.failed` (``notice`` gets the reason); the
    other packages are still installed. A failed update keeps the old revision.
    Packages that cannot go into this tree are in :attr:`Result.skipped` with
    the reason, and names the index does not know in :attr:`Result.missing`.
    Packages are installed in dependency order. A requested collection or
    scheme is expanded: its members are installed and the collection itself is
    recorded without files.

    After the tlpdb is written, :func:`texi.tlactions.run` does the
    ``execute``/``postaction`` work once for the whole batch (font maps, format
    and hyphenation files and the builds that follow); its warnings go through
    ``notice`` and the outcome is in :attr:`Result.actions`. Binaries of
    ``arch`` are cloned into ``bin/<arch>/``.

    The caller runs ``mktexlsr`` once afterwards when :attr:`Result.installed`
    is not empty.

    Parameters
    ----------
    db
        Package index for ``repository``.
    repository
        Repository URL containers are downloaded from.
    roots
        Packages to install.
    env_dir
        The ``.texenv/`` directory (its ``tlpkg/`` must exist or is created).
    standalone
        True for a full tree (``copy``/``install``), False for a usertree.
    system_installed
        Reports which of some names the system tree has; those are skipped.
    update
        Reinstall store packages whose index revision is newer.
    arch
        Target architecture for ``name.ARCH`` dependencies; None skips them.
    infrastructure
        Also install infrastructure packages (``texlive.infra``, ...).
    verify
        Refuse packages whose sha512 differs from the index (see :func:`fetch`).
    client
        httpx2 client for downloads.
    progress
        ``(label, done, total)`` callback.
    log
        Debug line callback (install order, why each dependency is there).
    notice
        Warning callback for downloads that failed and unknown dependencies.
    """
    say = log or (lambda _m: None)
    warn = notice or (lambda _m: None)
    mirror = pinned_mirror(repository, getattr(db, "source", None))
    if mirror != repository:
        say(f"downloading from {mirror} (the mirror of the package index)")
    pl = plan(
        db,
        roots,
        env_dir,
        standalone=standalone,
        system_installed=system_installed,
        update=update,
        arch=arch,
        infrastructure=infrastructure,
    )
    res = pl.resolution
    updates = pl.updates
    for dep, requirers in sorted(pl.missing.items()):
        if dep in res.roots:
            continue  # the caller reports requested names
        warn(
            f"{dep} is not in the package index (needed by {', '.join(sorted(requirers))})"
        )
    if res.order:
        say("install order: " + ", ".join(res.order))
    for name in res.order:
        if name not in res.roots and (path := res.why(name)):
            say(f"{name} <- {' <- '.join(reversed(path[:-1]))}")
    for cycle in res.cycles:
        say("dependency cycle: " + ", ".join(cycle))
    result = Result(
        present=pl.present,
        skipped=dict(pl.skipped),
        missing=pl.missing,
        resolution=res,
    )
    for name, why in sorted(pl.skipped.items()):
        warn(f"{name} not installed: {why}")
    tree = Tree(env_dir, standalone=standalone, arch=arch)
    order = pl.install
    total = len(order)
    for i, name in enumerate(order, 1):
        pkg = db.get(name)
        assert pkg is not None
        if progress:
            progress(f"Installing {name}", i - 1, 0)  # 0: no "done" if it fails
        if name in res.meta:
            tree.record(pkg)
            result.recorded.append(name)
            if name in updates:
                result.updated.append(name)
            say(f"{name} r{pkg.revision} recorded (members installed separately)")
            continue
        try:
            entry = lookup(repository, pkg)
            fresh = False
            if entry is None:
                entry, fresh = fetch(
                    repository,
                    pkg,
                    client=client,
                    progress=progress,
                    notice=warn,
                    verify=verify,
                    mirror=mirror,
                )
            tree.install(pkg, entry)
        except (ArchiveError, OSError) as e:
            if name in updates:
                warn(
                    f"Could not update {name}, keeping r{installed_revision(env_dir, name)}: {e}"
                )
            else:
                result.failed.append(name)
                warn(f"{name}: install failed: {e}")
            continue
        result.installed.append(name)
        if fresh:
            result.downloaded.append(name)
        if name in updates:
            result.updated.append(name)
        say(
            f"{name} r{pkg.revision} {'downloaded' if fresh else 'from cache'} "
            f"-> {entry.path}"
        )
    tree.flush()
    if progress and not result.failed:
        progress("Installing", total, total)
    if result.failed and standalone:
        # the tools may be missing; building formats now would only add noise
        warn(
            "Font maps and formats not built: "
            f"{', '.join(result.failed)} did not install. Run `texi sync` again."
        )
    elif not tree.batch.empty or tree.batch.changed:
        result.actions = tlactions.run(
            env_dir, standalone=standalone, batch=tree.batch, arch=arch, notice=notice
        )
    return result


def mktexlsr_target(env_dir: Path, *, standalone: bool) -> Path:
    """Directory to give ``mktexlsr`` after installing into ``env_dir``."""
    return env_dir / "texmf-dist" if standalone else env_dir


def mktexlsr_targets(env_dir: Path, *, standalone: bool) -> list[Path]:
    """Directories to give ``mktexlsr``: every ``!!`` tree of the search path."""
    if not standalone:
        return [env_dir]
    return [env_dir / d for d in ("texmf-dist", "texmf-var", "texmf-local")]


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


def prune(
    keep: set[str], *, dry_run: bool = False, partial_age: float = 3600.0
) -> list[tuple[str, int]]:
    """Delete store entries whose id is not in ``keep``, and incomplete downloads.

    Removes old revisions of a kept package as well as packages nobody in
    ``keep`` uses. Incomplete downloads (``.dl-*.partial`` files, ``.tmp-*``
    staging directories, directories without ``texi-archive.json``) are
    removed once untouched for ``partial_age`` seconds, so a download running
    in another texi process is left alone. Other files are never touched.

    Parameters
    ----------
    keep
        Store ids to keep, normally ``managed(env_dir)`` ids.
    dry_run
        Only report.
    partial_age
        Minimum age in seconds of an incomplete download before it is removed.

    Returns
    -------
    list[tuple[str, int]]
        ``(name, bytes)`` of each removed (or, with ``dry_run``, removable) item, sorted by name.
    """
    root = store_dir()
    removed: list[tuple[str, int]] = []
    if not root.is_dir():
        return removed
    now = time.time()
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / META_NAME).is_file():
            if path.name in keep:
                continue
        elif path.name.startswith((".dl-", ".tmp-")) or (
            path.is_dir() and not path.name.startswith(".")
        ):
            try:
                if now - path.stat().st_mtime < partial_age:
                    continue
            except OSError:
                continue
        else:
            continue
        size = _dir_size(path) if path.is_dir() else path.lstat().st_size
        removed.append((path.name, size))
        if dry_run:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    return removed
