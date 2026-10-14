"""Turn TeX / BibTeX / biber logs into a short list of diagnostics.

The parser expects logs written with a huge max_print_line (texi sets it), so
TeX never wraps lines, and works best with -file-line-error.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["error", "warning", "box"]

_FILE_EXTS = (
    ".tex",
    ".sty",
    ".cls",
    ".bbx",
    ".cbx",
    ".def",
    ".cfg",
    ".ltx",
    ".fd",
    ".clo",
    ".lbx",
    ".bib",
)
# `./main.tex:12: message` (-file-line-error)
_FLE = re.compile(
    r"^((?:\./|/|\w)[^\s:]*?(?:"
    + "|".join(map(re.escape, _FILE_EXTS))
    + r")):(\d+): (.*)$"
)
_BANG = re.compile(r"^! (.*)$")
_CONTEXT = re.compile(r"^l\.(\d+)\s?(.*)$")
_WARNING = re.compile(
    r"^((?:LaTeX(?: Font)?|Package \S+|Class \S+|pdfTeX|LuaTeX|Module \S+)) [Ww]arning: (.*)$"
)
_CONT = re.compile(r"^\((?:[^)\s]+)\)\s+(.*)$")  # `(pkg)   more text`
_BOX = re.compile(r"^((?:Overfull|Underfull) \\[hv]box .*)$")
_MISSING_CHAR = re.compile(r"^Missing character: (.*)$")
_ON_LINE = re.compile(r"on input line (\d+)")
_BOX_LINES = re.compile(r"at lines? (\d+)(?:--(\d+))?")
_OPEN = re.compile(r"\(([^\s()]*)|\)")


@dataclass
class Diagnostic:
    kind: Kind
    message: str
    file: str | None = None
    line: int | None = None
    context: list[str] = field(default_factory=list)
    source: str = "tex"  # tex | bibtex | biber | latexmk
    end_line: int | None = None  # last line of a range, e.g. a box spanning lines 7--8
    guessed: bool = False  # line copied from an earlier diagnostic, see guess_lines

    def key(self) -> tuple:
        """Identity for de-duplication: same text in the same place."""
        return (
            self.kind,
            self.source,
            self.file,
            self.line,
            re.sub(r"\d+", "#", self.message),
        )


def _clean_path(path: str) -> str:
    return path[2:] if path.startswith("./") else path


class _FileStack:
    """Best-effort tracking of which file TeX is reading, from `(path` / `)` in the log.

    Only file-looking `(path` opens are pushed. A `)` only closes a file when it
    sits where TeX prints one (line start, after `)`, `]`, `>`, a space, or right
    after a file name); parens inside message text are ignored.
    """

    def __init__(self) -> None:
        self.stack: list[str] = []

    def feed(self, line: str) -> None:
        opened_end = -1
        for m in _OPEN.finditer(line):
            if m.group(1) is not None:
                if m.group(1).startswith(("/", "./", "../")) or m.group(1).endswith(
                    _FILE_EXTS
                ):
                    self.stack.append(m.group(1))
                    opened_end = m.end()
            elif self.stack:
                before = line[m.start() - 1] if m.start() else "\n"
                if before in "\n)]> " or m.start() == opened_end:
                    self.stack.pop()

    @property
    def current(self) -> str | None:
        return self.stack[-1] if self.stack else None

    @property
    def current_tex(self) -> str | None:
        """Innermost .tex file (what the user wrote), else the innermost file."""
        return next(
            (f for f in reversed(self.stack) if f.endswith(".tex")), self.current
        )


def parse_tex_log(text: str) -> list[Diagnostic]:
    """Errors, warnings and box warnings from a TeX engine log."""
    out: list[Diagnostic] = []
    lines = text.splitlines()
    files = _FileStack()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1

        if m := _FLE.match(line):
            diag = Diagnostic(
                "error", m.group(3), _clean_path(m.group(1)), int(m.group(2))
            )
            i = _read_error_context(lines, i, diag)
            out.append(diag)
            continue
        if m := _BANG.match(line):
            diag = Diagnostic(
                "error",
                m.group(1).strip(),
                files.current and _clean_path(files.current),
            )
            i = _read_error_context(lines, i, diag)
            out.append(diag)
            continue
        if m := _WARNING.match(line):
            msg = m.group(2)
            cont_start = i
            # A warning runs until the next blank line: `(pkg)   text` or indented lines
            while i < len(lines) and lines[i].strip() and i - cont_start < 8:
                c = _CONT.match(lines[i])
                msg += " " + (c.group(1) if c else lines[i].strip())
                i += 1
            ln = _ON_LINE.search(msg)
            msg = _ON_LINE.sub("", msg).rstrip(" .") or msg
            where = (
                files.current if ln else files.current_tex
            )  # a line number refers to the innermost file
            out.append(
                Diagnostic(
                    "warning",
                    f"{m.group(1)}: {msg}",
                    where and _clean_path(where),
                    int(ln.group(1)) if ln else None,
                )
            )
            continue
        if m := _BOX.match(line):
            ln = _BOX_LINES.search(line)
            out.append(
                Diagnostic(
                    "box",
                    m.group(1),
                    files.current and _clean_path(files.current),
                    int(ln.group(1)) if ln else None,
                    end_line=int(ln.group(2)) if ln and ln.group(2) else None,
                )
            )
            continue
        if m := _MISSING_CHAR.match(line):
            out.append(
                Diagnostic(
                    "warning",
                    f"Missing character: {m.group(1)}",
                    files.current and _clean_path(files.current),
                )
            )
            continue
        if not _CONT.match(line):  # `(pkg)` continuation lines aren't file opens
            files.feed(line)
    return out


def _read_error_context(lines: list[str], i: int, diag: Diagnostic) -> int:
    """Consume the help text and `l.NN ...` context TeX prints after an error.

    Fills in the line number if it wasn't known. Stops at the context line or
    at the start of the next diagnostic.
    """
    for _ in range(12):
        if i >= len(lines):
            break
        nxt = lines[i]
        if (
            _BANG.match(nxt)
            or _FLE.match(nxt)
            or _WARNING.match(nxt)
            or _BOX.match(nxt)
        ):
            break
        if m := _CONTEXT.match(nxt):
            diag.line = diag.line or int(m.group(1))
            diag.context.append(nxt.replace("^^M", "").strip())
            i += 1
            if (
                i < len(lines)
                and lines[i].strip()
                and not lines[i].startswith(("?", "!"))
            ):
                diag.context.append(
                    lines[i].replace("^^M", "").strip()
                )  # the rest of the source line
                i += 1
            break
        i += 1
    return i


_BLG = re.compile(r"^(?:\[\d+\] \S+> )?(ERROR|WARN) - (.*)$")
_BIBTEX_WARN = re.compile(r"^Warning--(.*)$")
_BIBTEX_ERR = re.compile(r"^(I couldn't open .*|I found no .*|.*---.*)$")


def parse_blg(text: str, program: str = "biber") -> list[Diagnostic]:
    """Warnings/errors from a biber or bibtex .blg log."""
    out: list[Diagnostic] = []
    for line in text.splitlines():
        if m := _BLG.match(line):
            kind: Kind = "error" if m.group(1) == "ERROR" else "warning"
            out.append(Diagnostic(kind, m.group(2).strip(), source=program))
        elif program == "bibtex" and (m := _BIBTEX_WARN.match(line)):
            out.append(Diagnostic("warning", m.group(1).strip(), source="bibtex"))
        elif program == "bibtex" and (m := _BIBTEX_ERR.match(line)) and "---" in line:
            out.append(Diagnostic("error", line.strip(), source="bibtex"))
    return out


_MISSING_FILE = re.compile(r"File [`']([^'`]+?)(\.sty|\.cls)?' not found")


def missing_package(diag: Diagnostic) -> str | None:
    """Name of a missing .sty/.cls from a `File 'x.sty' not found` error, if that's what this is."""
    m = _MISSING_FILE.search(diag.message)
    return m.group(1) if m and m.group(2) else None


def dedupe(diags: list[Diagnostic]) -> list[tuple[Diagnostic, int]]:
    """Collapse repeated diagnostics, keeping first-seen order and a count."""
    seen: dict[tuple, int] = {}
    first: dict[tuple, Diagnostic] = {}
    for d in diags:
        k = d.key()
        seen[k] = seen.get(k, 0) + 1
        first.setdefault(k, d)
    return [(first[k], n) for k, n in seen.items()]


def guess_lines(diags: list[Diagnostic]) -> None:
    """Give line-less diagnostics the last line seen earlier in the same file.

    TeX prints no line for many warnings. The log is in processing order, so the
    previous known line in that file is a lower bound for where it happened.
    """
    last: dict[str, int] = {}
    for d in diags:
        if not d.file:
            continue
        if d.line:
            last[d.file] = d.line
        elif d.file in last:
            d.line, d.guessed = last[d.file], True
