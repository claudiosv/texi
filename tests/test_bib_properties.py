"""Property tests of .bib parsing and bibliography hints."""

import re

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from texi import bibhints, bibparse
from texi.logparse_core import Diagnostic

# Characters str.splitlines() breaks on but the grammar does not count as a new
# line: BibFile.lines then drifts from BibEntry.line (see the xfail test).
ODD_BREAKS = "".join(map(chr, [11, 12, 13, 28, 29, 30, 133, 0x2028, 0x2029]))

names = st.from_regex(r"[a-z][a-z0-9]{0,6}", fullmatch=True)
keys = st.from_regex(r"[A-Za-z][A-Za-z0-9:_-]{0,8}", fullmatch=True)
types = st.sampled_from(["article", "book", "Misc", "InProceedings"])
words = st.text("abcXYZ019 .,:-", max_size=10)
# Braced values, possibly with nested braces and line breaks.
values = st.recursive(
    words,
    lambda inner: st.one_of(
        inner.map("{{{}}}".format),
        st.tuples(inner, st.sampled_from(["", " ", "\n"]), inner).map("".join),
    ),
    max_leaves=5,
)
entries = st.tuples(types, keys, st.dictionaries(names, values, min_size=1, max_size=4))
# Text between entries is a comment as far as BibTeX is concerned.
gaps = st.sampled_from(["\n", "\n\n", "\n\n\n"])


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(gaps, entries), max_size=5))
def test_parse_bib_round_trip(rows: list) -> None:
    source = ""
    expected = []
    for gap, (ty, key, fields) in rows:
        source += gap
        line = source.count("\n") + 1
        body = "".join(f"  {n} = {{{v}}},\n" for n, v in fields.items())
        source += f"@{ty}{{{key},\n{body}}}"
        expected.append(bibparse.BibEntry(ty.lower(), key, fields, line))
    bib = bibparse.parse_bib(source)
    assert bib.errors == []
    assert bib.entries == expected
    assert bibparse.parse_bib(source.encode()) == bib
    for entry in bib.entries:
        assert bib.lines[entry.line - 1].lower().startswith(f"@{entry.type}{{")
    found = [e[1] for _, e in rows]
    dups = bib.duplicate_keys()
    assert len(dups) == len(set(dups))
    assert set(dups) == {k for k in found if found.count(k) > 1}


fragments = st.sampled_from([
    "@article{",
    "@string{",
    "@preamble{",
    "@comment{",
    "@",
    "{",
    "}",
    "(",
    ")",
    '"',
    "#",
    "=",
    ",",
    "\n",
    " ",
    "key",
    "title",
    "%",
    "\\",
])
soup = st.lists(
    st.one_of(
        fragments,
        st.text(
            st.characters(exclude_characters=ODD_BREAKS, exclude_categories=["Cs"]),
            max_size=6,
        ),
    ),
    max_size=30,
).map("".join)


@settings(max_examples=80, deadline=None)
@given(soup)
def test_parse_bib_never_raises_and_lines_stay_in_range(source: str) -> None:
    bib = bibparse.parse_bib(source)
    for entry in bib.entries:
        assert 1 <= entry.line <= len(bib.lines)
        assert "@" in bib.lines[entry.line - 1]
    assert bib.errors == sorted(bib.errors, key=lambda e: (e.line, e.column))


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="BibFile.lines uses splitlines(), BibEntry.line counts only newlines",
)
@pytest.mark.parametrize(
    "char", [chr(12), chr(0x2028)], ids=["form-feed", "line-separator"]
)
def test_entry_line_indexes_lines_after_unicode_line_break(char: str) -> None:
    bib = bibparse.parse_bib(f"x{char}y\n@misc{{k, a={{1}}}}\n")
    (entry,) = bib.entries
    assert bib.lines[entry.line - 1].startswith("@misc")


# A small key space, so that exact, near and case-only matches are all common.
close_keys = st.text("abcABC12", min_size=1, max_size=4)
bib_maps = st.dictionaries(
    st.sampled_from(["/p/refs.bib", "/p/other.bib", "x.bib"]),
    st.lists(close_keys, max_size=5).map(
        lambda ks: bibparse.parse_bib(
            "".join(f"@article{{{k},\n title = {{t}},\n}}\n" for k in ks)
        )
    ),
    max_size=3,
)
messages = st.one_of(
    st.text(max_size=30),
    st.builds(
        str.format,
        st.sampled_from([
            'I didn\'t find a database entry for "{0}"',
            "Duplicate entry key '{0}' in file 'refs.bib'",
            'string name "{0}" is undefined',
            "empty {1} in {0}",
            "Invalid field '{1}' for entrytype '{0}'",
            "Missing mandatory field '{1}'",
            "Data source '{0}' not found",
            "I couldn't open database file {0}",
            "I found no \\bibstyle command",
            "I found no \\bibdata command",
            "I found no \\citation commands",
        ]),
        st.one_of(close_keys, st.text(max_size=6)),
        st.sampled_from(["journaltitle", "date", "jorunal", "nothing"]),
    ),
)
codes = st.sampled_from([
    None,
    "no-such-code",
    *bibhints._HANDLERS,
    *bibhints._STATIC,
])
diagnostics = st.builds(
    Diagnostic,
    kind=st.sampled_from(["error", "warning"]),
    message=messages,
    file=st.one_of(st.none(), st.sampled_from(["refs.bib", "/q/x.bib", "ref.bib", ""])),
    line=st.one_of(st.none(), st.integers(-3, 30)),
    context=st.lists(
        st.one_of(st.text(max_size=20), close_keys.map("@a{{{}".format)), max_size=3
    ),
    source=st.sampled_from(["bibtex", "biber", "tex"]),
    code=codes,
    entry=st.one_of(st.none(), close_keys),
)


@settings(max_examples=200, deadline=None)
@given(d=diagnostics, bibs=bib_maps)
def test_explain_never_raises(d: Diagnostic, bibs: dict) -> None:
    hint = bibhints.explain(d, bibs)
    if d.source == "tex" or not d.code:
        assert hint is None
    else:
        assert hint is None or hint
    if d.code in bibhints._STATIC:
        assert hint == (bibhints._STATIC[d.code] if d.source != "tex" else None)


@settings(max_examples=150, deadline=None)
@given(key=close_keys, bibs=bib_maps, source=st.sampled_from(["bibtex", "biber"]))
def test_a_missing_key_is_not_suggested_for_itself(
    key: str, bibs: dict, source: str
) -> None:
    # A key that a .bib does define is the xfail case below.
    assume(all(key not in bib.keys for bib in bibs.values()))
    d = Diagnostic(
        "warning",
        f'I didn\'t find a database entry for "{key}"',
        source=source,
        code=f"{source}-missing-entry",
    )
    hint = bibhints.explain(d, bibs)
    assert hint is not None
    if suggested := re.search(r"did you mean (.*)\? Fix", hint):
        offered = re.findall(r"'([^']*)'", suggested[1])
        assert offered
        assert key not in offered
        assert set(offered) <= {k for bib in bibs.values() for k in bib.keys}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="explain suggests the missing key itself when a .bib read defines it",
)
def test_defined_but_reported_missing_key_is_not_suggested() -> None:
    d = Diagnostic(
        "warning",
        'I didn\'t find a database entry for "abc"',
        source="bibtex",
        code="bibtex-missing-entry",
    )
    bibs = {"r.bib": bibparse.parse_bib("@misc{abc, note={x}}")}
    assert "did you mean 'abc'" not in (bibhints.explain(d, bibs) or "")
