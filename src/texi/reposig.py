"""Verify the signature of a repository's package index with ``gpgv``.

TeX Live signs ``tlpkg/texlive.tlpdb.sha512``, which holds the sha512 of the
uncompressed ``texlive.tlpdb``. The signing key ships with texi
(``data/texlive-signing-key.gpg``) and is never downloaded from a mirror.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Literal

Mode = Literal["auto", "require", "none"]
Status = Literal["verified", "expired", "unsigned", "unavailable", "skipped"]

#: Fingerprint of the primary key of the TeX Live Distribution signing key.
TEXLIVE_FINGERPRINT = "C78B82D8C79512F79CC0D7C80D5E5D9106BAB6BC"
KEYRING = Path(__file__).parent / "data" / "texlive-signing-key.gpg"

_MESSAGES: dict[str, str] = {
    "expired": "the package index is signed with a key that has expired in texi's "
    "copy of the TeX Live key (upgrade texi)",
    "unsigned": "the package index has no TeX Live signature",
    "unavailable": "the package index was not verified: gpgv is unavailable "
    "(install GnuPG)",
}


class SignatureError(Exception):
    """The package index failed verification."""


def message(status: str | None) -> str | None:
    """The warning for a cached verification status, or None if nothing is wrong."""
    if status in {"verified", "skipped"}:
        return None
    return _MESSAGES.get(status or "", "the cached package index was never verified")


def find_gpgv() -> str | None:
    """``gpgv`` on PATH, ignoring relative entries and project ``.texenv`` trees."""
    dirs = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if os.path.isabs(d) and ".texenv" not in Path(d).parts
    ]
    path = os.pathsep.join(dirs)
    return shutil.which("gpgv", path=path) or shutil.which("gpgv2", path=path)


def _run_gpgv(gpgv: str, signature: bytes, signed: bytes) -> Status:
    """Check a detached signature; "verified", "expired" or "unsigned" (unknown key).

    Raises
    ------
    SignatureError
        If the signature is bad, expired or revoked, or gpgv cannot run.
    """
    with tempfile.TemporaryDirectory(prefix="texi-gpgv-") as tmp:
        sig, data = Path(tmp, "signature.asc"), Path(tmp, "signed")
        sig.write_bytes(signature)
        data.write_bytes(signed)
        cmd = [gpgv, "--status-fd", "1", "--keyring", str(KEYRING), str(sig), str(data)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            msg = f"cannot run {gpgv}: {e}"
            raise SignatureError(msg) from e
    status: dict[str, list[str]] = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if line.startswith("[GNUPG:] ") and (words := line.split()[1:]):
            status[words[0]] = words[1:]
    for bad in ("BADSIG", "REVKEYSIG", "EXPSIG"):
        if bad in status:
            msg = f"the signature of the package index was rejected ({bad})"
            raise SignatureError(msg)
    signer = (status.get("VALIDSIG") or [""])[-1]
    if signer == TEXLIVE_FINGERPRINT:
        if "EXPKEYSIG" in status:
            return "expired"
        if "GOODSIG" in status and proc.returncode == 0:
            return "verified"
    elif "NO_PUBKEY" in status:
        return "unsigned"
    detail = proc.stderr.decode(errors="replace").strip()
    msg = f"gpgv could not verify the package index: {detail or proc.returncode}"
    raise SignatureError(msg)


def check(
    tlpdb: bytes,
    base: str,
    *,
    mode: Mode,
    official: bool,
    gpgv: str | None,
    fetch: Callable[[str], bytes],
) -> Status:
    """Verify the uncompressed index ``tlpdb`` downloaded from the mirror ``base``.

    Parameters
    ----------
    tlpdb
        Bytes of ``texlive.tlpdb``.
    base
        Repository URL of the mirror that served the index.
    mode
        ``require`` accepts only "verified". ``auto`` also accepts a missing
        gpgv, an expired key and, unless ``official``, a missing signature.
    official
        The repository is the default CTAN one, which is always signed.
    gpgv
        Path of the program, or None if there is none.
    fetch
        Downloads a URL; raises OSError (URLError) on failure.

    Raises
    ------
    SignatureError
        If the index must not be used.
    """
    if mode == "none":
        return "skipped"
    status: Status = "unavailable"
    if gpgv is not None:
        status = _check_files(tlpdb, f"{base}/tlpkg/texlive.tlpdb.sha512", gpgv, fetch)
    if status != "verified" and (
        mode == "require" or (status == "unsigned" and official)
    ):
        msg = str(message(status))
        raise SignatureError(msg)
    return status


def _check_files(
    tlpdb: bytes, url: str, gpgv: str, fetch: Callable[[str], bytes]
) -> Status:
    def optional(target: str) -> bytes | None:
        try:
            return fetch(target)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 404:
                return None
            msg = f"cannot download {target}: HTTP {e.code}"
            raise SignatureError(msg) from e
        except OSError as e:
            if isinstance(getattr(e, "reason", None), FileNotFoundError):
                return None
            msg = f"cannot download {target}: {e}"
            raise SignatureError(msg) from e

    checksum = optional(url)
    if checksum is None:
        return "unsigned"
    words = checksum.split()
    if not words or words[0].decode(errors="replace").lower() != (
        hashlib.sha512(tlpdb).hexdigest()
    ):
        msg = f"the package index does not match {url}"
        raise SignatureError(msg)
    signature = optional(f"{url}.asc")
    if signature is None:
        return "unsigned"
    return _run_gpgv(gpgv, signature, checksum)
