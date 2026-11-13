"""Install ``tex-fmt`` (CTAN package ``latex-formatter``) into ``.texenv/bin/<arch>/``.

``latex-formatter`` is not in TeX Live, so it has no tlnet container and no
published checksum. CTAN serves prebuilt binaries for every platform at
``support/latex-formatter/bin/tex-fmt-<target>.tar.gz``; only the one for this
machine is downloaded. The binary is kept once per version in the archive store
(``ctan-latex-formatter-<version>-<target>/``) and cloned into each tree, like a package.
The CTAN JSON record (about 1 KB) tells the latest version; nothing else is
downloaded while the tree has that version.
The sha256 of what was downloaded is recorded in the store and checked on reuse,
so a changed or damaged store entry is noticed, but the first download is trusted
(HTTPS from CTAN only).
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx2

from texi import archive, tlpdb

PACKAGE = "latex-formatter"
BIN_URL = f"https://mirrors.ctan.org/support/{PACKAGE}/bin/{{name}}-{{asset}}.tar.gz"
INFO_URL = f"https://ctan.org/json/2.0/pkg/{PACKAGE}"
BINARY = "tex-fmt"

#: TeX Live architecture -> asset name in the zip, for machines that are not ambiguous.
_ASSETS = {
    "x86_64-linux": "x86_64-linux",
    "x86_64-linuxmusl": "x86_64-alpine",
    "aarch64-linux": "aarch64-linux",
    "armhf-linux": "armv7hf-linux",
}


def asset_name(arch: str, machine: str | None = None) -> str | None:
    """Name of the CTAN binary for a TeX Live architecture, or None (no binary, e.g. Windows)."""
    if arch == "universal-darwin":
        m = (machine or platform.machine()).lower()
        return "aarch64-macos" if m in {"arm64", "aarch64"} else "x86_64-macos"
    return _ASSETS.get(arch)


class TexFmtError(Exception):
    """tex-fmt could not be downloaded or unpacked."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(client: httpx2.Client) -> str:
    try:
        r = client.get(INFO_URL)
        r.raise_for_status()
        return str(r.json()["version"]["number"])
    except (httpx2.HTTPError, ValueError, KeyError, TypeError) as e:
        msg = f"cannot read the {PACKAGE} version from CTAN: {e}"
        raise TexFmtError(msg) from e


def _stored(entry: Path, asset: str) -> Path | None:
    """The binary of a store entry if it is complete and unchanged."""
    try:
        meta = json.loads((entry / "texi-ctan.json").read_text(encoding="utf-8"))
        binary = entry / BINARY
        if meta["asset"] == asset and _sha256(binary.read_bytes()) == meta["sha256"]:
            return binary
    except OSError, ValueError, KeyError:
        pass
    return None


