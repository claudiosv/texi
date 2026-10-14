from texi.logparse import dedupe, guess_lines, missing_package, parse_blg, parse_tex_log

LOG = r"""This is pdfTeX
(./main.tex (/usr/local/texlive/2026basic/texmf-dist/tex/latex/base/article.cls
Document Class: article
) (./sections/intro.tex
LaTeX Warning: Reference `fig:x' on page 1 undefined on input line 12.

Overfull \hbox (3.2pt too wide) in paragraph at lines 20--21
Package hyperref Warning: Token not allowed in a PDF string (Unicode):
(hyperref)                removing `math shift' on input line 30.

./sections/intro.tex:33: Undefined control sequence.
l.33 \foo
         bar
? 
) (/x/preamble/common.tex
! LaTeX Error: Command \Bbbk already defined.

See the LaTeX manual or LaTeX Companion for explanation.
l.3 \usepackage{amssymb}

./main.tex:5: LaTeX Error: File `nope.sty' not found.

Type X to quit
l.5 \usepackage{nope}
)
"""


def test_errors_with_file_and_line():
    errs = [d for d in parse_tex_log(LOG) if d.kind == "error"]
    assert [(e.file, e.line) for e in errs] == [
        ("sections/intro.tex", 33),
        ("/x/preamble/common.tex", 3),
        ("main.tex", 5),
    ]
    assert errs[0].message == "Undefined control sequence."
    assert any("bar" in c for c in errs[0].context)


def test_warnings_and_boxes():
    diags = parse_tex_log(LOG)
    w = [d for d in diags if d.kind == "warning"]
    assert w[0].line == 12 and w[0].file == "sections/intro.tex"
    assert "Reference `fig:x' on page 1 undefined" in w[0].message
    assert "removing `math shift'" in w[1].message  # (hyperref) continuation joined
    assert [d.kind for d in diags if d.kind == "box"] == ["box"]


def test_missing_package():
    errs = [d for d in parse_tex_log(LOG) if d.kind == "error"]
    assert [missing_package(e) for e in errs] == [None, None, "nope"]


def test_dedupe_counts_repeats():
    d = parse_tex_log(LOG + LOG)
    assert len(dedupe(d)) == len(parse_tex_log(LOG))
    assert all(n == 2 for _, n in dedupe(d))


def test_blg():
    d = parse_blg(
        "[1] Biber.pm:1> WARN - The file 'x.bcf' does not contain any citations!\nERROR - bad\n"
    )
    assert [x.kind for x in d] == ["warning", "error"]


def test_context_strips_carriage_return_marker():
    log = "! Undefined control sequence.\nl.3 Hello \\foo^^M\n   world\n"
    (d,) = parse_tex_log(log)
    assert "^^M" not in "".join(d.context) and d.line == 3


def test_warning_includes_indented_continuation():
    log = "LaTeX Warning: Unused global option(s):\n    [natbib=false,balance=false].\n\nnext\n"
    (d,) = parse_tex_log(log)
    assert d.message.endswith("[natbib=false,balance=false]")


def test_guess_lines_uses_previous_line_in_same_file():
    log = (
        "(./main.tex\nLaTeX Warning: Reference `a' on page 1 undefined on input line 8.\n\n"
        "Package foo Warning: no line here.\n\n)\n"
    )
    diags = parse_tex_log(log)
    guess_lines(diags)
    assert [(d.line, d.guessed) for d in diags] == [(8, False), (8, True)]


def test_box_line_range():
    log = "(./a.tex\nOverfull \\hbox (3pt too wide) in paragraph at lines 7--8\n"
    log += "Overfull \\hbox (1pt too wide) detected at line 5\n)\n"
    ranged, single = parse_tex_log(log)
    assert (ranged.line, ranged.end_line) == (7, 8)
    assert (single.line, single.end_line) == (5, None)
