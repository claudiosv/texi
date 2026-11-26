"""Property tests of the .texenv/ layout and environment (no external tools)."""

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from texi import texenv

FEW = settings(max_examples=40, deadline=None)

arches = st.from_regex(r"[a-z0-9_]{1,8}-[a-z]{1,8}", fullmatch=True)
env_values = st.text(max_size=15)
path_entries = st.text(
    st.characters(codec="utf-8", exclude_characters=os.pathsep + "\0"),
    min_size=1,
    max_size=10,
)
paths = st.lists(path_entries, min_size=1, max_size=4).map(os.pathsep.join)
other_keys = st.one_of(
    st.sampled_from([*texenv.SCRUBBED, "TEXMFHOME", "TEXMFCNF", "HOME"]),
    st.from_regex(r"[A-Z_]{1,10}", fullmatch=True).filter(lambda k: k != "PATH"),
)
# PATH is always present and non-empty; without it see the xfail test below
bases = st.builds(
    lambda env, path: env | {"PATH": path},
    st.dictionaries(other_keys, env_values, max_size=8),
    paths,
)


@given(arch=arches, base=bases)
def test_clone_env(arch: str, base: dict[str, str]) -> None:
    original = dict(base)
    env_dir = Path("/nonexistent/project/.texenv")
    env = texenv.clone_env(env_dir, arch, base)
    root = env_dir.resolve()
    assert base == original
    assert not set(env) & set(texenv.SCRUBBED)
    first, *rest = env["PATH"].split(os.pathsep)
    assert first == str(root / "bin" / arch)
    assert rest == base["PATH"].split(os.pathsep)
    assert "" not in rest
    tex = {k for k in env if k.startswith("TEXMF") and k not in base}
    tex |= {"TEXMFROOT", "TEXMFDIST", "TEXMFHOME", "TEXMFVAR", "TEXMFCNF"}
    for key in tex:
        assert Path(env[key]).is_relative_to(root)
    for key, value in base.items():
        if key not in texenv.SCRUBBED and key != "PATH" and not key.startswith("TEXMF"):
            assert env[key] == value
    assert texenv.clone_env(env_dir, arch, base) == env


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="clone_env without a base PATH leaves an empty PATH entry (cwd)",
)
@pytest.mark.parametrize(
    "base", [pytest.param({}, id="unset"), pytest.param({"PATH": ""}, id="empty")]
)
def test_clone_env_without_path(base: dict[str, str]) -> None:
    env = texenv.clone_env(Path("/nonexistent/.texenv"), "x", base)
    assert "" not in env["PATH"].split(os.pathsep)


repositories = st.text(max_size=30)


@FEW
@given(repository=repositories)
def test_init_usertree_is_a_usertree_and_keeps_its_tlpdb(repository: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp)
        assert not texenv.is_usertree(env_dir)
        texenv.init_usertree(env_dir, repository)
        db = env_dir / "tlpkg" / "texlive.tlpdb"
        first = db.read_bytes()
        assert texenv.is_usertree(env_dir)
        texenv.init_usertree(env_dir, "other")
        assert db.read_bytes() == first


@FEW
@given(
    repository=repositories.filter(lambda r: "setting_usertree" not in r),
)
def test_init_clone_is_not_a_usertree(repository: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp)
        texenv.init_clone(env_dir, repository)
        db = env_dir / "tlpkg" / "texlive.tlpdb"
        first = db.read_bytes()
        assert not texenv.is_usertree(env_dir)
        assert all((env_dir / d).is_dir() for d in texenv.CLONE_DIRS)
        texenv.init_clone(env_dir, "other")
        assert db.read_bytes() == first


@FEW
@given(names=st.lists(arches, max_size=4, unique=True))
def test_env_bin_is_the_first_arch(names: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp)
        for name in names:
            (env_dir / "bin" / name).mkdir(parents=True)
        expected = env_dir / "bin" / min(names) if names else None
        assert texenv.env_bin(env_dir) == expected
