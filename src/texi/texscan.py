r"""Find the packages and classes a TeX source loads.

Parses with the tree-sitter LaTeX grammar, so ``\usepackage`` in ``%`` comments,
``verbatim``-like environments and ``\iffalse ... \fi`` blocks is not counted. If the
grammar is not available (no network on first use), falls back to a regular expression.
"""

from __future__ import annotations

import os
import re
from functools import cache
from pathlib import Path
from typing import Any

_INCLUDE_RE = re.compile(
    r"\\(?:usepackage|RequirePackage|documentclass|LoadClass)(?:WithOptions)?"
    r"(?:\s*\[[^\]]*\])?\s*\{([^}]*)\}"
)
#: What a package or class name may look like; drops ``#1``, ``\foo`` and prose.
_NAME = re.compile(r"[A-Za-z0-9][\w.+-]*(?:/[\w.+-]+)*")
#: Loading commands the grammar parses as plain commands.
_GENERIC = frozenset({
    r"\LoadClass",
    r"\LoadClassWithOptions",
    r"\RequirePackageWithOptions",
})
_INCLUDES = frozenset({"package_include", "class_include"})


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/texi/grammar`` (default ``~/.cache/texi/grammar``)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "texi" / "grammar"


@cache
def _pack() -> Any | None:
    """The language pack, set to keep its grammars in :func:`cache_dir`."""
    try:
        import tree_sitter_language_pack as pack
        from tree_sitter_language_pack.options import PackConfig

        pack.configure(PackConfig(cache_dir=str(cache_dir())))
        return pack
    except Exception:  # not installed
        return None


@cache
def _parser() -> Any | None:
    try:
        return _pack().get_parser("latex")
    except Exception:  # not installed, or the grammar download failed
        return None


def is_cached() -> bool:
    """True if the grammar is already on disk, so loading it needs no network."""
    try:
        return "latex" in _pack().downloaded_languages()
    except Exception:
        return False


def prefetch() -> bool:
    """Download the grammar now (while there is network); True if it is usable."""
    return _parser() is not None


def _names(raw: str) -> set[str]:
    return {n for part in raw.split(",") if _NAME.fullmatch(n := part.strip())}


def _by_regex(text: str) -> set[str]:
    found: set[str] = set()
    for m in _INCLUDE_RE.finditer(text):
        found |= _names(m.group(1))
    return found


def _by_tree(parser: Any, src: bytes) -> set[str]:
    found: set[str] = set()
    stack = [parser.parse(src).root_node]
    while stack:
        node = stack.pop()
        if node.type in _INCLUDES:
            group = node.child_by_field_name("paths") or node.child_by_field_name(
                "path"
            )
            if group is not None:
                for p in group.children_by_field_name("path"):
                    found |= _names(p.text.decode("utf-8", "ignore"))
            continue
        if node.type == "generic_command":
            cmd = node.child_by_field_name("command")
            if cmd is not None and cmd.text.decode() in _GENERIC:
                for arg in node.children_by_field_name("arg"):
                    if arg.type == "curly_group":
                        found |= _names(arg.text.decode("utf-8", "ignore")[1:-1])
                        break
        stack.extend(node.children)
    return found


def loaded_names(src: bytes) -> set[str]:
    r"""Names in ``\usepackage``, ``\RequirePackage``, ``\documentclass`` and ``\LoadClass``."""
    parser = _parser()
    if parser is None:
        return _by_regex(src.decode("utf-8", "ignore"))
    return _by_tree(parser, src)


def unclosed_braces(text: str) -> list[int] | None:
    """Offsets in ``text`` of ``{`` that the parser could not match with a ``}``.

    The grammar also reports some valid LaTeX this way (an environment opened in one
    macro and closed in another), so use the result only to narrow down a brace TeX
    already complained about. None if the grammar is not available.
    """
    parser = _parser()
    if parser is None:
        return None
    src = text.encode("utf-8", "surrogatepass")
    offsets: set[int] = set()
    stack = [parser.parse(src).root_node]
    while stack:
        node = stack.pop()
        parent = node.parent
        if (
            node.type == "{"
            and not node.is_named
            and parent is not None
            and parent.type == "ERROR"
        ) or (node.type == "ERROR" and src.startswith(b"{", node.start_byte)):
            offsets.add(node.start_byte)
        stack.extend(node.children)
    if text.isascii():
        return sorted(offsets)
    return sorted(len(src[:o].decode("utf-8", "surrogatepass")) for o in offsets)
