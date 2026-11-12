from texi.logparse_texfmt import parse_texfmt

LOG = """\
INFO: tex-fmt a.tex: Formatting started. 
TRACE: tex-fmt a.tex: Line 2 (1). Indent: actual = 2, visual = 1: \\item a
WARN: tex-fmt a.tex: Line 3 (3). Line cannot be wrapped. aaaaaaaa
WARN: tex-fmt b.tex: Indent does not return to zero. Last non-indented line is line 2 
ERROR: tex-fmt a.tex: Incorrect formatting. 
ERROR: tex-fmt nope.tex: Could not open file. 
thread 'main' panicked
"""


def test_levels_and_kinds() -> None:
    d = parse_texfmt(LOG)
    assert [x.kind for x in d] == ["info", "info", "warning", "warning", "error", "error", "info"]
    assert d[1].code == "trace" and d[1].message == "Indent: actual = 2, visual = 1"
    assert d[1].context == ["\\item a"]


def test_line_warning() -> None:
    w = parse_texfmt(LOG)[2]
    assert (w.file, w.line, w.message) == ("a.tex", 3, "Line cannot be wrapped.")
    assert w.context == ["aaaaaaaa"] and w.code == "line-cannot-be-wrapped"
    assert w.hint


def test_file_level_messages() -> None:
    d = parse_texfmt(LOG)
    assert d[3].line == 2 and d[3].message.startswith("Indent does not return to zero")
    assert d[4].message == "Incorrect formatting." and d[4].line is None
    assert d[4].hint is None
    assert d[5].code == "could-not-open-file"


def test_unparsed_line_is_kept() -> None:
    last = parse_texfmt(LOG)[-1]
    assert last.code == "output" and last.message == "thread 'main' panicked"


def test_garbage_does_not_raise() -> None:
    assert parse_texfmt("") == []
    assert parse_texfmt("\r\n\r\nERROR: tex-fmt\n")[0].code == "output"


def test_line_number_is_the_original_line() -> None:
    d = parse_texfmt("TRACE: tex-fmt m.tex: Line 5 (3). Indent: actual = 0, visual = 0: b\n")[0]
    assert d.line == 3 and d.context == ["b"]
    assert parse_texfmt(
        "TRACE: tex-fmt m.tex: Line 5 (3). Indent: actual = 0, visual = 0: b\n", formatted=True
    )[0].line == 5
