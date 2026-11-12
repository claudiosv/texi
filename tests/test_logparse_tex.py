import pytest
from logfixtures import read

from texi.logparse import dedupe, missing_package, parse_build, parse_tex, parse_tex_log


def errs(name: str):
    return [d for d in parse_tex_log(read(name)) if d.kind == "error"]


def warns(name: str):
    return [d for d in parse_tex_log(read(name)) if d.kind == "warning"]


def boxes(name: str):
    return [d for d in parse_tex_log(read(name)) if d.kind == "box"]


# -- engine banners and run facts --------------------------------------------


@pytest.mark.parametrize(
    ("name", "engine", "fmt"),
    [
        ("pdflatex_ok.log", "pdftex", "pdflatex"),
        ("xelatex_ok.log", "xetex", "xelatex"),
        ("lualatex_ok.log", "luahbtex", "lualatex"),
    ],
)
def test_engine_banner_and_clean_run(name, engine, fmt):
    r = parse_tex(read(name))
    assert r.diagnostics == []
    info = r.info
    assert info is not None
    assert (info.engine, info.format) == (engine, fmt)
    assert info.version
    assert info.output
    assert info.output.endswith(".pdf")
    assert info.ok
    assert not info.fatal
    assert not info.no_output
    assert info.latex_date == "2026-11-01"
    assert info.document_class == "article"
    assert info.shell_escape == "restricted"
    assert info.file_line_error


def test_pdflatex_ok_output_and_files():
    info = parse_tex(read("pdflatex_ok.log")).info
    assert info is not None
    assert info.pages == 3
    assert info.bytes
    assert info.files[0] == "./ok.tex"
    assert any(f.endswith("article.cls") for f in info.files)
    assert info.openout == ["pdflatex_ok.aux"]
    assert info.page_markers == 3
    assert info.extended_mode
    assert info.parse_first_line
    assert info.input_name == "ok.tex"


def test_nofle_log_is_flagged():
    info = parse_tex(read("pdflatex_undefcs_nofle.log")).info
    assert info is not None
    assert not info.file_line_error


# -- errors ------------------------------------------------------------------


def test_undefined_control_sequence_fle():
    es = errs("pdflatex_undefcs.log")
    assert [(e.file, e.line, e.code) for e in es] == [
        ("undefcs.tex", 3, "undefined-control-sequence"),
        ("undefcs.tex", 4, "missing-dollar"),
        ("undefcs.tex", 5, "missing-token"),
        ("undefcs.tex", 5, "missing-dollar"),
        ("undefcs.tex", 5, "missing-token"),
    ]
    first = es[0]
    assert first.message == "Undefined control sequence."
    assert first.context == ["l.3 Hello \\foo", "world."]
    assert any("misspelled" in h for h in first.help)
    assert es[1].stack == ["<inserted text> $"] or any(
        "inserted text" in s for s in es[1].stack
    )


def test_undefined_control_sequence_without_file_line_error():
    (e,) = errs("pdflatex_undefcs_nofle.log")
    assert (e.file, e.line) == ("undefcs_nofle.tex", 3)
    assert e.code == "undefined-control-sequence"


def test_missing_file_and_emergency_stop():
    es = errs("pdflatex_missingfile.log")
    assert [e.code for e in es] == ["file-not-found", "emergency-stop", "fatal-error"]
    assert es[0].message == "LaTeX Error: File `nope.tex' not found."
    assert es[1].context[0].startswith("l.3 \\input{nope}")
    assert es[1].stack  # <read *>
    assert missing_package(es[0]) is None
    info = parse_tex(read("pdflatex_missingfile.log")).info
    assert info is not None
    assert info.fatal
    assert info.no_output
    assert not info.ok


@pytest.mark.parametrize(
    "name", ["pdflatex_missingpkg.log", "pdflatex_missingpkg_nofle.log"]
)
def test_missing_package(name):
    es = errs(name)
    assert [missing_package(e) for e in es] == ["nonexistentpkg", None, None]
    # the line comes from the follow-up Emergency stop when TeX printed none
    assert es[0].line == 3
    assert es[0].file


