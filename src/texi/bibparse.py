"""Parse ``.bib`` files with the tree-sitter BibTeX grammar (latex-lsp).

The grammar ships with ``tree-sitter-language-pack``. Parsing is error
tolerant: malformed entries are reported in :attr:`BibFile.errors` and the
rest of the file is still returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser

__all__ = ["BibEntry", "BibError", "BibFile", "parse_bib", "parse_bib_file"]

# Entry types that carry no citation key and are not references.
_SPECIAL = {"string", "preamble", "comment"}


@dataclass(frozen=True)
class BibEntry:
    """One reference entry, for example ``@article{key, ...}``."""

    type: str
    """Lower-cased entry type without ``@``, for example ``"article"``."""
    key: str
    fields: dict[str, str]
    """Lower-cased field name to raw value text (braces/quotes stripped, ``#`` kept)."""
    line: int
    """1-based line of the ``@``."""


@dataclass(frozen=True)
class BibError:
    line: int
    column: int
    message: str


@dataclass
class BibFile:
    entries: list[BibEntry] = field(default_factory=list)
    strings: dict[str, str] = field(default_factory=dict)
    """``@string`` macros, name to raw value."""
    preambles: list[str] = field(default_factory=list)
    errors: list[BibError] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    """Source lines, so callers can quote the offending line."""

    @property
    def keys(self) -> list[str]:
        return [e.key for e in self.entries]

    def duplicate_keys(self) -> list[str]:
        """Return citation keys defined more than once, in first-seen order."""
        seen: set[str] = set()
        dups: dict[str, None] = {}
        for k in self.keys:
            if k in seen:
                dups[k] = None
            seen.add(k)
        return list(dups)


@cache
def _parser() -> Parser:
    return get_parser("bibtex")


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _strip(raw: str) -> str:
    """Remove one level of outer braces or quotes from a single token."""
    if len(raw) >= 2 and (
        (raw[0] == "{" and raw[-1] == "}") or (raw[0] == '"' and raw[-1] == '"')
    ):
        return raw[1:-1]
    return raw


def _value(node: Node | None) -> str:
    """Join the ``#``-concatenated tokens of a ``value`` node."""
    if node is None:
        return ""
    parts = [_strip(_text(t).strip()) for t in node.children if t.type == "token"]
    return " # ".join(parts)


def _key(node: Node | None) -> str:
    return _text(node).strip("{(, \t\r\n") if node is not None else ""


def _collect_errors(root: Node, out: list[BibError]) -> None:
    stack = [root]
    while stack:
        n = stack.pop()
        if n.is_missing:
            out.append(
                BibError(
                    n.start_point.row + 1, n.start_point.column + 1, f"missing {n.type}"
                )
            )
        elif n.type in ("ERROR", "junk") or (n.is_error):
            out.append(
                BibError(
                    n.start_point.row + 1,
                    n.start_point.column + 1,
                    f"unexpected text: {_text(n).strip()[:40]!r}",
                )
            )
        elif n.has_error:
            stack.extend(n.children)
    out.sort(key=lambda e: (e.line, e.column))


def parse_bib(source: str | bytes) -> BibFile:
    """Parse BibTeX source text."""
    data = source.encode() if isinstance(source, str) else source
    root = _parser().parse(data).root_node
    text = data.decode("utf-8", errors="replace")
    # Split on "\n" only, as the grammar counts rows: splitlines() also breaks at
    # form feeds, U+2028 and the like, which would shift BibEntry.line.
    lines = text.removesuffix("\n").split("\n") if text else []
    result = BibFile(lines=[line.removesuffix("\r") for line in lines])
    for node in root.children:
        if node.type == "entry":
            ty = _text(node.child_by_field_name("ty") or node).lstrip("@").lower()
            fields: dict[str, str] = {}
            for f in node.children_by_field_name("field"):
                name = f.child_by_field_name("name")
                if name is not None:
                    fields[_text(name).lower()] = _value(f.child_by_field_name("value"))
            result.entries.append(
                BibEntry(
                    ty,
                    _key(node.child_by_field_name("key")),
                    fields,
                    node.start_point.row + 1,
                )
            )
        elif node.type == "string":
            name = node.child_by_field_name("name")
            if name is not None:
                result.strings[_text(name)] = _value(node.child_by_field_name("value"))
        elif node.type == "preamble":
            result.preambles.append(_value(node.child_by_field_name("value")))
    if root.has_error:
        _collect_errors(root, result.errors)
    return result


def parse_bib_file(path: Path) -> BibFile:
    """Parse a ``.bib`` file from disk."""
    return parse_bib(path.read_bytes())
