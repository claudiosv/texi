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
    rb"\LoadClass",
    rb"\LoadClassWithOptions",
    rb"\RequirePackageWithOptions",
})
_INCLUDES = frozenset({"package_include", "class_include"})
#: How far past such a command to look for its names.
_MAX_OPTIONS = 4000
#: What follows such a command when it has options, which the grammar leaves outside
#: the command: blanks and comments, ``[...]`` groups, then the ``{names}``.
_OPTIONS_THEN_NAMES = re.compile(
    rb"(?:\s|%[^\r\n]*[\r\n])*"
    rb"(?:\[(?:\\.|%[^\r\n]*[\r\n]|\{[^{}]*\}|[^]\\%{}])*\](?:\s|%[^\r\n]*[\r\n])*)+"
    rb"\{([^}]*)\}"
)
_DOCUMENTCLASS_RE = re.compile(
    r"(?P<declaration>\\documentclass(?![A-Za-z@])"
    r"(?:\s*\[[^\]]*\])?\s*\{[^}]+\})|\\[^\r\n]"
)


def _without_comments(text: str) -> str:
    r"""Strip TeX line comments, respecting escaped percent signs (``\%``)."""
    return re.sub(
        r"\\[^\r\n]|%[^\r\n]*", lambda m: "" if m[0].startswith("%") else m[0], text
    )


def _without_nul(src: bytes) -> bytes:
    """The grammar ends a comment at a NUL byte, TeX does not; offsets stay the same."""
    return src.replace(b"\0", b" ")


def has_documentclass(src: bytes) -> bool:
    r"""Whether a source declares ``\documentclass``; use a comment-aware fallback."""
    parser = _parser()
    if parser is None:
        text = _without_comments(src.decode("utf-8", "ignore"))
        return any(m.group("declaration") for m in _DOCUMENTCLASS_RE.finditer(text))
    stack = [parser.parse(_without_nul(src)).root_node]
    while stack:
        node = stack.pop()
        if node.type == "class_include":
            if any(child.text == rb"\documentclass" for child in node.children):
                return True
        stack.extend(node.children)
    return False


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
    src = _without_nul(src)
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
            # compare bytes: a command name need not be UTF-8
            if cmd is not None and cmd.text in _GENERIC:
                for arg in node.children_by_field_name("arg"):
                    if arg.type == "curly_group":
                        found |= _names(arg.text.decode("utf-8", "ignore")[1:-1])
                        break
                else:
                    # bounded, so many unclosed ``[`` do not cost quadratic time
                    end = cmd.end_byte + _MAX_OPTIONS
                    if m := _OPTIONS_THEN_NAMES.match(src, cmd.end_byte, end):
                        found |= _names(m[1].decode("utf-8", "ignore"))
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
    src = _without_nul(text.encode("utf-8", "surrogatepass"))
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
