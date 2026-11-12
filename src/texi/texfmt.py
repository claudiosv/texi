"""Install ``tex-fmt`` (CTAN package ``latex-formatter``) into ``.texenv/bin/<arch>/``.

``latex-formatter`` is not in TeX Live, so it has no tlnet container and no
published checksum. CTAN ships one zip with prebuilt binaries for every platform
(``bin/tex-fmt-<target>.tar.gz``). The binary is kept once in the archive store
(``ctan-latex-formatter-<version>/``) and cloned into each tree, like a package.
The sha256 of what was downloaded is recorded in the store and checked on reuse,
so a changed or damaged store entry is noticed, but the first download is trusted
(HTTPS from CTAN only).
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from texi import archive, tlpdb

PACKAGE = "latex-formatter"
ZIP_URL = f"https://mirrors.ctan.org/support/{PACKAGE}.zip"
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
        return "aarch64-macos" if m in ("arm64", "aarch64") else "x86_64-macos"
    return _ASSETS.get(arch)


class TexFmtError(Exception):
    """tex-fmt could not be downloaded or unpacked."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(client: httpx.Client) -> str:
    try:
        r = client.get(INFO_URL)
        r.raise_for_status()
        return str(r.json()["version"]["number"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        raise TexFmtError(f"cannot read the {PACKAGE} version from CTAN: {e}") from e


def _stored(entry: Path, asset: str) -> Path | None:
    """The binary of a store entry if it is complete and unchanged."""
    try:
        meta = json.loads((entry / "texi-ctan.json").read_text(encoding="utf-8"))
        binary = entry / BINARY
        if meta["asset"] == asset and _sha256(binary.read_bytes()) == meta["sha256"]:
            return binary
    except (OSError, ValueError, KeyError):
        pass
    return None


def _download(
    client: httpx.Client, asset: str, entry: Path, version: str
) -> Path:
    try:
        r = client.get(ZIP_URL)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            tgz = zf.read(f"{PACKAGE}/bin/{BINARY}-{asset}.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
            member = next(
                (m for m in tf.getmembers() if m.isfile() and Path(m.name).name == BINARY),
                None,
            )
            if member is None or (fh := tf.extractfile(member)) is None:
                raise TexFmtError(f"no {BINARY} in {BINARY}-{asset}.tar.gz")
            data = fh.read()
    except (httpx.HTTPError, zipfile.BadZipFile, KeyError, tarfile.TarError, OSError) as e:
        raise TexFmtError(f"cannot download {PACKAGE}: {e}") from e
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
                "url": ZIP_URL,
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


def ensure(
    env_dir: Path,
    arch: str | None = None,
    *,
    client: httpx.Client | None = None,
    notice: Callable[[str], None] | None = None,
) -> Path | None:
    """Make sure ``tex-fmt`` is in the tree; returns its path, or None with a notice.

    Nothing is downloaded when the tree has it. Otherwise the store entry for
    the current CTAN version is used, downloading the zip only if it is missing.
    A failure is reported but never raised: tex-fmt is not needed to build.
    """
    say = notice or (lambda _m: None)
    arch = arch or tlpdb.platform_arch()
    if arch is None:
        say("tex-fmt not installed: unknown architecture")
        return None
    if (have := installed(env_dir, arch)) is not None:
        return have
    asset = asset_name(arch)
    if asset is None:
        say(f"tex-fmt not installed: CTAN has no {PACKAGE} binary for {arch}")
        return None
    own = client is None
    cl = client or httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=120.0),
        headers={"User-Agent": tlpdb.USER_AGENT},
    )
    try:
        version = _version(cl)
        entry = archive.store_dir() / f"ctan-{PACKAGE}-{version}-{asset}"
        binary = _stored(entry, asset) or _download(cl, asset, entry, version)
    except TexFmtError as e:
        say(f"tex-fmt not installed: {e}")
        return None
    finally:
        if own:
            cl.close()
    dst = env_dir / "bin" / arch / BINARY
    archive.clone_file(binary, dst)
    return dst