def test_emergency_stop_fle_and_plain():
    fle = errs("pdflatex_emergency.log")
    plain = errs("pdflatex_emergency_nofle.log")
    assert [e.code for e in fle] == ["file-not-found", "emergency-stop", "fatal-error"]
    assert [e.code for e in plain] == [
        "file-not-found",
        "emergency-stop",
        "fatal-error",
    ]
    assert fle[2].message == "==> Fatal error occurred, no output PDF file produced!"
    assert plain[2].message == fle[2].message
    assert all(e.line == 3 for e in fle[:2])
    assert all(e.line == 3 for e in plain[:2])


def test_runaway_argument_and_file_ended():
    es = errs("pdflatex_runaway.log")
    assert [e.code for e in es] == [
        "paragraph-ended",
        "unbalanced",
        "file-ended",
        "emergency-stop",
        "fatal-error",
    ]
    assert es[0].stack[0] == "Runaway argument: {x"
    assert es[0].line == 5
    assert es[0].file == "runaway.tex"
    assert es[2].stack[0].startswith("Runaway argument: {abc")
    assert es[2].message.startswith("File ended while scanning use of \\textbf")
    info = parse_tex(read("pdflatex_runaway.log")).info
    assert info is not None
    assert info.fatal
    assert info.no_output


def test_file_ended_only():
    es = errs("pdflatex_fileended.log")
    assert [e.code for e in es] == ["file-ended", "emergency-stop", "fatal-error"]
    assert "unterminated" in es[0].stack[0]


def test_math_errors():
    es = errs("pdflatex_math.log")
    assert [(e.code, e.line) for e in es[:5]] == [
        ("double-script", 3),
        ("double-script", 3),
        ("missing-dollar", 5),
        ("missing-dollar", 6),
        ("missing-token", 7),
    ]
    assert [e.message for e in es[:2]] == ["Double superscript.", "Double subscript."]
    assert es[-3].code == "file-ended"


def test_package_generic_class_errors_and_warnings():
    es = errs("pdflatex_pkgerror.log")
    assert [(e.code, e.line, e.package) for e in es] == [
        ("package-error", 4, "mypkg"),
        (None, 5, None),
        ("class-error", 6, "myclass"),
    ]
    assert es[0].message == "Package mypkg Error: Something went wrong."
    assert "Try this" in es[0].help
    assert es[1].message == "Gen error."
    ws = warns("pdflatex_pkgerror.log")
    assert [(w.message, w.line, w.package) for w in ws] == [
        ("Package mypkg Warning: A warning with two lines", 7, "mypkg"),
        ("Package mypkg Warning: No line warning", None, "mypkg"),
        ("Class myclass Warning: Class warning", 10, "myclass"),
        ("LaTeX Warning: Custom LaTeX warning", 11, None),
    ]


def test_babel_error_from_package_file():
    (e,) = errs("pdflatex_babel.log")
    assert e.file
    assert e.file.endswith("babel.sty")
    assert e.line == 4481
    assert e.message.startswith("Package babel Error: Unknown option 'klingon'.")
    assert "Suggested actions" in e.message  # (babel) continuation lines joined
    assert e.package == "babel"


def test_unicode_error_wrapped_message():
    (e,) = errs("pdflatex_unicode.log")
    assert e.code == "unicode-char"
    assert e.message.endswith("not set up for use with LaTeX.")
    assert e.line == 4


# -- warnings ----------------------------------------------------------------


def test_undefined_refs_and_rerun():
    ws = warns("pdflatex_refs.log")
    assert [(w.code, w.line) for w in ws] == [
        ("undefined-reference", 3),
        ("undefined-citation", 3),
        ("undefined-reference", 5),
        ("undefined-references", None),
        ("rerun", None),
    ]
    assert ws[0].page == 1
    assert all(w.file == "refs.tex" for w in ws)
    info = parse_tex(read("pdflatex_refs.log")).info
    assert info is not None
    assert info.undefined_references
    assert info.rerun


def test_undefined_citation_only():
    ws = warns("pdflatex_undefcs_cite.log")
    assert ws[0].message.startswith("LaTeX Warning: Citation `a' on page 1 undefined")


