"""Parser for tex-fmt's log output (it writes to stderr).

Every line has the form ``LEVEL: tex-fmt FILE: MESSAGE``. Messages about one
source line start with ``Line N (M).``: M, in parentheses, is the line in the file
tex-fmt read (the one to link to); N is the line in the formatted output, which
differs once tex-fmt has split earlier lines. The rest of the line is the message, then the
source text it is about. Anything else is kept as an ``info`` diagnostic with
code ``output`` so nothing is lost.
"""

import re

from texi.logparse_core import MAX_MATCH, Diagnostic, Kind, split_lines

SOURCE = "tex-fmt"

_LINE = re.compile(r"^(ERROR|WARN|INFO|TRACE): tex-fmt (.+?): (.*)$")
_AT_LINE = re.compile(r"^Line (\d+) \((\d+)\)\.\s*(.*)$")
_INDENT = re.compile(r"^(Indent: actual = \d+, visual = \d+):\s*(.*)$")
_SENTENCE = re.compile(r"^(.*?\.)(?:\s+(.*))?$")
_LINE_NO = re.compile(r"\bline (\d+)\s*$")

_KINDS: dict[str, Kind] = {
    "ERROR": "error",
    "WARN": "warning",
    "INFO": "info",
    "TRACE": "info",
}

_HINTS = {
    "line-cannot-be-wrapped": "tex-fmt found no place to break this line; shorten it or add a space where it may wrap",
    "indent-does-not-return-to-zero": "a \\begin without its \\end, or a stray brace, leaves the indent open",
    "negative-indents": "an \\end or } without its opening \\begin or {",
    "indent-is-negative": "an \\end or } without its opening \\begin or {",
    "could-not-open-file": "check the path",
}


def _slug(message: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", message.lower())[:8])


def parse_texfmt(text: str, *, formatted: bool = False) -> list[Diagnostic]:
    """Diagnostics from tex-fmt's output, in order. Never raises.

    ``Diagnostic.line`` is the line in the original file. With ``formatted`` it is the
    line in the formatted output instead, which is what the file holds once tex-fmt
    has rewritten it (not with ``--check``).
    """
    diags: list[Diagnostic] = []
    for raw in split_lines(text):
        line = raw[:MAX_MATCH].rstrip()
        if not line.strip():
            continue
        m = _LINE.match(line)
        if not m:
            diags.append(
                Diagnostic(
                    "info", line.strip(), source=SOURCE, code="output", raw=[raw]
                )
            )
            continue
        level, file, rest = m.groups()
        rest = rest.strip()
        number: int | None = None
        context: list[str] = []
        if at := _AT_LINE.match(rest):
            number = int(at.group(1 if formatted else 2))
            rest = at.group(3)
            if ind := _INDENT.match(rest):
                message, content = ind.group(1), ind.group(2)
            elif sent := _SENTENCE.match(rest):
                message, content = sent.group(1), sent.group(2) or ""
            else:
                message, content = rest, ""
            if content:
                context = [content]
        else:
            message = rest
            if tail := _LINE_NO.search(rest):
                number = int(tail.group(1))  # "Last non-indented line is line 1"
        diag = Diagnostic(
            _KINDS[level],
            message,
            file=file,
            line=number,
            context=context,
            source=SOURCE,
            code="trace" if level == "TRACE" else _slug(message),
            raw=[raw],
        )
        if level in {"ERROR", "WARN"}:
            diag.hint = _HINTS.get(diag.code or "")
        diags.append(diag)
    return diags
