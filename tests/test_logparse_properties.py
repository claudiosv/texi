"""Property tests of the log parsers: arbitrary and log-shaped text."""
# ruff: file-ignore[implicit-namespace-package]

import dataclasses
import time
from collections import Counter
from collections.abc import Callable

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from texi import logparse as lp
from texi.logparse_bib import biber_temp_sources
from texi.logparse_tex import _FileStack, _unwrap
from texi.logparse_texfmt import parse_texfmt

KINDS = {"error", "warning", "box", "info"}
# Never a line TeX, bibtex, makeindex, latexmk or tex-fmt would print is excluded:
# fragments make the interesting branches reachable, st.text() covers the rest.
TEX_LINES = [
    "! Boom.",
    "! Emergency stop.",
    "! Undefined control sequence.",
    "./main.tex:12: Oops.",
    "./main.tex:13: Emergency stop.",
    "l.5 \\foo",
    "<argument> \\bar",
    "?",
    "",
    "LaTeX Info: hi.",
    "Package x Info: hi on input line 3.",
    "(x)             more on input line 9.",
    "LaTeX Warning: Reference 'a' on page 2 undefined",
    "Package x Warning: Rerun",
    "Package x Error: bad.",
    " on input line 7.",
    "(x)",
    "pdfTeX warning (ext4): dup",
    "LuaTeX error (file a): no pages of output",
    "Overfull \\hbox (1.0pt too wide) in paragraph at lines 7--8",
    "Missing character: There is no x in font y!",
    "Runaway argument?",
    "{abc \\par",
    "(./main.tex (./a.sty) [1] )",
    ")",
    "kpathsea: Running mktexpk",
    "kpathsea: bad",
    "runsystem(ls)...disabled.",
    "Output written on a.pdf (2 pages, 100 bytes).",
    "No pages of output.",
    "xdvipdfmx:fatal: broken",
    "** WARNING ** odd",
    "[\\directlua]:1: attempt",
    "lua error: x",
    "luaotfload | db : error: not found",
    "Error 1 (driver return code) generating output;",
    "This is pdfTeX, Version 3.14 (TeX Live) (format=pdflatex)",
    "**main.tex",
    "\\openout1 = `a.aux'.",
]
BIB_LINES = [
    "This is BibTeX, Version 0.99d",
    "Warning--empty year in k",
    "--line 3 of file a.bib",
    "---line 3 of file a.bib",
    "I was expecting a `,'---line 4 of file a.bib",
    "I couldn't open style file x.bst",
    "x---while reading file a.aux",
    " : @article{",
    "(There were 2 error messages)",
    "You've used 3 entries,",
    "[1] a.pm:2> ERROR - Cannot find 'x.bib'!",
    "INFO - This is Biber 2.20",
    "WARN - BibTeX subsystem: /t/a.bib_1234.utf8, line 3, warning: x",
    "WARNINGS: 2",
]
INDEX_LINES = [
    "This is makeindex, version 2.17.",
    "Scanning input file a.idx...done (1 entries accepted, 0 rejected).",
    "Sorting entries...done",
    "Generating output file a.ind...done (3 lines written, 0 warnings).",
    "!! Input index error (file = a.idx, line = 2):",
    "## Warning (input = a.idx, line = 2; output = a.ind, line = 3):",
    "   -- Extra stuff",
    "Nothing written in a.ind.",
    "Input index file a.idx not found.",
    "ERROR: bad",
    "makeglossaries: x",
]
LATEXMK_LINES = [
    "Latexmk: This is Latexmk, John Collins, Version 4.86.",
    "Run number 1 of rule 'pdflatex'",
    "Running 'pdflatex  -recorder \"a.tex\"'",
    "------------",
    "Latexmk: applying rule 'bibtex a'...",
    "Latexmk: Missing input file: 'a.bbl'",
    "Latexmk: Could not find file 'a.tex'",
    "Latexmk: Maximum runs of pdflatex reached",
    "Latexmk: Failure in processing file 'a.tex':",
    "   pdflatex: failed",
    "Collected error summary (may duplicate other messages):",
    "  pdflatex: Command for 'pdflatex' gave return code 1",
    "  bibtex a: gave an error in previous invocation of latexmk.",
    "  xdvipdfmx: failed to create output file",
    "Changed files or newly in use/created:",
    "  a.bbl",
    "Latexmk: All targets (a.pdf) are up-to-date",
]
TEXFMT_LINES = [
    "ERROR: tex-fmt a.tex: Line 3 (2). Line cannot be wrapped. text",
    (
        "WARN: tex-fmt a.tex: Indent does not return to zero. "
        "Last non-indented line is line 1"
    ),
    "INFO: tex-fmt a.tex: Line 1 (1). Indent: actual = 2, visual = 0: x",
    "TRACE: tex-fmt a.tex: Line 1 (1). x",
]
ALL_LINES = TEX_LINES + BIB_LINES + INDEX_LINES + LATEXMK_LINES + TEXFMT_LINES


