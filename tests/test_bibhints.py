from pathlib import Path

from texi.bibhints import explain
from texi.bibparse import BibFile, parse_bib
from texi.logparse_bib import parse_blg

BIB = """@article{smith2020,
  title = {A},
  year = 2020
}
@article{smith2020,
  title = {B},
}
@misc{doe2021, note = {x}}
"""
FIX = Path(__file__).parent / "fixtures" / "logs"


def _bibs(text: str = BIB) -> dict[str, BibFile]:
    return {"/p/refs.bib": parse_bib(text)}


def test_missing_entry_suggests_near_key() -> None:
    (d,) = parse_blg('Warning--I didn\'t find a database entry for "smith2021"\n')
    h = explain(d, _bibs())
    assert h and "smith2020" in h


def test_missing_entry_case() -> None:
    (d,) = parse_blg('Warning--I didn\'t find a database entry for "Doe2021"\n')
    h = explain(d, _bibs())
    assert h and "differs only in case" in h


def test_missing_entry_without_bibs() -> None:
    (d,) = parse_blg('Warning--I didn\'t find a database entry for "x"\n')
    assert "\\bibliography" in explain(d, {})


def test_duplicate_lists_locations() -> None:
    blg = "[1] Utils.pm:1> WARN - Duplicate entry key 'smith2020' in file 'refs.bib', skipping ...\n"
    (d,) = parse_blg(blg)
    h = explain(d, _bibs())
    assert h and "refs.bib:1, refs.bib:5" in h and "differ" in h


def test_undefined_string() -> None:
    (d,) = parse_blg('Warning--string name "foo" is undefined\n--line 3 of file r.bib\n')
    assert "bare word" in explain(d, {})


def test_empty_field_names_entry_line() -> None:
    (d,) = parse_blg("Warning--empty note in doe2021\n")
    h = explain(d, _bibs())
    assert h and "@misc" in h and "refs.bib:8" in h


def test_datamodel_typo_and_missing() -> None:
    inv = "[1] U.pm:1> WARN - Datamodel: Entry 'doe2021' (refs.bib): Invalid field 'jorunal' for entrytype 'article'\n"
    (d,) = parse_blg(inv)
    assert "'journal'" in explain(d, _bibs())
    miss = "[1] U.pm:1> WARN - Datamodel: Entry 'doe2021' (refs.bib): Missing mandatory field 'journaltitle'\n"
    (d,) = parse_blg(miss)
    h = explain(d, _bibs())
    assert h and "alias" in h and "refs.bib:8" in h


def test_missing_source_similar_file() -> None:
    (d,) = parse_blg("[1] U.pm:1> ERROR - Data source 'ref.bib' not found in section 0\n")
    assert "refs.bib" in explain(d, _bibs())


def test_bibtex_syntax_quotes_line() -> None:
    (d,) = parse_blg("I was expecting a `,' or a `}'---line 3 of file refs.bib\n")
    h = explain(d, _bibs())
    assert h and "smith2020" in h and "comma" in h


def test_fixture_logs_all_error_codes_have_hints() -> None:
    for name in ("bibtex_errors", "bibtex_nostyle", "biber_errors", "biber_nocontrol"):
        for d in parse_blg((FIX / f"{name}.blg").read_text()):
            if d.kind == "error" and d.code not in {"biber-syntax", "bibtex-error", "biber-error"}:
                assert explain(d, {}), (name, d.code, d.message)
