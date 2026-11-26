"""Property tests of the tex-fmt installer against a mock CTAN (no network)."""

import gzip
import io
import json
import subprocess
import tarfile
import tempfile
import zlib
from pathlib import Path

import httpx2
import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from texi import texfmt

ARCH = "x86_64-linux"
OLD = b"old binary"


def _tarball(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _ensure(
    info: tuple[int, bytes],
    download: tuple[int, bytes],
    *,
    installed: bool = False,
    record: str | None = None,
) -> tuple[Path | None, bytes | None, list[str]]:
    """Run ensure() on a fresh tree and cache: (path, its content, notices)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        status, body = info if str(request.url) == texfmt.INFO_URL else download
        return httpx2.Response(status, content=body)

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "tex-fmt 0.0.1\n", "")

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", f"{tmp}/cache")
        mp.setattr(texfmt.subprocess, "run", run)
        env = Path(tmp) / ".texenv"
        if installed:
            binary = env / "bin" / ARCH / texfmt.BINARY
            binary.parent.mkdir(parents=True)
            binary.write_bytes(OLD)
        if record is not None:
            (env / "tlpkg").mkdir(parents=True)
            (env / "tlpkg" / "texi-tex-fmt.json").write_text(record)
        notices: list[str] = []
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
            path = texfmt.ensure(env, ARCH, client=client, notice=notices.append)
        if path is not None:
            assert path == env / "bin" / ARCH / texfmt.BINARY
        return path, None if path is None else path.read_bytes(), notices


def _truncated_gzip(body: bytes) -> bool:
    """Whether reading the body as gzip runs off its end (EOFError)."""
    try:
        gzip.decompress(body)
    except EOFError:
        return True
    except OSError, zlib.error:
        pass
    return False


json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(["version", "number", "x"]), inner, max_size=3),
    ),
    max_leaves=6,
)
# The version becomes part of a store directory name, so keep it short and plain.
# Not covered (xfails below): JSON nested deeply enough to overflow the decoder,
# and a version too long to be a file name.
versions = st.from_regex(r"[0-9]{1,3}(\.[0-9]{1,3}){0,2}", fullmatch=True)
good_info = versions.map(lambda v: json.dumps({"version": {"number": v}}).encode())
infos = st.one_of(
    good_info,
    st.binary(max_size=60),
    json_values.filter(
        lambda v: not (isinstance(v, dict) and isinstance(v.get("version"), dict))
    ).map(lambda v: json.dumps(v).encode()),
)


@st.composite
def damaged_tarballs(draw: st.DrawFn) -> bytes:
    """A tarball, perhaps with the wrong member or some bytes overwritten."""
    name = draw(st.sampled_from(["tex-fmt", "dir/tex-fmt", "other"]))
    raw = bytearray(_tarball(name, draw(st.binary(max_size=40))))
    # Damage the tar itself, the gzip layer around it, or neither.
    inner = draw(st.booleans())
    if not inner:
        raw = bytearray(gzip.compress(bytes(raw), mtime=0))
    for _ in range(draw(st.integers(0, 3))):
        at = draw(st.integers(0, min(len(raw), 600) - 1))
        raw[at] = draw(st.integers(0, 255))
    return gzip.compress(bytes(raw), mtime=0) if inner else bytes(raw)


bodies = st.one_of(st.binary(max_size=80), damaged_tarballs())
statuses = st.one_of(st.just(200), st.integers(100, 599))


@settings(max_examples=120, deadline=None)
@given(
    info=st.tuples(statuses, infos),
    download=st.tuples(statuses, bodies),
    installed=st.booleans(),
)
@example(info=(200, b"[]"), download=(200, b""), installed=False)
@example(info=(200, b"null"), download=(200, b""), installed=True)
def test_ensure_reports_failures_instead_of_raising(
    info: tuple[int, bytes], download: tuple[int, bytes], installed: bool
) -> None:
    # A cut-off gzip stream raises EOFError: see the xfail test.
    assume(not _truncated_gzip(download[1]))
    path, content, notices = _ensure(info, download, installed=installed)
    if path is None:
        assert not installed
        assert len(notices) == 1
    else:
        assert notices == []
        if installed and not (200 <= info[0] < 300 and 200 <= download[0] < 300):
            assert content == OLD


@settings(max_examples=30, deadline=None)
@given(version=versions, data=st.binary(max_size=100))
def test_ensure_installs_what_was_downloaded(version: str, data: bytes) -> None:
    info = json.dumps({"version": {"number": version}}).encode()
    body = gzip.compress(_tarball("tex-fmt", data), mtime=0)
    path, content, notices = _ensure((200, info), (200, body))
    assert path is not None
    assert content == data
    assert notices == []


INFO = b'{"version": {"number": "1.0"}}'
GOOD = gzip.compress(_tarball("tex-fmt", b"x" * 100), mtime=0)


@pytest.mark.xfail(
    strict=True,
    raises=EOFError,
    reason="_download does not catch EOFError from a truncated gzip stream",
)
def test_ensure_survives_a_truncated_download() -> None:
    _ensure((200, INFO), (200, GOOD[: len(GOOD) // 2]))


@pytest.mark.xfail(
    strict=True,
    raises=RecursionError,
    reason="_version does not catch RecursionError from deeply nested JSON",
)
def test_ensure_survives_deeply_nested_json() -> None:
    _ensure((200, b"[" * 500_000), (200, GOOD))


@pytest.mark.xfail(
    strict=True,
    raises=OSError,
    reason="_download does not catch OSError creating the entry of an overlong version",
)
def test_ensure_survives_an_overlong_version() -> None:
    info = json.dumps({"version": {"number": "1" * 300}}).encode()
    _ensure((200, info), (200, GOOD))


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="installed_version does not catch TypeError for a non-object record",
)
@pytest.mark.parametrize("record", ["[]", "null"])
def test_ensure_survives_a_damaged_version_record(record: str) -> None:
    _ensure((200, INFO), (200, GOOD), installed=True, record=record)


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="_stored does not catch TypeError for a non-object texi-ctan.json",
)
@pytest.mark.parametrize("meta", ["[]", "null"])
def test_a_damaged_store_record_is_not_trusted(meta: str, tmp_path: Path) -> None:
    (tmp_path / "texi-ctan.json").write_text(meta)
    assert texfmt._stored(tmp_path, ARCH) is None