SURROGATES = [chr(c) for c in (0xD800, 0xDC80, 0xDCFF)]
STRESS = [
    " on input line " + "1" * 5000,
    " on page " + "2" * 5000,
    "l." + "3" * 5000 + " x",
    " at lines " + "4" * 5000,
    " " * 3000 + "b",
    "x)" * 1000,
    "(./a.tex " + ")" * 1000,
]


def logs(lines: list[str]) -> st.SearchStrategy[str]:
    # st.text() never produces lone surrogates, so they are added by hand; the
    # stress fragments hold numbers over the 4300-digit int() limit and long runs of
    # whitespace and parentheses.
    fragment = st.sampled_from(lines)
    odd = st.text(st.one_of(st.characters(), st.sampled_from(SURROGATES)), max_size=100)
    line = st.one_of(
        fragment,
        fragment,
        st.builds(str.__add__, fragment, odd),
        st.builds(str.__add__, fragment, st.sampled_from(STRESS)),
        st.sampled_from(STRESS),
        st.text(max_size=100),
        odd,
    )
    return st.lists(line, max_size=25).map("\n".join)


tex_logs = logs(TEX_LINES)
any_logs = logs(ALL_LINES)


@settings(deadline=None)
@given(
    text=tex_logs, info=st.booleans(), width=st.one_of(st.none(), st.integers(1, 90))
)
def test_parse_tex_never_raises(text: str, info: bool, width: int | None) -> None:
    result = lp.parse_tex(text, include_info=info, max_print_line=width)
    assert result.info is not None
    for d in result.diagnostics:
        assert d.kind in KINDS
        assert d.line is None or isinstance(d.line, int)
        assert info or d.kind != "info"


@settings(deadline=None)
@given(text=tex_logs, info=st.booleans())
def test_parse_tex_ignores_crlf(text: str, info: bool) -> None:
    text = text.replace("\r", "")
    assert lp.parse_tex(text.replace("\n", "\r\n"), include_info=info) == lp.parse_tex(
        text, include_info=info
    )


@settings(deadline=None)
@given(text=tex_logs)
def test_include_info_only_adds_info(text: str) -> None:
    plain = lp.parse_tex(text)
    full = lp.parse_tex(text, include_info=True)
    assert [d for d in full.diagnostics if d.kind != "info"] == plain.diagnostics
    assert full.info == plain.info


printable = st.characters(min_codepoint=32, max_codepoint=126)


@settings(deadline=None)
@given(
    lines=st.lists(st.text(printable, max_size=60), max_size=8),
    width=st.integers(2, 30),
)
@example(lines=["abcd", "", "ef"], width=2)
def test_unwrap_undoes_tex_wrapping(lines: list[str], width: int) -> None:
    # TeX breaks after exactly width characters, so an exact multiple leaves an
    # empty line behind.
    wrapped = [ln[i : i + width] for ln in lines for i in range(0, len(ln) + 1, width)]
    assert _unwrap(wrapped, width) == lines


@settings(deadline=None)
@given(
    lines=st.lists(st.sampled_from(TEX_LINES), min_size=1, max_size=12),
    width=st.integers(20, 79),
)
def test_a_wrapped_log_parses_like_the_unwrapped_one(
    lines: list[str], width: int
) -> None:
    wrapped = [ln[i : i + width] for ln in lines for i in range(0, len(ln) + 1, width)]
    got = lp.parse_tex("\n".join(wrapped), max_print_line=width)
    want = lp.parse_tex("\n".join(lines), max_print_line=10**9)
    assert got.info is not None
    got.info.wrapped_at = None
    assert got == want


@settings(deadline=None)
@given(
    text=any_logs,
    program=st.sampled_from([None, "bibtex", "biber", "makeindex", "xindy"]),
    code=st.one_of(st.none(), st.integers(-1, 300)),
    engine=st.booleans(),
)
def test_other_parsers_never_raise(
    text: str, program: str | None, code: int | None, engine: bool
) -> None:
    bib = lp.parse_bib(text, program)
    assert lp.parse_blg(text, program or "biber") == bib.diagnostics
    index = lp.parse_index(text, program)
    latexmk = lp.parse_latexmk(text, exit_code=code, engine_output=engine)
    fmt = parse_texfmt(text)
    for d in [*bib.diagnostics, *index.diagnostics, *latexmk.diagnostics, *fmt]:
        assert d.kind in KINDS
    if code not in {None, 0}:
        assert latexmk.info.status == "failed"
        assert any(d.kind == "error" for d in latexmk.diagnostics)


@settings(deadline=None)
@given(text=any_logs)
def test_other_parsers_ignore_crlf(text: str) -> None:
    text = text.replace("\r", "")
    crlf = text.replace("\n", "\r\n")
    for parse in (lp.parse_bib, lp.parse_index, lp.parse_latexmk, parse_texfmt):
        assert parse(crlf) == parse(text)


@settings(deadline=None)
@given(text=logs(TEXFMT_LINES), formatted=st.booleans())
def test_texfmt_keeps_every_line(text: str, formatted: bool) -> None:
    diags = parse_texfmt(text, formatted=formatted)
    kept = [ln.removesuffix("\r") for ln in text.split("\n")]
    assert [d.raw[0] for d in diags] == [ln for ln in kept if ln[:4000].strip()]


