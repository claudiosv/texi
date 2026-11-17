from texi.bibparse import parse_bib, parse_bib_file

SRC = """% comment
@string{me = "Me"}
@preamble{"\\newcommand{\\x}{y}"}
@comment{ignored}
@Book{k:1,
  author = me # " and You",
  title = {A {B} c},
  year = 2020,
}
@article(k2, journal = "J")
"""


def test_entries_and_fields():
    bf = parse_bib(SRC)
    assert bf.keys == ["k:1", "k2"]
    book = bf.entries[0]
    assert book.type == "book"
    assert book.line == 5
    assert book.fields["author"] == 'me #  and You'
    assert book.fields["title"] == "A {B} c"
    assert book.fields["year"] == "2020"
    assert bf.entries[1].fields == {"journal": "J"}


def test_strings_preamble():
    bf = parse_bib(SRC)
    assert bf.strings == {"me": "Me"}
    assert len(bf.preambles) == 1


def test_clean_has_no_errors():
    assert parse_bib(SRC).errors == []


def test_errors_reported_and_recovered():
    bf = parse_bib("@article{a, title={x}}\n@bad{\n@book{b, year=1}\n")
    assert "a" in bf.keys
    assert bf.errors


def test_duplicate_keys():
    bf = parse_bib("@misc{a,}\n@misc{b,}\n@misc{a,}\n")
    assert bf.duplicate_keys() == ["a"]


def test_parse_file(tmp_path):
    p = tmp_path / "x.bib"
    p.write_text("@misc{z, note={n}}")
    assert parse_bib_file(p).entries[0].fields["note"] == "n"
