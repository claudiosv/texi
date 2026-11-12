"""Write ``ls-R`` filename databases (what ``mktexlsr`` does) without running ``mktexlsr``.

Format, as ``mktexlsr.pl`` writes it::

    % ls-R -- filename database for kpathsea; do not change this line.

    ./:
    file-in-the-root
    subdir

    ./subdir:
    file

kpathsea only needs the directory structure and the file names; ordering within
a directory is free, so entries are sorted to make the file reproducible.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

MAGIC = "% ls-R -- filename database for kpathsea; do not change this line."
NAME = "ls-R"
#: Version-control names that ``mktexlsr`` leaves out.
SKIPPED = frozenset({".git", ".svn", ".hg", ".bzr", "CVS"})


class LsrError(Exception):
    """A tree could not be indexed or its ``ls-R`` not written."""


def _listing(root: Path) -> list[tuple[str, list[str]]]:
    """``(dir, entries)`` in the order they are written; ``dir`` is ``.`` or ``./a/b``.

    Symlinks to directories are followed (a link back into a parent is not).
    """
    out: list[tuple[str, list[str]]] = []
    active: set[Path] = set()

    def walk(path: Path, label: str) -> None:
        real = path.resolve()
        if real in active:
            return
        active.add(real)
        try:
            with os.scandir(path) as it:
                entries = sorted(
                    (e for e in it if e.name not in SKIPPED), key=lambda e: e.name
                )
        except OSError as e:
            msg = f"cannot read {path}: {e}"
            raise LsrError(msg) from e
        names = [e.name for e in entries]
        if label == "." and NAME not in names:
            names = sorted([*names, NAME])  # the file we are about to write
        out.append((label, names))
        for e in entries:
            if e.is_dir():  # follows symlinks
                walk(Path(e.path), f"{label}/{e.name}")
        active.discard(real)

    walk(root, ".")
    return out


def render(root: Path) -> str:
    """The text of ``root/ls-R`` for the tree as it is now."""
    parts = [MAGIC, ""]
    for label, names in _listing(root):
        parts.append("./:" if label == "." else f"{label}:")
        parts += names
        parts.append("")
    return "\n".join(parts) + "\n"


def write(root: Path) -> Path:
    """Rebuild ``root/ls-R`` atomically; returns its path.

    Raises
    ------
    LsrError
        ``root`` is not a directory, or not writable.
    """
    if not root.is_dir():
        msg = f"{root} is not a directory"
        raise LsrError(msg)
    text = render(root)
    target = root / NAME
    try:
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".ls-R.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            Path(tmp).chmod(0o644)
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        msg = f"cannot write {target}: {e}"
        raise LsrError(msg) from e
    return target


def write_all(roots: Iterable[Path]) -> list[Path]:
    """:func:`write` for each existing directory in ``roots`` (a missing one is skipped)."""
    return [write(r) for r in roots if r.is_dir()]
