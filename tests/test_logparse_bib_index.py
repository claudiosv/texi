"""BibTeX, Biber, makeindex and xindy log fixtures."""

from logfixtures import read

from texi.logparse import parse_bib, parse_blg, parse_index


def bib(name):
    return parse_bib(read(name))


def summary(diags):
    return [(d.kind, d.code, d.file, d.line) for d in diags]


def test_biber_errors():
    r = bib("biber_errors.blg")
    assert r.info.tool == "biber" and r.info.version == "2.19"
    assert (r.info.error_count, r.info.warning_count) == (3, 1)
    assert summary(r.diagnostics) == [
        ("error", "biber-syntax", "refs.bib", 12),
        ("warning", "biber-syntax", "refs.bib", 8),
        ("error", "biber-source-not-found", "missing.bib", None),
        ("error", "biber-cannot-open", "refs2.bib", None),
        ("error", "biber-fatal", None, None),
    ]
    assert all(d.source == "biber" for d in r.diagnostics)


def test_biber_missing_control_file():
    r = bib("biber_nocontrol.blg")
    (d,) = r.diagnostics
    assert d.code == "biber-missing-control-file" and d.file == "nonexistent.bcf"


def test_biber_warnings():
    r = bib("biber_warnings.blg")
    assert r.info.warning_count == 6
    assert [d.code for d in r.diagnostics] == [
        "biber-duplicate-entry",
        "biber-datamodel",
        "biber-datamodel",
        "biber-missing-entry",
        "biber-warning",
        "biber-warning",
    ]
    assert r.diagnostics[0].entry == "smith"
    assert r.diagnostics[-1].line == 12
    assert all(d.kind == "warning" for d in r.diagnostics)


def test_bibtex_errors():
    r = bib("bibtex_errors.blg")
    i = r.info
    assert (i.tool, i.version, i.aux, i.style) == (
        "bibtex",
        "0.99e",
        "bt.aux",
        "plain.bst",
    )
    assert i.databases == ["refs.bib"] and i.entries_used == 2 and i.error_count == 2
    assert summary(r.diagnostics) == [
        ("error", "bibtex-missing-database", "bt.aux", 6),
        ("error", "bibtex-syntax", "refs.bib", 3),
        ("warning", "bibtex-missing-entry", None, None),
        ("warning", "bibtex-empty-field", None, None),
    ]
    assert r.diagnostics[2].entry == "absent"
    assert r.diagnostics[3].entry == "nojournal"


def test_bibtex_nostyle():
    r = bib("bibtex_nostyle.blg")
    assert [d.code for d in r.diagnostics] == [
        "bibtex-missing-style",
        "bibtex-missing-command",
    ]
    assert r.diagnostics[0].file == "bt4.aux" and r.diagnostics[0].line == 3


def test_bibtex_repeated_bibstyle():
    r = bib("bibtex_warn.blg")
    assert [d.code for d in r.diagnostics] == [
        "bibtex-repeated-bibstyle",
        "bibtex-empty-field",
    ]
    assert r.info.error_count == 1


def test_parse_blg_compat():
    ds = parse_blg(read("bibtex_errors.blg"), "bibtex")
    assert len(ds) == 4 and {d.source for d in ds} == {"bibtex"}
    assert parse_blg("") == []


def test_makeindex_errors():
    r = parse_index(read("makeindex_errors.ilg"))
    assert r.info.tool == "makeindex" and r.info.version == "2.18"
    assert (r.info.accepted, r.info.rejected, r.info.warning_count) == (4, 3, 1)
    assert [(d.kind, d.file, d.line) for d in r.diagnostics] == [
        ("error", "bad.idx", 4),
        ("error", "bad.idx", 5),
        ("error", "bad.idx", 6),
        ("warning", "bad.idx", 3),
    ]
    assert r.diagnostics[3].code == "makeindex-warning"


def test_makeindex_ok():
    for name, out in [
        ("makeindex_ok.ilg", "idx.ind"),
        ("makeglossaries_ok.glg", "main.gls"),
    ]:
        r = parse_index(read(name))
        assert r.diagnostics == []
        assert r.info.output == out and r.info.rejected == 0 and r.info.sorted


def test_makeindex_missing_input():
    (d,) = parse_index(read("makeindex_missing.txt")).diagnostics
    assert d.code == "index-input-missing" and d.kind == "error"


def test_xindy():
    r = parse_index(read("xindy_warn.glg"))
    assert r.info.tool == "xindy" and r.info.nothing_written
    assert [(d.kind, d.code) for d in r.diagnostics] == [
        ("warning", "xindy-warning"),
        ("error", "xindy-error"),
        ("warning", "index-empty"),
    ]


def test_biber_temp_file_is_mapped_and_explained() -> None:
    import hashlib

    from texi.logparse_bib import biber_temp_sources, explain_syntax, parse_blg

    md5 = hashlib.md5(b"main.bib").hexdigest()
    blg = (
        "[961] Biber.pm:132> WARN - BibTeX subsystem: "
        f"/var/folders/x/T/biber_tmp_ab/{md5}_41959.utf8, line 3, warning: "
        "possible runaway string started at line 2\n"
    )
    (d,) = parse_blg(blg)
    assert d.file == md5 and d.line == 3
    bcf = "<bcf:datasource type='file' datatype='bibtex'>main.bib</bcf:datasource>"
    assert biber_temp_sources(bcf)[md5] == "main.bib"
    d.file = "/p/main.bib"
    lines = ["@misc{k,", "  author = {A and family=B,", "  useprefix=true and C},", "}"]
    hint = explain_syntax(d, lines)
    assert hint and "from line 2 to 3" in hint and "'useprefix='" in hint
