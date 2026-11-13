r"""Find the unclosed argument behind TeX's "File ended while scanning use of \\x".

TeX reports the *end* of the run-away argument, which is the end of the file. The log
also prints the start of the argument (``Runaway argument? {2026-03-28 \\par ...``).
This module finds where that argument starts in the sources: among the braces the
tree-sitter parser could not close, else the first use of the macro with that start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from texi import texscan

#: Error messages that come with a "Runaway ..." line, and the macro they name.
_MACRO = re.compile(
    r"(?:scanning use of|Paragraph ended before) \\*(?:\\)?\s*(\\*)([A-Za-z@]+)"
)
_BLANK = re.compile(r"\n[ \t]*\n")


@dataclass
class Located:
    """Where a run-away argument starts in a source file."""

    file: str
    #: Line of the opening ``{`` (1-based).
    line: int
    macro: str
    #: Line of the first blank line inside the argument, if any.
    blank_line: int | None
    #: The first characters of the argument as TeX printed them.
    snippet: str

    def hint(self) -> str:
        name = self.file.rsplit("/", 1)[-1]
        text = (
            f"the argument of \\{self.macro} that starts at {name}:{self.line} "
            f"({self.snippet}) is never closed: TeX read to the end of the file"
        )
        if self.blank_line:
            text += (
                f". The first blank line inside it is at line {self.blank_line}; "
                "the missing } may be just before it"
            )
        return text


def macro_of(message: str) -> str | None:
    """The macro named in the error message, without backslashes."""
    m = _MACRO.search(message)
    return m.group(2) if m else None


def runaway_text(stack: list[str]) -> str | None:
    """The text after ``Runaway argument:`` in a diagnostic's stack lines."""
    for line in stack:
        if line.startswith("Runaway ") and ":" in line:
            return line.split(":", 1)[1].strip()
    return None


def locate(runaway: str, macro: str | None, sources: dict[str, str]) -> Located | None:
    """Find where the run-away argument ``runaway`` starts.

    Parameters
    ----------
    runaway
        The text TeX printed after ``Runaway argument?``.
    macro
        The macro whose argument was being read (None if unknown).
    sources
        ``path -> text`` of the project's source files.

    Returns
    -------
    Located | None
        The best match, or None if no source has the argument.
    """
    token = runaway.split(maxsplit=1)[0] if runaway.split() else ""
    if not token.startswith("{") or len(token) < 2:
        return None
    wanted = re.escape(token[1:])
    starts = re.compile(r"\{\s*" + wanted)
    before = (
        re.compile(r"\\" + re.escape(macro) + r"\s*(?:\[[^\]]*\]\s*)*\Z")
        if macro
        else None
    )

    def fits(text: str, brace: int) -> bool:
        return bool(starts.match(text, brace)) and (
            before is None or bool(before.search(text, max(0, brace - 200), brace))
        )

    # The braces the parser could not close; skips comments and closed uses.
    for path, text in sources.items():
        for brace in texscan.unclosed_braces(text) or []:
            if fits(text, brace):
                return _located(path, text, brace, macro, token)
    # No grammar, or it lost track: the first use of the macro with this start.
    if macro:
        pat = re.compile(
            r"\\" + re.escape(macro) + r"\s*(?:\[[^\]]*\]\s*)*\{(?=\s*" + wanted + ")"
        )
    else:
        pat = re.compile(r"\{(?=\s*" + wanted + ")")
    for path, text in sources.items():
        if m := pat.search(text):
            return _located(path, text, m.end() - 1, macro, token)
    return None


def _located(
    path: str, text: str, brace: int, macro: str | None, token: str
) -> Located:
    line = text.count("\n", 0, brace) + 1
    blank = _BLANK.search(text, brace)
    blank_line = text.count("\n", 0, blank.start()) + 2 if blank else None
    return Located(path, line, macro or "?", blank_line, token)