def test_boxes():
    bs = boxes("pdflatex_boxes.log")
    assert [(b.code, b.line, b.end_line) for b in bs] == [
        ("overfull-hbox", 5, 6),
        ("underfull-hbox", 7, None),
        ("underfull-hbox", 10, None),
        ("overfull-hbox", 10, 11),
        ("underfull-vbox", 12, None),
        ("underfull-vbox", 12, None),
        ("overfull-vbox", None, None),
        ("overfull-vbox", None, None),
    ]
    assert "while \\output is active" in bs[-1].message
    assert any("Averyvery" in r for r in bs[0].raw)  # the offending text is kept
    assert all(b.file == "boxes.tex" for b in bs)


def test_font_shape_warning_joins_font_continuation():
    ws = warns("pdflatex_fontshape.log")
    assert [(w.code, w.line) for w in ws[:2]] == [
        ("font-shape-undefined", 3),
        ("font-shape-undefined", 4),
    ]
    assert ws[0].message == (
        "LaTeX Font Warning: Font shape `OT1/cmr/m/xx' undefined using `OT1/cmr/m/n' instead"
    )


def test_missing_character():
    (w,) = warns("pdflatex_missingchar.log")
    assert w.code == "missing-character"
    assert w.message.startswith("Missing character: There is no")
    assert w.file == "missingchar.tex"


def test_info_messages_only_on_request():
    text = read("pdflatex_fontshape.log")
    assert not [d for d in parse_tex(text).diagnostics if d.kind == "info"]
    infos = [
        d for d in parse_tex(text, include_info=True).diagnostics if d.kind == "info"
    ]
    assert infos
    assert all(i.message.startswith("LaTeX Font Info:") for i in infos)
    assert any(
        "Trying to load font information for OT1+qqq" in i.message for i in infos
    )


def test_shell_escape_and_openout():
    r = parse_tex(read("pdflatex_shellescape.log"))
    assert r.info is not None
    assert r.info.openout == ["pdflatex_shellescape.aux", "out.txt"]
    assert r.info.shell_commands == ["runsystem(echo hi)...disabled (restricted)."]
    (w,) = r.diagnostics
    assert w.code == "shell-escape-disabled"
    assert "echo hi" in w.message


def test_pdftex_warning():
    (w,) = parse_tex(read("pdflatex_pdftexwarn.log")).diagnostics
    assert (
        w.message
        == "pdfTeX warning: pdflatex (file nonexistent.map): cannot open font map file"
    )
    assert w.code == "map-file"
    assert w.package == "pdftex"


# -- LuaTeX / XeTeX ----------------------------------------------------------


def test_lua_errors_with_traceback():
    es = errs("lualatex_luaerror.log")
    assert [(e.code, e.line, e.message) for e in es] == [
        ("lua-error", 3, "Lua error: boom"),
        ("lua-error", 4, "Lua error: attempt to index a nil value (local 'x')"),
    ]
    assert "stack traceback:" in es[0].stack
    assert any("in function 'error'" in s for s in es[0].stack)
    assert es[0].context[0].startswith("l.3 \\directlua")
    ws = warns("lualatex_luaerror.log")
    assert [w.message for w in ws] == [
        "LuaTeX warning (pdf backend): no pages of output."
    ]
    info = parse_tex(read("lualatex_luaerror.log")).info
    assert info is not None
    assert info.no_output
    assert info.engine == "luahbtex"


def test_luaotfload_and_fontspec_errors():
    r = parse_tex(read("lualatex_luawarn.log"))
    assert any(d.code == "luaotfload" for d in r.diagnostics)
    es = [d for d in r.diagnostics if d.kind == "error"]
    assert {e.code for e in es} == {"package-error", "font-not-loadable"}
    fs = [e for e in es if e.package == "fontspec"]
    assert len(fs) == 3
    assert "cannot be found" in fs[0].message
    assert dedupe(fs)[0][1] == 3


def test_xetex_errors_and_missing_chars_come_with_codepoints():
    r = parse_tex(read("xelatex_xetexerr.log"))
    assert r.info is not None
    assert r.info.engine == "xetex"
    assert [d.code for d in r.diagnostics if d.kind == "error"][
        -1
    ] == "undefined-control-sequence"
    assert any(d.code == "missing-character" for d in r.diagnostics)


def test_xdvipdfmx_stdout():
    r = parse_tex(read("xelatex_xdvipdfmx.stdout.txt"))
    msgs = {d.code: d for d in r.diagnostics}
    assert msgs["xdvipdfmx-fatal"].source == "xdvipdfmx"
    assert "passed invalid object" in msgs["xdvipdfmx-fatal"].message
    assert msgs["driver-error"].message.startswith(
        "Output driver failed (return code 256)"
    )
    assert "Unable to load picture" in next(
        d.message for d in r.diagnostics if d.line == 4
    )
    assert r.info is not None
    assert r.info.fatal


