"""Property tests of the tex-fmt installer against a mock CTAN (no network)."""

import gzip
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

import httpx2
import pytest
from hypothesis import example, given, settings
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


json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(["version", "number", "x"]), inner, max_size=3),
    ),
    max_leaves=6,
)
# The version becomes part of a store directory name; these are the usable ones.
versions = st.from_regex(r"[0-9]{1,3}(\.[0-9]{1,3}){0,2}", fullmatch=True)
good_info = versions.map(lambda v: json.dumps({"version": {"number": v}}).encode())
infos = st.one_of(
    good_info,
    st.binary(max_size=60),
    json_values.map(lambda v: json.dumps(v).encode()),
    # overlong versions and ones that look like paths
    st.one_of(
        st.text(max_size=20),
        st.text("./\\a1", max_size=20),
        st.integers(250, 400).map(lambda n: "1" * n),
    ).map(lambda v: json.dumps({"version": {"number": v}}).encode()),
    # nested deeply enough to overflow the decoder
    st.just(b"[" * 500_000),
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


@st.composite
def truncated_tarballs(draw: st.DrawFn) -> bytes:
    raw = draw(damaged_tarballs())
    return raw[: draw(st.integers(0, len(raw)))]


bodies = st.one_of(st.binary(max_size=80), damaged_tarballs(), truncated_tarballs())
# contents of an existing tlpkg/texi-tex-fmt.json
records = st.one_of(
    st.none(),
    st.text(max_size=20),
    json_values.map(json.dumps),
    st.just("[" * 500_000),
)
statuses = st.one_of(st.just(200), st.integers(100, 599))


@settings(max_examples=120, deadline=None)
@given(
    info=st.tuples(statuses, infos),
    download=st.tuples(statuses, bodies),
    installed=st.booleans(),
    record=records,
)
@example(info=(200, b"[]"), download=(200, b""), installed=False, record=None)
@example(info=(200, b"null"), download=(200, b""), installed=True, record=None)
def test_ensure_reports_failures_instead_of_raising(
    info: tuple[int, bytes],
    download: tuple[int, bytes],
    installed: bool,
    record: str | None,
) -> None:
    path, content, notices = _ensure(info, download, installed=installed, record=record)
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


def test_ensure_survives_a_truncated_download() -> None:
    _ensure((200, INFO), (200, GOOD[: len(GOOD) // 2]))


def test_ensure_survives_deeply_nested_json() -> None:
    _ensure((200, b"[" * 500_000), (200, GOOD))


def test_ensure_survives_an_overlong_version() -> None:
    info = json.dumps({"version": {"number": "1" * 300}}).encode()
    _ensure((200, info), (200, GOOD))


@pytest.mark.parametrize("record", ["[]", "null"])
def test_ensure_survives_a_damaged_version_record(record: str) -> None:
    _ensure((200, INFO), (200, GOOD), installed=True, record=record)


@pytest.mark.parametrize("meta", ["[]", "null"])
def test_a_damaged_store_record_is_not_trusted(meta: str, tmp_path: Path) -> None:
    (tmp_path / "texi-ctan.json").write_text(meta)
    assert texfmt._stored(tmp_path, ARCH) is None


@pytest.mark.parametrize(
    "version",
    [
        "../../../../escaped",
        "1/../../../escaped",
        "..\\..\\escaped",
        "/etc/escaped",
        "1/2",
        "1\x002",
        "",
    ],
)
def test_a_version_cannot_leave_the_store(version: str, tmp_path: Path) -> None:
    info = json.dumps({"version": {"number": version}}).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = info if str(request.url) == texfmt.INFO_URL else GOOD
        return httpx2.Response(200, content=body)

    notices: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", str(tmp_path / "a" / "b" / "cache"))
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
            env = tmp_path / ".texenv"
            assert (
                texfmt.ensure(env, ARCH, client=client, notice=notices.append) is None
            )
    assert len(notices) == 1
    assert list(tmp_path.rglob("*")) == []


def test_a_bad_redirect_is_reported() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "http://[bad"})

    notices: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", tmp)
        with httpx2.Client(
            transport=httpx2.MockTransport(handler), follow_redirects=True
        ) as client:
            env = Path(tmp) / ".texenv"
            assert (
                texfmt.ensure(env, ARCH, client=client, notice=notices.append) is None
            )
    assert len(notices) == 1


@pytest.mark.parametrize("blocked", ["cache", ".texenv"])
def test_ensure_survives_an_unwritable_destination(
    blocked: str, tmp_path: Path
) -> None:
    (tmp_path / blocked).write_text("a file where a directory is needed")

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = INFO if str(request.url) == texfmt.INFO_URL else GOOD
        return httpx2.Response(200, content=body)

    notices: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
            env = tmp_path / ".texenv"
            assert (
                texfmt.ensure(env, ARCH, client=client, notice=notices.append) is None
            )
    assert len(notices) == 1


def test_a_version_command_printing_garbage_gives_none(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / ARCH / texfmt.BINARY
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nprintf '\\377'\n")
    binary.chmod(0o755)
    assert texfmt.installed_version(tmp_path, ARCH) is None
