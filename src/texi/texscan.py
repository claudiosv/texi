r"""Find the packages and classes a TeX source loads.

Parses with the tree-sitter LaTeX grammar, so ``\usepackage`` in ``%`` comments,
``verbatim``-like environments and ``\iffalse ... \fi`` blocks is not counted. If the
grammar is not available (no network on first use), falls back to a regular expression.
"""

from __future__ import annotations

import re
from functools import cache
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


@cache
def _parser() -> Any | None:
    try:
        from tree_sitter_language_pack import get_parser

        return get_parser("latex")
    except Exception:  # not installed, or the grammar download failed
        return None


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