def test_xdvipdfmx_log_has_picture_error():
    picture, driver = errs("xelatex_xdvipdfmx.log")
    assert picture.line == 4
    assert "bad.png" in picture.message
    assert driver.code == "driver-error"


# -- wrapped logs ------------------------------------------------------------


def test_wrapped_79_column_log_is_unwrapped():
    r = parse_tex(read("pdflatex_wrapped79.log"))
    assert r.info is not None
    assert r.info.wrapped_at == 79
    ws = [d for d in r.diagnostics if d.kind == "warning"]
    assert ws[0].line == 5
    assert ws[0].file == "wrap.tex"
    assert "getwrappedbytheengineatcolumn79" in ws[0].message
    (e,) = [d for d in r.diagnostics if d.kind == "error"]
    assert (e.file, e.line) == ("wrap.tex", 6)
    assert r.info.output == "pdflatex_wrapped79.pdf"


def test_explicit_max_print_line():
    wrapped = "LaTeX Warning: Reference `abcdefghij\nklmno' on page 1 undefined on input line 3.\n"
    (w,) = parse_tex(wrapped, max_print_line=36).diagnostics
    assert "`abcdefghijklmno'" in w.message
    assert w.line == 3


# -- file stack --------------------------------------------------------------


def test_file_stack_through_nested_packages():
    log = (
        "(./main.tex (/t/a.sty (/t/b.def) ) (./sec/x.tex\n"
        "LaTeX Warning: Foo on input line 7.\n\n) (/t/c.sty)\n"
        "LaTeX Warning: Bar.\n\n)\n"
    )
    a, b = parse_tex(log).diagnostics
    assert (a.file, a.line) == ("sec/x.tex", 7)
    assert (b.file, b.line) == ("main.tex", None)


def test_file_stack_parens_in_text_do_not_pop():
    log = (
        "(./main.tex\n"
        "File: size10.clo 2026-06-04 v1.4n Standard LaTeX file (size option)\n"
        "Package: foo 2020 (UK, DPC, DJ)\n"
        "LaTeX Warning: Foo on input line 3.\n)\n"
    )
    (d,) = parse_tex(log).diagnostics
    assert d.file == "main.tex"


def test_close_paren_at_end_of_text_line_closes_file():
    log = "(./a.tex (/t/b.sty\nluaotfload | db : loaded from /x/db.lua.gz)\nLaTeX Warning: W on input line 2.\n"
    (d,) = parse_tex(log).diagnostics
    assert d.file == "a.tex"
    assert d.line == 2


def test_float_dimension_is_not_a_file():
    log = "(./a.tex\nOverfull \\hbox (3.2pt too wide) in paragraph at lines 3--4\n[]\n\nLaTeX Warning: X on input line 9.\n"
    b, w = parse_tex(log).diagnostics
    assert b.file == w.file == "a.tex"


# -- postprocessing / API ----------------------------------------------------


def test_parse_build_combines_sources():
    res = parse_build(
        tex_log=read("pdflatex_refs.log"),
        blg=read("bibtex_warn.blg"),
        blg_program="bibtex",
        ilg=read("makeindex_errors.ilg"),
    )
    sources = [d.source for d in res.diagnostics]
    assert sources[0] == "tex"
    assert "bibtex" in sources
    assert "makeindex" in sources
    assert res.failed  # the makeindex input errors
    assert res.tex is not None
    assert res.tex.engine == "pdftex"
    assert res.latexmk is None


def test_parse_build_latexmk_only_adds_when_logs_have_no_errors():
    out = read("latexmk_failed.txt")
    with_errors = parse_build(tex_log=read("pdflatex_undefcs.log"), latexmk_output=out)
    assert not [d for d in with_errors.diagnostics if d.source == "latexmk"]
    assert with_errors.latexmk is not None
    assert with_errors.latexmk.status == "failed"
    clean = parse_build(tex_log=read("pdflatex_ok.log"), latexmk_output=out)
    assert any(d.source == "latexmk" for d in clean.diagnostics)
    assert clean.failed