@settings(deadline=None)
@given(text=logs(LATEXMK_LINES))
def test_run_sequence_counts_every_run(text: str) -> None:
    info = lp.parse_latexmk(text).info
    summary = lp.run_sequence(info)
    counts = [int(part.rsplit(" x", 1)[1]) for part in summary.split(", ") if part]
    assert sum(counts) == info.passes() == len(info.runs)


@settings(max_examples=50, deadline=None)
@given(
    tex=st.one_of(st.none(), tex_logs),
    blg=st.one_of(st.none(), logs(BIB_LINES)),
    latexmk=st.one_of(st.none(), logs(LATEXMK_LINES)),
)
def test_latexmk_never_buries_a_tool_error(
    tex: str | None, blg: str | None, latexmk: str | None
) -> None:
    tools = lp.parse_build(tex_log=tex, blg=blg)
    result = lp.parse_build(tex_log=tex, blg=blg, latexmk_output=latexmk)
    n = len(tools.diagnostics)
    assert result.diagnostics[:n] == tools.diagnostics
    if tools.errors:
        assert result.diagnostics == tools.diagnostics


diagnostics = st.builds(
    lp.Diagnostic,
    kind=st.sampled_from(["error", "warning"]),
    message=st.sampled_from(["a", "page 1", "page 22", "b"]),
    file=st.sampled_from([None, "", "a.tex", "b.tex"]),
    line=st.one_of(st.none(), st.integers(0, 5)),
)


@given(diags=st.lists(diagnostics, max_size=12))
def test_dedupe_counts_and_keeps_first_seen_order(
    diags: list[lp.Diagnostic],
) -> None:
    out = lp.dedupe(diags)
    keys = [d.key() for d, _ in out]
    assert keys == list(dict.fromkeys(d.key() for d in diags))
    assert {d.key(): n for d, n in out} == Counter(d.key() for d in diags)
    for d, _ in out:
        assert d is next(x for x in diags if x.key() == d.key())


@given(diags=st.lists(diagnostics, max_size=12))
def test_guess_lines_copies_the_previous_line_of_the_same_file(
    diags: list[lp.Diagnostic],
) -> None:
    before = [dataclasses.replace(d) for d in diags]
    lp.guess_lines(diags)
    for i, (old, new) in enumerate(zip(before, diags, strict=True)):
        earlier = [d.line for d in before[:i] if d.file == old.file and d.line]
        if old.file and not old.line and earlier:
            assert new == dataclasses.replace(old, line=earlier[-1], guessed=True)
        else:
            assert new == old
    again = [dataclasses.replace(d) for d in diags]
    lp.guess_lines(diags)
    assert diags == again


@given(
    name=st.from_regex(r"[A-Za-z][A-Za-z0-9-]{0,20}", fullmatch=True),
    ext=st.sampled_from([".sty", ".cls", "", ".tex"]),
)
def test_missing_package(name: str, ext: str) -> None:
    diag = lp.Diagnostic("error", f"LaTeX Error: File `{name}{ext}' not found.")
    assert lp.missing_package(diag) == (name if ext in {".sty", ".cls"} else None)


@pytest.mark.parametrize("where", ["on page", "on input line"])
def test_huge_number_in_a_warning(where: str) -> None:
    lp.parse_tex(f"LaTeX Warning: x\n {where} {'1' * 5000}\n")


def test_lone_surrogate_in_a_log() -> None:
    lp.parse_tex("first line\n" + chr(0xDC80))


def test_include_info_keeps_error_lines() -> None:
    text = "! Boom.\n\nLaTeX Info: hi.\n\n! Emergency stop.\nl.5 x\n"
    full = lp.parse_tex(text, include_info=True).diagnostics
    assert [d for d in full if d.kind != "info"] == lp.parse_tex(text).diagnostics


def _cpu_seconds(f: Callable[[], object]) -> float:
    start = time.process_time()
    f()
    return time.process_time() - start


def test_long_whitespace_in_a_warning_is_fast() -> None:
    # linear: about a millisecond; now: several tenths of a second
    budget = 0.05
    text = "LaTeX Warning: x\n a" + " " * 20_000 + "b\n"
    assert _cpu_seconds(lambda: lp.parse_tex(text)) < budget


def test_many_parens_on_one_long_line_are_fast() -> None:
    # "a 1 MB single line is fine": compare with the same line with no file open,
    # which skips the copy
    parens = ("x" * 12 + ")") * 80_000
    base = _cpu_seconds(lambda: _FileStack().feed(parens))
    slow = _cpu_seconds(lambda: _FileStack().feed("(./a.tex " + parens))
    assert slow < 10 * base + 0.05


def test_biber_temp_sources_with_a_lone_surrogate() -> None:
    name = f"a{chr(0xDC80)}.bib"
    bcf = f"<bcf:datasource> {name} </bcf:datasource>"
    assert list(biber_temp_sources(bcf).values()) == [name]
