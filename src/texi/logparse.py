"""Turn TeX / BibTeX / biber / makeindex / latexmk output into diagnostics.

This module is the stable facade; the work happens in

- `texi.logparse_core`: `Diagnostic` and the result dataclasses
- `texi.logparse_tex`: pdfTeX / XeTeX / LuaTeX logs and engine stdout
- `texi.logparse_bib`: BibTeX and Biber `.blg`
- `texi.logparse_index`: makeindex / xindy / makeglossaries `.ilg` / `.glg`
- `texi.logparse_latexmk`: latexmk's own output and the run sequence

TeX logs parse best when written with a huge max_print_line (texi sets it) and
-file-line-error, but classic 79-column wrapped logs, CRLF files and truncated
files are handled too. No parser raises on bad input.
"""

import re
from collections.abc import Sequence

from texi.logparse_bib import BibLogInfo, BibResult, parse_bib, parse_blg
from texi.logparse_core import (
    BuildResult,
    Diagnostic,
    Kind,
    LatexmkInfo,
    ParseResult,
    RuleRun,
    TexLogInfo,
)
from texi.logparse_index import IndexLogInfo, IndexResult, parse_index
from texi.logparse_latexmk import (
    EXIT_CODES,
    LatexmkResult,
    parse_latexmk,
    run_sequence,
)
from texi.logparse_tex import parse_tex, parse_tex_log

_MISSING_FILE = re.compile(r"File [`']([^'`]+?)(\.sty|\.cls)?' not found")


def missing_package(diag: Diagnostic) -> str | None:
    """Name of a missing .sty/.cls from a `File 'x.sty' not found` error, if that's what this is."""
    m = _MISSING_FILE.search(diag.message[:4000])
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


def _as_list(x: str | Sequence[str] | None) -> list[str]:
    if x is None:
        return []
    return [x] if isinstance(x, str) else list(x)


def parse_build(
    *,
    tex_log: str | Sequence[str] | None = None,
    blg: str | Sequence[str] | None = None,
    blg_program: str | None = None,
    ilg: str | Sequence[str] | None = None,
    latexmk_output: str | None = None,
    latexmk_exit_code: int | None = None,
    include_info: bool = False,
) -> BuildResult:
    """Combine the logs of one build into diagnostics plus run facts.

    Pass whichever texts you have (`tex_log`, `blg` and `ilg` may each be one
    text or several, e.g. one per main file; `.glg` goes through `ilg`).
    Diagnostics come out in this order: TeX, bibliography, index, latexmk.

    latexmk's own diagnostics (missing source file, too many passes, a rule that
    failed, rc-file errors) are only added when the tool logs hold no error, so
    a real TeX error is never buried under "latexmk failed". Without a `tex_log`
    the engine output embedded in `latexmk_output` is parsed instead.
    """
    result = BuildResult()
    tex_texts = _as_list(tex_log)
    for t in tex_texts:
        r = parse_tex(t, include_info=include_info)
        result.diagnostics += r.diagnostics
        result.tex = r.info
    for t in _as_list(blg):
        result.diagnostics += parse_bib(t, blg_program).diagnostics
    for t in _as_list(ilg):
        result.diagnostics += parse_index(t).diagnostics
    if latexmk_output is not None:
        lm = parse_latexmk(
            latexmk_output,
            exit_code=latexmk_exit_code,
            engine_output=not tex_texts,
        )
        result.latexmk = lm.info
        if result.tex is None and lm.tex_runs:
            result.tex = lm.tex_runs[-1]
        if not result.errors:
            result.diagnostics += lm.diagnostics
    return result


__all__ = [
    "EXIT_CODES",
    "BibLogInfo",
    "BibResult",
    "BuildResult",
    "Diagnostic",
    "IndexLogInfo",
    "IndexResult",
    "Kind",
    "LatexmkInfo",
    "LatexmkResult",
    "ParseResult",
    "RuleRun",
    "TexLogInfo",
    "dedupe",
    "guess_lines",
    "missing_package",
    "parse_bib",
    "parse_blg",
    "parse_build",
    "parse_index",
    "parse_latexmk",
    "parse_tex",
    "parse_tex_log",
    "run_sequence",
]
