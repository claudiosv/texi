"""Property tests of the ls-R writer against an independent walk of random trees."""

import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from texi import lsr

# Distinct even on a case-insensitive filesystem.
names = st.sampled_from(["a", "b", "c", "Z", ".hidden", "ls-R", ".git", "CVS", "x y"])
paths = st.lists(names, min_size=1, max_size=3)
# A symlink points at an earlier directory, at a file, at itself, or nowhere.
operations = st.lists(
    st.tuples(st.sampled_from(["dir", "file", "link"]), paths, st.integers(-3, 20)),
    max_size=10,
)


def build(root: Path, ops: list[tuple[str, list[str], int]]) -> None:
    dirs, files, links = [root], [], 0
    for kind, parts, pick in ops:
        path = root.joinpath(*parts)
        # Create nothing through a symlink or below a file.
        above = [root.joinpath(*parts[:i]) for i in range(1, len(parts))]
        if any(p.is_symlink() or p.is_file() for p in above):
            continue
        if path.is_symlink() or path.exists():
            continue
        if kind == "link" and links == 3:  # cross links multiply the listing
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        dirs += [p for p in above if p not in dirs]
        if kind == "dir":
            path.mkdir()
            dirs.append(path)
        elif kind == "file":
            path.write_bytes(b"")
            files.append(path)
        else:
            links += 1
            if pick == -3:
                target = path
            elif pick == -2:
                target = root / "nowhere"
            elif pick == -1 and files:
                target = files[0]
            else:
                target = dirs[pick % len(dirs)]
            path.symlink_to(os.path.relpath(target, path.parent))


def expected(root: Path) -> dict[str, list[str]]:
    """``label -> names``, not following a link back to a directory above."""
    out: dict[str, list[str]] = {}
    todo = [(str(root), ".", frozenset([os.path.realpath(root)]))]
    while todo:
        path, label, above = todo.pop()
        listed = [n for n in os.listdir(path) if n not in lsr.SKIPPED]
        if label == "." and "ls-R" not in listed:
            listed.append("ls-R")
        out[label] = sorted(listed)
        for name in listed:
            child = os.path.join(path, name)
            if name not in lsr.SKIPPED and Path(child).is_dir():
                real = os.path.realpath(child)
                if real not in above:
                    todo.append((child, f"{label}/{name}", above | {real}))
    return out


def parse(text: str) -> list[tuple[str, list[str]]]:
    assert text.startswith(lsr.MAGIC + "\n\n")
    assert text.endswith("\n\n")
    out = []
    for block in text[len(lsr.MAGIC) + 2 : -2].split("\n\n"):
        head, *listed = block.split("\n")
        assert head.endswith(":")
        out.append(("." if head == "./:" else head[:-1], listed))
    return out


@settings(max_examples=60, deadline=None)
@given(ops=operations)
def test_render_lists_what_an_independent_walk_finds(
    ops: list[tuple[str, list[str], int]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, ops)
        blocks = parse(lsr.render(root))
        assert blocks[0][0] == "."
        assert len(blocks) == len(dict(blocks))
        assert dict(blocks) == expected(root)


@settings(max_examples=30, deadline=None)
@given(ops=operations)
def test_write_is_stable_and_leaves_no_temporary_file(
    ops: list[tuple[str, list[str], int]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "tree")
        root.mkdir()
        build(root, ops)
        target = root / "ls-R"
        if target.is_dir() or target.is_symlink():
            return  # not a state texi or mktexlsr creates
        before = set(os.listdir(root))
        text = lsr.render(root)
        assert lsr.write_all([root, Path(tmp, "absent")]) == [target]
        assert target.read_text(encoding="utf-8") == text
        assert set(os.listdir(root)) == before | {"ls-R"}
        assert lsr.write(root) == target
        assert target.read_text(encoding="utf-8") == text


# --- regressions --------------------------------------------------------------


@pytest.mark.parametrize("operation", [lsr.render, lsr.write])
def test_a_symlink_to_itself_does_not_raise_a_raw_os_error(
    tmp_path: Path, operation: Callable[[Path], object]
) -> None:
    (tmp_path / "self").symlink_to("self")
    with contextlib.suppress(lsr.LsrError):
        operation(tmp_path)
    assert "self" in lsr.render(tmp_path).split("\n")
