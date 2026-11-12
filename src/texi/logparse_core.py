"""Shared data types and helpers for the log parsers.

Everything here is import-cycle free: the per-tool parsers (`logparse_tex`,
`logparse_bib`, `logparse_index`, `logparse_latexmk`) import this module, and
`texi.logparse` re-exports the public names.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["error", "warning", "box", "info"]

# Longest line the regexes look at in full. Longer lines are cut for matching
# (TeX logs written with max_print_line=100000 can hold huge single lines).
MAX_MATCH = 4000


@dataclass
class Diagnostic:
    kind: Kind
    message: str
    file: str | None = None
    line: int | None = None
    context: list[str] = field(default_factory=list)
    source: str = "tex"  # tex | bibtex | biber | makeindex | xindy | latexmk
    end_line: int | None = None  # last line of a range, e.g. a box spanning lines 7--8
    guessed: bool = False  # line copied from an earlier diagnostic, see guess_lines
    code: str | None = None  # stable slug such as "undefined-control-sequence"
    column: int | None = None
    package: str | None = None  # package/class/module that raised it
    page: int | None = None  # output page, from "on page N"
    entry: str | None = None  # bibliography entry key
    help: list[str] = field(default_factory=list)  # TeX help text
    stack: list[str] = field(default_factory=list)  # <argument>, macro, traceback lines
    raw: list[str] = field(default_factory=list)  # the original log lines
    hint: str | None = None  # what to read and what is probably wrong, added by texi

    def key(self) -> tuple:
        """Identity for de-duplication: same text in the same place."""
        return (
            self.kind,
            self.source,
            self.file,
            self.line,
            re.sub(r"\d+", "#", self.message),
        )


@dataclass
class TexLogInfo:
    """Facts about one engine run, read from its .log."""

    engine: str | None = None  # pdftex | xetex | luatex | luahbtex | ...
    version: str | None = None
    banner: str | None = None
    format: str | None = None  # e.g. pdflatex
    shell_escape: str | None = None  # "restricted" | "enabled" | "disabled"
    file_line_error: bool = False
    parse_first_line: bool = False  # `%&-line parsing enabled.`
    extended_mode: bool = False
    input_name: str | None = None  # the `**file` line
    latex_date: str | None = None
    document_class: str | None = None
    output: str | None = None  # file named by "Output written on"
    pages: int | None = None
    bytes: int | None = None
    no_output: bool = False  # "No pages of output." or "no output PDF file produced"
    fatal: bool = False  # "Fatal error occurred" / "Emergency stop"
    files: list[str] = field(default_factory=list)  # every file the engine opened
    openout: list[str] = field(default_factory=list)  # \openout targets
    shell_commands: list[str] = field(default_factory=list)  # runpopen / \write18
    undefined_references: bool = False  # "There were undefined references."
    rerun: bool = False  # "Label(s) may have changed. Rerun ..."
    page_markers: int = 0  # number of `[N]` shipouts seen
    wrapped_at: int | None = None  # column the log was hard-wrapped at, if it was

    @property
    def ok(self) -> bool:
        """The engine produced output and did not die."""
        return self.output is not None and not self.fatal and not self.no_output


@dataclass
class ParseResult:
    """Diagnostics from one log file plus whatever facts the parser found."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    info: TexLogInfo | None = None


@dataclass
class RuleRun:
    """One execution of a latexmk rule."""

    rule: str
    number: int  # "Run number N of rule"
    command: str | None = None
    exit_code: int | None = None


@dataclass
class LatexmkInfo:
    """What latexmk's own output says happened."""

    version: str | None = None
    runs: list[RuleRun] = field(default_factory=list)
    applied_rules: list[str] = field(default_factory=list)
    status: Literal["success", "up-to-date", "failed", "unknown"] = "unknown"
    exit_code: int | None = None
    exit_meaning: str | None = None
    targets: list[str] = field(default_factory=list)  # "All targets (x.pdf) up-to-date"
    missing_files: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)  # e.g. index files it made
    max_runs_reached: bool = False
    failed_rules: list[str] = field(default_factory=list)
    undefined: list[str] = field(default_factory=list)  # refs/cites with line numbers
    summary: list[str] = field(default_factory=list)  # "Summary of warnings" lines
    rc_error: bool = False
    bad_options: bool = False
    forced_hint: bool = False  # latexmk suggested -f
    previous_error: bool = False  # "gave an error in previous invocation"

    def passes(self, rule: str | None = None) -> int:
        """How many times `rule` (or any rule) ran."""
        return sum(1 for r in self.runs if rule is None or r.rule == rule)

    def rules_run(self) -> list[str]:
        """Rule names in first-run order."""
        seen: dict[str, None] = {}
        for r in self.runs:
            seen.setdefault(r.rule)
        return list(seen)


@dataclass
class BuildResult:
    """Everything texi could learn from one build's logs."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    tex: TexLogInfo | None = None
    latexmk: LatexmkInfo | None = None

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "error"]

    @property
    def failed(self) -> bool:
        """True when any error was found or latexmk reports a failure."""
        return bool(self.errors) or (
            self.latexmk is not None and self.latexmk.status == "failed"
        )


def clean_path(path: str) -> str:
    return path.removeprefix("./")


def split_lines(text: str) -> list[str]:
    r"""Split on \\n only (not form feeds etc.), dropping \\r from CRLF logs."""
    lines = text.split("\n")
    return [ln.removesuffix("\r") for ln in lines]


def to_int(s: str | None) -> int | None:
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None
