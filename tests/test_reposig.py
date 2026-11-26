"""Package index verification, with a fake gpgv and a fake repository (no network)."""

import hashlib
import io
import lzma
import urllib.error
from pathlib import Path

import pytest

from texi import reposig
from texi import tlpdb as tl

TEXT = b"name 00texlive.config\ncategory TLCore\nrevision 2\n"
OFFICIAL = tl.DEFAULT_REPOSITORY
CUSTOM = "https://r.example/tlnet"
MIRROR = "https://m.example/tlnet"
KEY = reposig.TEXLIVE_FINGERPRINT
GOOD = f"GOODSIG ID TL\nVALIDSIG SUB d 0 0 4 0 1 10 00 {KEY}"
EXPIRED = f"EXPKEYSIG ID TL\nVALIDSIG SUB d 0 0 4 0 1 10 00 {KEY}"


class Resp(io.BytesIO):
    def __init__(self, body: bytes, url: str) -> None:
        super().__init__(body)
        self.headers: dict[str, str] = {}
        self.url = url

    def geturl(self) -> str:
        return self.url


class Repo:
    """What the mirror serves, by file suffix; every request is recorded."""

    def __init__(self) -> None:
        digest = hashlib.sha512(TEXT).hexdigest()
        self.files = {
            ".xz": lzma.compress(TEXT),
            ".sha512": f"{digest}  texlive.tlpdb\n".encode(),
            ".asc": b"signature",
        }
        self.urls: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        suffix = Path(url).suffix
        if suffix not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return Resp(self.files[suffix], f"{MIRROR}/tlpkg/{url.rsplit('/', 1)[1]}")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    served = Repo()
    monkeypatch.setattr(tl, "urlopen", served)
    return served


def fake_gpgv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, code: int = 0
) -> None:
    script = tmp_path / "gpgv"
    lines = "".join(f"echo '[GNUPG:] {s}'\n" for s in status.splitlines())
    script.write_text(f"#!/bin/sh\n{lines}exit {code}\n")
    script.chmod(0o755)
    monkeypatch.setattr(reposig, "find_gpgv", lambda: str(script))


def test_verified_from_the_serving_mirror(repo, tmp_path, monkeypatch) -> None:
    fake_gpgv(tmp_path, monkeypatch, GOOD)
    db = tl.load_remote(OFFICIAL, verify="require")
    assert db.signature_warning is None
    assert repo.urls[1:] == [
        f"{MIRROR}/tlpkg/texlive.tlpdb.sha512",
        f"{MIRROR}/tlpkg/texlive.tlpdb.sha512.asc",
    ]
    assert tl.load_remote(OFFICIAL, verify="require").from_cache
    assert len(repo.urls) == 3


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("BADSIG ID TL", 1),
        (f"REVKEYSIG ID TL\nVALIDSIG S d 0 0 4 0 1 10 00 {KEY}", 0),
        ("GOODSIG ID X\nVALIDSIG S d 0 0 4 0 1 10 00 OTHERKEY", 0),
        ("", 0),
        (GOOD, 2),
    ],
)
def test_rejected_signature_is_not_cached(
    repo, tmp_path, monkeypatch, status, code
) -> None:
    fake_gpgv(tmp_path, monkeypatch, status, code)
    with pytest.raises(tl.TlpdbVerifyError):
        tl.load_remote(CUSTOM)
    assert not tl.has_cache(CUSTOM)


def test_wrong_hash_is_rejected_and_retried(repo, tmp_path, monkeypatch) -> None:
    fake_gpgv(tmp_path, monkeypatch, GOOD)
    repo.files[".sha512"] = b"0" * 128 + b"  texlive.tlpdb\n"
    with pytest.raises(tl.TlpdbVerifyError, match="does not match"):
        tl.load_remote(CUSTOM)
    assert sum(u.endswith(".xz") for u in repo.urls) == 1 + tl.STALE_RETRIES


def test_failure_keeps_the_verified_cache(repo, tmp_path, monkeypatch) -> None:
    fake_gpgv(tmp_path, monkeypatch, GOOD)
    tl.load_remote(OFFICIAL)
    fake_gpgv(tmp_path, monkeypatch, "BADSIG ID TL", 1)
    db = tl.load_remote(OFFICIAL, refresh=True, verify="require")
    assert db.from_cache
    assert "rejected" in str(db.signature_warning)


@pytest.mark.parametrize("missing", [".sha512", ".asc"])
def test_missing_signature(repo, tmp_path, monkeypatch, missing) -> None:
    fake_gpgv(tmp_path, monkeypatch, GOOD)
    del repo.files[missing]
    assert "no TeX Live signature" in str(tl.load_remote(CUSTOM).signature_warning)
    with pytest.raises(tl.TlpdbVerifyError):
        tl.load_remote(OFFICIAL)
    with pytest.raises(tl.TlpdbVerifyError):
        tl.load_remote(CUSTOM, verify="require")


def test_expired_key(repo, tmp_path, monkeypatch) -> None:
    fake_gpgv(tmp_path, monkeypatch, EXPIRED)
    assert "expired" in str(tl.load_remote(OFFICIAL).signature_warning)
    with pytest.raises(tl.TlpdbVerifyError):
        tl.load_remote(OFFICIAL, verify="require")


def test_without_gpgv(repo) -> None:
    db = tl.load_remote(OFFICIAL)
    assert "gpgv is unavailable" in str(db.signature_warning)
    assert len(repo.urls) == 1
    with pytest.raises(tl.TlpdbVerifyError):
        tl.load_remote(OFFICIAL, verify="require")


def test_unverified_cache_is_downloaded_again(repo, tmp_path, monkeypatch) -> None:
    tl.load_remote(OFFICIAL)
    fake_gpgv(tmp_path, monkeypatch, GOOD)
    db = tl.load_remote(OFFICIAL)
    assert not db.from_cache
    assert db.signature_warning is None


def test_none_skips(repo, tmp_path, monkeypatch) -> None:
    fake_gpgv(tmp_path, monkeypatch, "BADSIG ID TL", 1)
    assert tl.load_remote(OFFICIAL, verify="none").signature_warning is None
    assert len(repo.urls) == 1


def test_mode_comes_from_the_project(repo, tmp_path) -> None:
    (tmp_path / "texproject.toml").write_text('[project]\nverify_repo = "require"\n')
    with pytest.raises(tl.TlpdbVerifyError):
        tl.load_remote(OFFICIAL)


def test_find_gpgv_ignores_project_trees(tmp_path, monkeypatch) -> None:
    monkeypatch.undo()  # the real find_gpgv
    bin_dir = tmp_path / ".texenv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gpgv").write_text("#!/bin/sh\n")
    (bin_dir / "gpgv").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert reposig.find_gpgv() is None
