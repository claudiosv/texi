"""latexmk stdout/stderr fixtures."""

import pytest
from logfixtures import LOGS, exit_code, read

from texi.logparse import EXIT_CODES, parse_build, parse_latexmk, run_sequence


def lm(name, with_code=True):
    code = (
        exit_code(name + ".exit")
        if with_code and (LOGS / f"{name}.exit").exists()
        else None
    )
    return parse_latexmk(read(name + ".txt"), exit_code=code)


def test_success_two_passes():
    r = lm("latexmk_success")
    i = r.info
    assert i.status == "success" and i.passes() == 2 and i.rules_run() == ["pdflatex"]
    assert i.targets == ["main.pdf"] and run_sequence(i) == "pdflatex x2"
    assert [d.code for d in r.diagnostics] == [
        "undefined-reference",
        "undefined-references",
        "rerun",
    ]


def test_up_to_date():
    r = lm("latexmk_uptodate")
    assert r.info.status == "up-to-date" and r.info.passes() == 0
    assert r.info.targets == ["main.pdf"] and r.diagnostics == []


def test_failed_run():
    r = lm("latexmk_failed")
    i = r.info
    assert i.status == "failed" and i.exit_code == 12
    assert i.exit_meaning == EXIT_CODES[12]
    assert i.failed_rules == ["pdflatex"] and i.missing_files == ["nope.tex"]
    codes = [d.code for d in r.diagnostics]
    assert "undefined-control-sequence" in codes and "latexmk-rule-failed" in codes
    assert codes[-1] == "latexmk-missing-file"


def test_failed_force_previous_error():
    r = lm("latexmk_failed_force")
    assert r.info.status == "failed" and r.info.previous_error
    assert [d.code for d in r.diagnostics] == ["latexmk-previous-error"]


def test_max_runs():
    r = lm("latexmk_maxruns")
    assert r.info.max_runs_reached and r.info.passes() == 2
    assert "latexmk-max-runs" in [d.code for d in r.diagnostics]


def test_no_source_file():
    r = lm("latexmk_nofile")
    assert r.info.exit_code == 11 and r.info.missing_files == ["main2.tex"]
    (d,) = r.diagnostics
    assert d.code == "latexmk-no-source" and d.file == "main2.tex"


def test_rc_error():
    r = lm("latexmk_rcerror")
    assert r.info.rc_error and r.info.exit_code == 255
    assert [d.code for d in r.diagnostics] == ["latexmk-rc-error"]


def test_bad_option():
    r = lm("latexmk_badarg")
    assert r.info.bad_options and r.info.exit_code == 10
    assert [d.code for d in r.diagnostics] == ["latexmk-bad-option"]


def test_bib_index_sequence():
    r = lm("latexmk_bib_index")
    i = r.info
    assert i.status == "success" and i.passes() == 6
    assert i.rules_run() == ["pdflatex", "makeindex main.idx", "bibtex main"]
    assert run_sequence(i) == (
        "pdflatex x1, makeindex main.idx x1, bibtex main x1, "
        "pdflatex x1, bibtex main x1, pdflatex x1"
    )
    assert "main.idx" in i.generated and i.passes("bibtex main") == 2


@pytest.mark.parametrize("code", sorted(EXIT_CODES))
def test_exit_code_meaning(code):
    i = parse_latexmk("", exit_code=code).info
    assert i.exit_code == code and i.exit_meaning == EXIT_CODES[code]
    assert i.status == ("success" if code == 0 else "failed")


def test_unknown_exit_code():
    assert parse_latexmk("", exit_code=77).info.status == "failed"


def test_parse_build_combines():
    r = parse_build(
        tex_log=read("pdflatex_undefcs.log"),
        latexmk_output=read("latexmk_failed.txt"),
        latexmk_exit_code=12,
    )
    assert r.failed and r.latexmk is not None and r.tex is not None
    assert not any(d.source == "latexmk" for d in r.diagnostics)


def test_parse_build_latexmk_only_when_no_tex_errors():
    r = parse_build(
        tex_log=read("pdflatex_ok.log"),
        latexmk_output=read("latexmk_nofile.txt"),
        latexmk_exit_code=11,
    )
    assert [d.code for d in r.errors] == ["latexmk-no-source"] and r.failed


def test_parse_build_latexmk_stdout_fallback():
    r = parse_build(latexmk_output=read("latexmk_failed.txt"), latexmk_exit_code=12)
    assert any(d.code == "undefined-control-sequence" for d in r.diagnostics)