def _download(client: httpx2.Client, asset: str, entry: Path, version: str) -> Path:
    try:
        url = BIN_URL.format(name=BINARY, asset=asset)
        r = client.get(url)
        r.raise_for_status()
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            member = next(
                (
                    m
                    for m in tf.getmembers()
                    if m.isfile() and Path(m.name).name == BINARY
                ),
                None,
            )
            if member is None or (fh := tf.extractfile(member)) is None:
                msg = f"no {BINARY} in {BINARY}-{asset}.tar.gz"
                raise TexFmtError(msg)
            data = fh.read()
    except (httpx2.HTTPError, tarfile.TarError, OSError) as e:
        msg = f"cannot download {PACKAGE}: {e}"
        raise TexFmtError(msg) from e
    entry.mkdir(parents=True, exist_ok=True)
    binary = entry / BINARY
    tmp = entry / f".{BINARY}.part"
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    tmp.replace(binary)
    (entry / "texi-ctan.json").write_text(
        json.dumps(
            {
                "package": PACKAGE,
                "version": version,
                "asset": asset,
                "sha256": _sha256(data),
                "url": url,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return binary


def installed(env_dir: Path, arch: str) -> Path | None:
    """``.texenv/bin/<arch>/tex-fmt`` if it exists."""
    path = env_dir / "bin" / arch / BINARY
    return path if path.is_file() else None


def _record(env_dir: Path) -> Path:
    return env_dir / "tlpkg" / "texi-tex-fmt.json"


def installed_version(env_dir: Path, arch: str | None = None) -> str | None:
    """The version of the tree's tex-fmt.

    From ``tlpkg/texi-tex-fmt.json``; for a tree without that record (installed
    by an older texi) from ``tex-fmt --version``.
    """
    try:
        return str(json.loads(_record(env_dir).read_text(encoding="utf-8"))["version"])
    except OSError, ValueError, KeyError:
        pass
    if arch and (path := installed(env_dir, arch)):
        try:
            out = subprocess.run(
                [str(path), "--version"], capture_output=True, text=True, timeout=10
            ).stdout
        except OSError, subprocess.SubprocessError:
            return None
        if m := re.search(r"\d+(?:\.\d+)+", out):
            return m.group(0)
    return None


def outdated(
    env_dir: Path,
    arch: str,
    *,
    client: httpx2.Client | None = None,
) -> tuple[str, str] | None:
    """``(installed, latest)`` if the tree's tex-fmt is older than CTAN's, else None.

    Reads only the CTAN JSON record. A tree without tex-fmt, or an unreachable CTAN,
    gives None.
    """
    if installed(env_dir, arch) is None:
        return None
    have = installed_version(env_dir, arch)
    own = client is None
    cl = client or httpx2.Client(
        follow_redirects=True,
        timeout=httpx2.Timeout(30.0, read=120.0),
        headers={"User-Agent": tlpdb.USER_AGENT},
    )
    try:
        latest = _version(cl)
    except TexFmtError:
        return None
    finally:
        if own:
            cl.close()
    return (have or "unknown", latest) if have != latest else None


def ensure(
    env_dir: Path,
    arch: str | None = None,
    *,
    client: httpx2.Client | None = None,
    notice: Callable[[str], None] | None = None,
    update: bool = True,
) -> Path | None:
    """Make sure ``tex-fmt`` is in the tree; returns its path, or None with a notice.

    The latest version comes from the CTAN JSON record. If the tree already has
    that version nothing else happens. Otherwise the store entry for it is
    cloned into the tree, downloading the binary only if the store lacks it.
    With ``update`` false a tree that has a ``tex-fmt`` is left alone (no request).
    Failures are reported but never raised: tex-fmt is not needed to build, and a
    tree that has one keeps it when CTAN cannot be reached.
    """
    say = notice or (lambda _m: None)
    arch = arch or tlpdb.platform_arch()
    if arch is None:
        say("tex-fmt not installed: unknown architecture")
        return None
    have = installed(env_dir, arch)
    if have is not None and not update:
        return have
    asset = asset_name(arch)
    if asset is None:
        say(f"tex-fmt not installed: CTAN has no {PACKAGE} binary for {arch}")
        return None
    own = client is None
    cl = client or httpx2.Client(
        follow_redirects=True,
        timeout=httpx2.Timeout(30.0, read=120.0),
        headers={"User-Agent": tlpdb.USER_AGENT},
    )
    try:
        version = _version(cl)
        if have is not None and installed_version(env_dir, arch) == version:
            return have
        entry = archive.store_dir() / f"ctan-{PACKAGE}-{version}-{asset}"
        binary = _stored(entry, asset) or _download(cl, asset, entry, version)
    except TexFmtError as e:
        if have is None:
            say(f"tex-fmt not installed: {e}")
        return have
    finally:
        if own:
            cl.close()
    dst = env_dir / "bin" / arch / BINARY
    archive.clone_file(binary, dst)
    rec = _record(env_dir)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({"package": PACKAGE, "version": version}) + "\n", encoding="utf-8"
    )
    return dst
