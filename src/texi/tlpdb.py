"""Parse, fetch, cache and query the TeX Live package database (tlpdb).

The tlpdb is a text file of blank-line-separated entries made of ``key value``
lines. File lists are sections (``runfiles size=N``) followed by indented path
lines. A full copy lives at ``<repository>/tlpkg/texlive.tlpdb.xz``.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import urllib.error
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

DEFAULT_REPOSITORY = "https://mirror.ctan.org/systems/texlive/tlnet"
USER_AGENT = "texi (+https://github.com/claudiosv/texi)"
CACHE_MAX_AGE = 24 * 3600
TIMEOUT = 20.0

_XZ_MAGIC = b"\xfd7zXZ\x00"
_ATTR_RE = re.compile(r'\s+(\w+)=("[^"]*"|\S+)')
_SECTION_KINDS = ("runfiles", "docfiles", "srcfiles", "binfiles")


class TlpdbError(Exception):
    """The database could not be loaded."""


@dataclass
class TlFile:
    """One file listed in a package.

    Parameters
    ----------
    path
        Path as written in the tlpdb (may start with ``RELOC/``).
    attrs
        Trailing ``key=value`` attributes (``details``, ``language``).
    """

    path: str
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def full_path(self) -> str:
        """Path with ``RELOC/`` expanded to ``texmf-dist/``."""
        return expand_reloc(self.path)

    @property
    def basename(self) -> str:
        """File name without directories."""
        return self.path.rsplit("/", 1)[-1]


def expand_reloc(path: str) -> str:
    """Expand a leading ``RELOC/`` to ``texmf-dist/``."""
    if path.startswith("RELOC/"):
        return "texmf-dist/" + path[len("RELOC/") :]
    return path


@dataclass
class TlPackage:
    """A tlpdb entry.

    Parameters
    ----------
    name
        TeX Live package name.
    category
        Package, Collection, Scheme, TLCore, ...
    revision
        TeX Live revision number.
    shortdesc
        One-line description.
    longdesc
        Long description with the repeated lines joined by spaces.
    catalogue_key
        Value of the ``catalogue`` key; empty when absent.
    depends
        ``depend`` values in order.
    fields
        Every other key verbatim (values of repeated keys in order).
    files
        Sections ``runfiles``, ``docfiles``, ``srcfiles`` and
        ``binfiles:<arch>`` mapped to their files.
    sizes
        Declared ``size=`` per section (summed if a section repeats).
    """

    name: str
    category: str = ""
    revision: str = ""
    shortdesc: str = ""
    longdesc: str = ""
    catalogue_key: str = ""
    depends: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, list[TlFile]] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    @property
    def catalogue(self) -> str:
        """CTAN catalogue key; the package name when none is given."""
        return self.catalogue_key or self.name

    def first(self, key: str) -> str | None:
        """First value of an extra key, or None."""
        vals = self.fields.get(key)
        return vals[0] if vals else None

    def all_files(self) -> list[TlFile]:
        """All files of all sections."""
        return [f for fs in self.files.values() for f in fs]

    def to_dict(self) -> dict[str, object]:
        """Machine-readable form with every field."""
        return {
            "name": self.name,
            "category": self.category,
            "revision": self.revision,
            "shortdesc": self.shortdesc,
            "longdesc": self.longdesc,
            "catalogue": self.catalogue,
            "catalogue_explicit": bool(self.catalogue_key),
            "depends": list(self.depends),
            "fields": {k: list(v) for k, v in self.fields.items()},
            "sizes": dict(self.sizes),
            "files": {
                sec: [
                    {"path": f.full_path, **({"attrs": f.attrs} if f.attrs else {})}
                    for f in fs
                ]
                for sec, fs in self.files.items()
            },
        }


@dataclass
class SearchHit:
    """A search result.

    Parameters
    ----------
    package
        The matching package.
    score
        Rank score; higher is better.
    matched_on
        Best matching field: name, catalogue, shortdesc, longdesc or file.
    files
        Matching file paths (only when files were searched).
    """

    package: TlPackage
    score: int
    matched_on: str
    files: list[str] = field(default_factory=list)


@dataclass
class TlpdbStats:
    """Summary figures of a database."""

    entries: int
    categories: dict[str, int]
    files: dict[str, int]
    with_binfiles: int
    architectures: list[str]
    release: str | None
    minrelease: str | None
    frozen: str | None
    revision: str | None


class Tlpdb:
    """A parsed database.

    Parameters
    ----------
    packages
        Entries by name.
    source
        URI or path the database was read from (final URI after redirects).
    fetched
        When the data was fetched (or the file's mtime).
    size_bytes
        Unpacked size.
    compressed_bytes
        Size of the ``.xz`` file, or None for a plain file.
    """

    def __init__(
        self,
        packages: dict[str, TlPackage],
        source: str = "",
        fetched: datetime | None = None,
        size_bytes: int = 0,
        compressed_bytes: int | None = None,
    ) -> None:
        self.packages = packages
        self.source = source
        self.fetched = fetched
        self.size_bytes = size_bytes
        self.compressed_bytes = compressed_bytes
        #: Repository the user asked for (before redirects), if remote.
        self.repository: str | None = None
        #: Path of the cached ``.xz`` when loaded from a remote repository.
        self.cache_path: Path | None = None
        #: True when served from the cache without a successful network check.
        self.from_cache = False
        #: True when the cache was used because the network failed.
        self.offline = False
        self._file_index: dict[str, list[str]] | None = None

    def get(self, name: str) -> TlPackage | None:
        """Package by exact name."""
        return self.packages.get(name)

    def by_catalogue(self, key: str) -> list[TlPackage]:
        """Packages whose CTAN catalogue key is ``key`` (case-sensitive)."""
        return [p for p in self.packages.values() if p.catalogue == key]

    def file_index(self) -> dict[str, list[str]]:
        """File basename mapped to the names of packages containing it (lazy)."""
        if self._file_index is None:
            idx: dict[str, list[str]] = {}
            for p in self.packages.values():
                for f in p.all_files():
                    names = idx.setdefault(f.basename, [])
                    if not names or names[-1] != p.name:
                        names.append(p.name)
            self._file_index = idx
        return self._file_index

    def search(
        self,
        query: str,
        *,
        regex: bool = False,
        in_name: bool = True,
        in_desc: bool = True,
        in_files: bool = False,
        category: str | None = None,
    ) -> list[SearchHit]:
        """Search packages, best match first.

        Ranking: exact name, name prefix, name substring, shortdesc, longdesc,
        file. Without ``regex`` the query is split on whitespace and every
        term must match somewhere (case-insensitive); with ``regex`` the whole
        query is one case-insensitive pattern.

        Parameters
        ----------
        query
            Text or regular expression.
        regex
            Treat the query as a regular expression.
        in_name
            Match names (and catalogue keys).
        in_desc
            Match short and long descriptions.
        in_files
            Match file paths.
        category
            Only packages of this category (case-insensitive).

        Returns
        -------
        list[SearchHit]
            Hits sorted by descending score, then name.

        Raises
        ------
        TlpdbError
            If ``regex`` is set and the pattern is invalid.
        """
        if regex:
            try:
                pats = [re.compile(query, re.IGNORECASE)]
            except re.error as e:
                raise TlpdbError(f"invalid regular expression: {e}") from e
        else:
            pats = [re.compile(re.escape(t), re.IGNORECASE) for t in query.split()]
        if not pats:
            return []
        cat = category.lower() if category else None
        hits: list[SearchHit] = []
        for p in self.packages.values():
            if cat and p.category.lower() != cat:
                continue
            total = 0
            best = ("", 0)
            files: list[str] = []
            for pat in pats:
                score, fld, matched = self._score(
                    p, pat, regex, in_name, in_desc, in_files
                )
                if score == 0:
                    total = 0
                    break
                total += score
                files.extend(m for m in matched if m not in files)
                if score > best[1]:
                    best = (fld, score)
            if total:
                hits.append(SearchHit(p, total, best[0], files))
        hits.sort(key=lambda h: (-h.score, h.package.name))
        return hits

    @staticmethod
    def _score(
        p: TlPackage,
        pat: re.Pattern[str],
        regex: bool,
        in_name: bool,
        in_desc: bool,
        in_files: bool,
    ) -> tuple[int, str, list[str]]:
        """Best (score, field, matched files) of one pattern against a package."""
        score, fld = 0, ""
        matched: list[str] = []
        if in_name:
            for value, label in ((p.name, "name"), (p.catalogue_key, "catalogue")):
                if not value:
                    continue
                m = pat.search(value)
                if not m:
                    continue
                if pat.fullmatch(value):
                    s = 100 if label == "name" else 90
                elif m.start() == 0:
                    s = 80 if label == "name" else 70
                else:
                    s = 60 if label == "name" else 50
                if s > score:
                    score, fld = s, label
        if in_desc and score < 40 and pat.search(p.shortdesc):
            score, fld = 40, "shortdesc"
        if in_desc and score < 20 and pat.search(p.longdesc):
            score, fld = 20, "longdesc"
        if in_files:
            for f in p.all_files():
                full = f.full_path
                if pat.search(full):
                    matched.append(full)
            if matched:
                exact = any(pat.fullmatch(m.rsplit("/", 1)[-1]) for m in matched)
                s = 15 if exact else 10
                if s > score:
                    score, fld = s, "file"
        return score, fld, matched

    def stats(self) -> TlpdbStats:
        """Entry, category and file counts plus release info from ``00texlive.config``."""
        cats = Counter(p.category for p in self.packages.values())
        files: Counter[str] = Counter()
        archs: set[str] = set()
        with_bin = 0
        for p in self.packages.values():
            has_bin = False
            for sec, fs in p.files.items():
                kind, _, arch = sec.partition(":")
                files[kind] += len(fs)
                if kind == "binfiles":
                    has_bin = has_bin or bool(fs)
                    archs.add(arch)
            with_bin += has_bin
        cfg = self.packages.get("00texlive.config")
        info: dict[str, str] = {}
        if cfg:
            for d in cfg.depends:
                k, _, v = d.partition("/")
                info[k] = v
        return TlpdbStats(
            entries=len(self.packages),
            categories=dict(sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))),
            files={k: files.get(k, 0) for k in _SECTION_KINDS},
            with_binfiles=with_bin,
            architectures=sorted(archs),
            release=info.get("release"),
            minrelease=info.get("minrelease"),
            frozen=info.get("frozen"),
            revision=info.get("revision"),
        )


def _parse_file_line(line: str) -> TlFile:
    """Parse `` path key=value ...``."""
    body = line.strip()
    m = _ATTR_RE.search(body)
    if not m:
        return TlFile(body)
    # attributes run to the end of the line; path is everything before the first one
    tail = body[m.start() :]
    attrs = {k: v.strip('"') for k, v in _ATTR_RE.findall(tail)}
    return TlFile(body[: m.start()], attrs)


def _parse_entry(lines: list[str]) -> TlPackage | None:
    """Parse the lines of one blank-line-separated entry."""
    pkg = TlPackage(name="")
    longdesc: list[str] = []
    section: str | None = None
    for line in lines:
        if line[0] in " \t":
            if section is not None and line.strip():
                pkg.files[section].append(_parse_file_line(line))
            continue
        key, _, value = line.partition(" ")
        section = None
        if key in _SECTION_KINDS:
            attrs = {k: v for k, v in _ATTR_RE.findall(" " + value)}
            sec = key
            if key == "binfiles":
                sec = f"binfiles:{attrs.get('arch', '').strip()}"
            pkg.files.setdefault(sec, [])
            size = attrs.get("size")
            if size and size.isdigit():
                pkg.sizes[sec] = pkg.sizes.get(sec, 0) + int(size)
            section = sec
        elif key == "name":
            pkg.name = value
        elif key == "category":
            pkg.category = value
        elif key == "revision":
            pkg.revision = value
        elif key == "shortdesc":
            pkg.shortdesc = value
        elif key == "longdesc":
            longdesc.append(value)
        elif key == "catalogue":
            pkg.catalogue_key = value
        elif key == "depend":
            pkg.depends.append(value)
        else:
            pkg.fields.setdefault(key, []).append(value)
    pkg.longdesc = " ".join(longdesc)
    return pkg if pkg.name else None


def parse_tlpdb(text: str) -> Tlpdb:
    """Parse tlpdb text.

    Parameters
    ----------
    text
        Contents of ``texlive.tlpdb``.

    Returns
    -------
    Tlpdb
        Database with ``size_bytes`` set; ``source`` is empty.
    """
    packages: dict[str, TlPackage] = {}
    block: list[str] = []

    def flush() -> None:
        if block:
            pkg = _parse_entry(block)
            if pkg:
                packages[pkg.name] = pkg
            block.clear()

    for line in text.splitlines():
        if line.strip():
            block.append(line.rstrip())
        else:
            flush()
    flush()
    return Tlpdb(packages, size_bytes=len(text.encode("utf-8")))


def _decode(data: bytes) -> tuple[str, int | None]:
    """Decompress if xz; returns text and the compressed size (None if plain)."""
    if data.startswith(_XZ_MAGIC):
        try:
            raw = lzma.decompress(data)
        except lzma.LZMAError as e:
            raise TlpdbError(f"corrupt xz data: {e}") from e
        return raw.decode("utf-8", errors="replace"), len(data)
    return data.decode("utf-8", errors="replace"), None


def load_local(path: Path | str) -> Tlpdb:
    """Load a local tlpdb file (plain or ``.xz``) or a repository directory.

    Raises
    ------
    TlpdbError
        If the file is missing or unreadable.
    """
    p = Path(path).expanduser()
    if p.is_dir():
        for cand in ("tlpkg/texlive.tlpdb", "tlpkg/texlive.tlpdb.xz", "texlive.tlpdb"):
            if (p / cand).is_file():
                p = p / cand
                break
    if not p.is_file():
        raise TlpdbError(f"tlpdb file not found: {p}")
    try:
        data = p.read_bytes()
    except OSError as e:
        raise TlpdbError(f"cannot read {p}: {e}") from e
    text, comp = _decode(data)
    db = parse_tlpdb(text)
    db.source = str(p.resolve())
    db.fetched = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    db.compressed_bytes = comp
    return db


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/texi/tlpdb`` (default ``~/.cache/texi/tlpdb``)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "texi" / "tlpdb"


def normalize_repository(repo: str) -> str:
    """Map the ``ctan``/``tlnet`` aliases to the mirror URL; strip a trailing slash."""
    repo = repo.strip()
    if repo.lower() in ("ctan", "tlnet"):
        return DEFAULT_REPOSITORY
    return repo.rstrip("/")


def tlmgr_repository() -> str | None:
    """The repository from ``tlmgr option repository`` if it is an http(s) URL."""
    if not shutil.which("tlmgr"):
        return None
    try:
        out = subprocess.run(
            ["tlmgr", "option", "repository"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    # "Default package repository (repository): http://..."; the URL has ':' too
    m = re.search(r"\):\s*(\S+)", out)
    if not m:
        return None
    value = m.group(1)
    if value.lower() in ("ctan", "tlnet"):
        return DEFAULT_REPOSITORY
    if value.startswith(("http://", "https://")):
        return value.rstrip("/")
    return None


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_remote(
    repository: str, *, refresh: bool = False, max_age: float = CACHE_MAX_AGE
) -> Tlpdb:
    """Load ``<repository>/tlpkg/texlive.tlpdb.xz`` through the cache.

    The cache (``<cache_dir>/<sha1(repo)>.xz`` plus a ``.json`` sidecar with
    ETag, Last-Modified, fetch time and final URL) is used without network when
    younger than ``max_age`` seconds (unless ``refresh``), revalidated with a
    conditional GET otherwise, and used as a fallback when the network fails.

    Raises
    ------
    TlpdbError
        If the download fails and no cache exists.
    """
    repo = normalize_repository(repository)
    key = hashlib.sha1(repo.encode()).hexdigest()
    cdir = cache_dir()
    xz = cdir / f"{key}.xz"
    meta_path = cdir / f"{key}.json"
    meta = _read_meta(meta_path) if xz.is_file() else {}
    now = datetime.now(timezone.utc).timestamp()
    fetched_ts = float(meta.get("fetched", xz.stat().st_mtime if xz.is_file() else 0))

    def build(from_cache: bool, offline: bool = False) -> Tlpdb:
        text, comp = _decode(xz.read_bytes())
        db = parse_tlpdb(text)
        db.compressed_bytes = comp
        db.source = str(meta.get("url") or f"{repo}/tlpkg/texlive.tlpdb.xz")
        db.fetched = datetime.fromtimestamp(fetched_ts, tz=timezone.utc)
        db.repository = repo
        db.cache_path = xz
        db.from_cache = from_cache
        db.offline = offline
        return db

    if xz.is_file() and not refresh and now - fetched_ts < max_age:
        return build(from_cache=True)

    url = f"{repo}/tlpkg/texlive.tlpdb.xz"
    headers = {"User-Agent": USER_AGENT}
    if xz.is_file():
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    try:
        with urlopen(Request(url, headers=headers), timeout=TIMEOUT) as resp:
            body = resp.read()
            final = resp.geturl() if hasattr(resp, "geturl") else url
            etag = resp.headers.get("ETag")
            last_mod = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304 and xz.is_file():
            fetched_ts = now
            meta["fetched"] = now
            _write_meta(meta_path, meta)
            return build(from_cache=True)
        if xz.is_file():
            return build(from_cache=True, offline=True)
        raise TlpdbError(f"cannot download {url}: HTTP {e.code} {e.reason}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        if xz.is_file():
            return build(from_cache=True, offline=True)
        reason = getattr(e, "reason", e)
        raise TlpdbError(
            f"cannot download {url}: {reason}. You seem to be offline and there is "
            "no cached copy; use --local PATH or try again with a connection."
        ) from e

    _decode(body)  # validate before replacing the cache
    cdir.mkdir(parents=True, exist_ok=True)
    tmp = xz.with_suffix(".xz.tmp")
    tmp.write_bytes(body)
    tmp.replace(xz)
    fetched_ts = now
    meta = {"url": final, "fetched": now}
    if etag:
        meta["etag"] = etag
    if last_mod:
        meta["last_modified"] = last_mod
    _write_meta(meta_path, meta)
    return build(from_cache=False)


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        pass


def load(
    source: str | Path | None = None,
    *,
    local: Path | str | None = None,
    refresh: bool = False,
) -> Tlpdb:
    """Load a database from a repository URL or a local file.

    Parameters
    ----------
    source
        Repository URL (``http(s)://``, ``file://``, or an alias), or a local
        path when ``local`` is not given and it exists on disk.
    local
        Local tlpdb file (plain or ``.xz``); takes precedence.
    refresh
        Ignore the cache age for remote repositories.
    """
    if local is not None:
        return load_local(local)
    if source is None:
        source = DEFAULT_REPOSITORY
    s = str(source)
    if not re.match(r"^(https?|file)://", s) and s.lower() not in ("ctan", "tlnet"):
        return load_local(s)
    return load_remote(s, refresh=refresh)
