"""Property tests of package index verification (no network, no real gpgv)."""

import hashlib
import lzma
import subprocess
import tempfile
import urllib.error
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from texi import reposig
from texi import tlpdb as tl

KEY = reposig.TEXLIVE_FINGERPRINT
REPO = "https://r.example/tlnet"
REJECT = ("BADSIG", "REVKEYSIG", "EXPSIG")
MODES = st.sampled_from(["auto", "require", "none"])

keywords = st.sampled_from([
    *REJECT,
    "GOODSIG",
    "EXPKEYSIG",
    "VALIDSIG",
    "NO_PUBKEY",
    "ERRSIG",
    "NEWSIG",
])
words = st.sampled_from([KEY, KEY.lower(), KEY[:-1], "0" * 40, "ID"])
status_lines = st.builds(
    lambda k, args: " ".join(["[GNUPG:]", k, *args]),
    keywords,
    st.lists(words, max_size=3),
)
noise = st.one_of(st.text(), st.just("[GNUPG:] "), status_lines.map(" ".__add__))
outputs = st.lists(st.one_of(status_lines, noise), max_size=8).map("\n".join)


@given(stdout=outputs, code=st.integers(0, 2))
@example(stdout="[GNUPG:] VALIDSIG", code=0)
@example(stdout="[GNUPG:] ", code=0)
def test_gpgv_output_is_trusted_only_with_the_pinned_key(
    stdout: str, code: int
) -> None:
    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, code, stdout.encode(), b"")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(reposig.subprocess, "run", run)
        try:
            result = reposig._run_gpgv("gpgv", b"sig", b"data")
        except reposig.SignatureError:
            return
    rows = [
        line.split()[1:] for line in stdout.splitlines() if line.startswith("[GNUPG:] ")
    ]
    seen = {row[0] for row in rows if row}
    assert not seen & set(REJECT)
    if result == "unsigned":
        assert "NO_PUBKEY" in seen
        return
    assert any(row[0] == "VALIDSIG" and row[-1] == KEY for row in rows if row)
    if result == "verified":
        assert code == 0
        assert "GOODSIG" in seen
        assert "EXPKEYSIG" not in seen
    else:
        assert result == "expired"
        assert "EXPKEYSIG" in seen


@given(data=st.binary(max_size=200), checksum=st.binary(max_size=200))
def test_a_checksum_that_does_not_match_never_reaches_gpgv(
    data: bytes, checksum: bytes
) -> None:
    digest = hashlib.sha512(data).hexdigest().encode()
    matches = checksum.split()[:1] != [] and checksum.split()[0].lower() == digest

    def never(*args: object) -> reposig.Status:
        pytest.fail("gpgv ran on a mismatching checksum")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(reposig, "_run_gpgv", never)
        if not matches:
            with pytest.raises(reposig.SignatureError):
                reposig._check_files(data, "u", "gpgv", lambda url: checksum)


def _missing(url: str) -> bytes:
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]


outcomes = st.sampled_from(["verified", "expired", "unsigned", "bad"])


@given(
    data=st.binary(max_size=100),
    mode=MODES,
    official=st.booleans(),
    outcome=st.one_of(st.none(), outcomes),
)
def test_check_policy(
    data: bytes, mode: reposig.Mode, official: bool, outcome: str | None
) -> None:
    """``outcome`` None means there is no gpgv."""

    def fetch(url: str) -> bytes:
        if outcome == "unsigned":
            return _missing(url)
        return hashlib.sha512(data).hexdigest().encode()

    def run(*args: object) -> str:
        if outcome == "bad":
            msg = "bad"
            raise reposig.SignatureError(msg)
        return str(outcome)

    status = outcome or "unavailable"
    refused = mode != "none" and (
        status == "bad"
        or (mode == "require" and status != "verified")
        or (official and status == "unsigned")
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(reposig, "_run_gpgv", run)

        def check() -> reposig.Status:
            return reposig.check(
                data,
                REPO,
                mode=mode,
                official=official,
                gpgv=None if outcome is None else "gpgv",
                fetch=fetch,
            )

        if refused:
            with pytest.raises(reposig.SignatureError):
                check()
        else:
            assert check() == ("skipped" if mode == "none" else status)


class Resp:
    def __init__(self, body: bytes, url: str) -> None:
        self.body, self.url = body, url
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body

    def geturl(self) -> str:
        return self.url


def _index(revision: int) -> bytes:
    return f"name pkg\ncategory Package\nrevision {revision}\n".encode()


steps = st.tuples(MODES, st.one_of(st.none(), outcomes), st.booleans())


@settings(max_examples=40, deadline=None)
@given(history=st.lists(steps, min_size=1, max_size=6))
def test_cache_never_holds_a_rejected_index(
    history: list[tuple[reposig.Mode, str | None, bool]],
) -> None:
    """Any sequence of loads, each of a newer index, against one cache."""
    accepted: dict[int, str] = {}  # revision -> status it was accepted with
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", tmp)
        for revision, (mode, outcome, refresh) in enumerate(history, start=1):
            text = _index(revision)

            def urlopen(req, timeout, text=text, outcome=outcome):
                url = req.full_url
                if url.endswith(".xz"):
                    return Resp(lzma.compress(text), url)
                if outcome == "unsigned":
                    return _missing(url)
                return Resp(hashlib.sha512(text).hexdigest().encode(), url)

            def run(*args: object, outcome=outcome) -> str:
                if outcome == "bad":
                    msg = "bad"
                    raise reposig.SignatureError(msg)
                return str(outcome)

            mp.setattr(tl, "urlopen", urlopen)
            mp.setattr(reposig, "_run_gpgv", run)
            gpgv = None if outcome is None else "gpgv"
            mp.setattr(reposig, "find_gpgv", lambda gpgv=gpgv: gpgv)
            status = "skipped" if mode == "none" else outcome or "unavailable"
            try:
                db = tl.load_remote(REPO, refresh=refresh, verify=mode)
            except tl.TlpdbVerifyError:
                assert mode != "none"
                assert status != "verified"
                continue
            got = db.revisions()["pkg"]
            db.close()
            if got == revision and not db.from_cache:
                assert status != "bad"
                assert mode != "require" or status == "verified"
                accepted[got] = status
            else:  # an older, cached copy: it must have been accepted earlier
                assert got in accepted
                if mode == "require":
                    assert accepted[got] == "verified"
            xz = next(Path(tmp).rglob("*.xz"))
            assert lzma.decompress(xz.read_bytes()) == _index(max(accepted))


path_parts = st.lists(
    st.sampled_from(["a", "bin", ".texenv", "x.texenv"]), min_size=1, max_size=4
)


@given(entries=st.lists(st.tuples(st.booleans(), path_parts), max_size=6))
def test_gpgv_search_path_is_absolute_and_outside_texenv(
    entries: list[tuple[bool, list[str]]],
) -> None:
    dirs = [("/" if absolute else "") + "/".join(p) for absolute, p in entries]
    searched: list[str] = []

    def which(name: str, path: str) -> None:
        searched.append(path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", ":".join(dirs))
        mp.setattr(reposig.shutil, "which", which)
        _find_gpgv()
    wanted = [d for (a, p), d in zip(entries, dirs) if a and ".texenv" not in p]
    assert set(searched) == {":".join(wanted)}


# The autouse fixture in conftest replaces find_gpgv, so keep the real one.
_find_gpgv = reposig.find_gpgv
