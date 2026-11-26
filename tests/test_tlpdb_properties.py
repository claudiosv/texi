"""Property tests of the tlpdb parser, index, search and ``texi cache`` helpers."""

import operator
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.errors import MarkupError
from typer.testing import CliRunner

from texi import tlpdb as tl
from texi import tlpdb_cli
from texi.cli import app

NL = "\n"
E_ACUTE_UPPER = chr(0xC9)
E_ACUTE = chr(0xE9)
SUPERSCRIPT_TWO = chr(0xB2)
LINE_SEPARATOR = chr(0x2028)
#: Characters besides "\n" that str.splitlines treats as a line break. The parser
#: splits on them and read_system_tlpdb does not (xfail below).
EXTRA_BREAKS = "\r\x0b\x0c\x1c\x1d\x1e\x85" + chr(0x2028) + chr(0x2029)

# Category "No" holds the characters that are isdigit() but that int() rejects,
# such as superscript two (xfail below). Surrogates cannot occur in decoded text.
chars = st.characters(exclude_categories=("Cs", "No"))
keys = st.sampled_from([
    "name",
    "category",
    "revision",
    "shortdesc",
    "longdesc",
    "catalogue",
    "depend",
    "runfiles",
    "docfiles",
    "srcfiles",
    "binfiles",
    "execute",
])
values = st.one_of(
    st.text(chars, max_size=20),
    st.integers(-5, 10**30).map(str),
    st.sampled_from([
        "size=3",
        "size=x",
        'arch="a b" size=1',
        "arch=windows",
        "00texlive.config",
        "release/2026",
        "x.ARCH",
        "",
    ]),
)
tlpdb_lines = st.one_of(
    st.builds(lambda k, v: f"{k} {v}", keys, values),
    values.map(lambda v: " " + v),
    st.just(""),
    st.text(chars, max_size=30),
)
tlpdb_texts = st.one_of(
    st.lists(tlpdb_lines, max_size=25).map(NL.join), st.text(chars, max_size=200)
)


@settings(max_examples=60, deadline=None)
@given(text=tlpdb_texts)
def test_parse_tlpdb_never_raises(text: str) -> None:
    with tl.parse_tlpdb(text) as db:
        names = db.names()
        assert len(names) == len(set(names)) == db.count()
        assert all(names)
        assert set(db.revisions()) <= set(names)
        assert db.stats().entries == len(names)
        for name in names:
            pkg = db.get(name)
            assert pkg is not None
            assert pkg.name == name
            pkg.to_dict()


names = st.one_of(
    st.from_regex(r"[a-z][a-z0-9.-]{0,8}", fullmatch=True),
    st.sampled_from(["00texlive.config", "00texlive.installation"]),
)
free_text = st.text(st.characters(exclude_categories=("Cs", "No")), max_size=30).filter(
    lambda s: not set(s) & set(EXTRA_BREAKS + "\n")
)
# Trailing space after a revision is excluded: see the _newest_revision xfail.
revisions = st.one_of(st.none(), st.integers(0, 10**9))
depends = st.lists(
    st.sampled_from(["a", "b.ARCH", "release/2026", "opt_location:__MASTER__"]),
    max_size=3,
)
entries = st.lists(
    st.tuples(names, revisions, free_text, depends),
    max_size=6,
    unique_by=operator.itemgetter(0),
)


@settings(max_examples=60, deadline=None)
@given(rows=entries)
def test_the_three_tlpdb_readers_agree(
    rows: list[tuple[str, int | None, str, list[str]]],
) -> None:
    blocks = []
    for name, revision, desc, deps in rows:
        lines = [f"name {name}", "category Package"]
        if revision is not None:
            lines.append(f"revision {revision}")
        lines.append(f"longdesc {desc}")
        lines += [f"depend {d}" for d in deps]
        blocks.append(NL.join(lines) + NL)
    text = NL.join(blocks)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "tlpkg", "texlive.tlpdb")
        path.parent.mkdir()
        path.write_bytes(text.encode("utf-8"))
        system = tl.read_system_tlpdb(path)
    wanted = {name: rev for name, rev, _, _ in rows if rev is not None}
    with tl.parse_tlpdb(text) as db:
        assert set(db.names()) == system.installed == {row[0] for row in rows}
        assert db.revisions() == system.revisions == wanted
        merged: dict[str, str] = {}
        for name, *_ in rows:
            if name.startswith("00texlive."):
                merged.update(db.settings(name))
        assert system.settings == merged
    assert tl._newest_revision(text) == max(wanted.values(), default=0)


# --- search -----------------------------------------------------------------

# ASCII only and no NUL: see the non-ASCII and NUL xfails below.
ALPHABET = "abAB-_%\\\"' *"
words = st.text(ALPHABET, max_size=8)
docs = st.lists(
    st.tuples(words, words, words, st.sampled_from(["Package", "Collection"])),
    max_size=5,
)
queries = st.lists(st.text(ALPHABET.strip(), min_size=1, max_size=5), max_size=3).map(
    " ".join
)


@settings(max_examples=100, deadline=None)
@given(
    rows=docs,
    query=queries,
    in_name=st.booleans(),
    in_desc=st.booleans(),
    category=st.sampled_from([None, "package", "Collection"]),
)
@example(
    rows=[("100%", "a_b", "x\\y", "Package")],
    query="%",
    in_name=True,
    in_desc=True,
    category=None,
)
def test_search_fts_and_like_both_equal_a_plain_scan(
    rows: list[tuple[str, str, str, str]],
    query: str,
    in_name: bool,
    in_desc: bool,
    category: str | None,
) -> None:
    text = NL.join(
        f"name p{i}\ncategory {cat}\ncatalogue c{key}\n"
        f"shortdesc {short}\nlongdesc {long}\n"
        for i, (key, short, long, cat) in enumerate(rows)
    )
    results = []
    for fts in (True, False):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tl, "_FTS_ENABLED", fts)
            with tl.parse_tlpdb(text) as db:
                hits = db.search(
                    query, in_name=in_name, in_desc=in_desc, category=category
                )
                results.append([(h.package.name, h.score, h.matched_on) for h in hits])
                every = [db.get(n) for n in db.names()]
    assert results[0] == results[1]
    terms = query.lower().split()
    wanted = set()
    for pkg in every:
        assert pkg is not None
        searched = [pkg.name, pkg.catalogue_key] if in_name else []
        searched += [pkg.shortdesc, pkg.longdesc] if in_desc else []
        if (
            terms
            and (category is None or pkg.category.lower() == category.lower())
            and all(any(t in v.lower() for v in searched) for t in terms)
        ):
            wanted.add(pkg.name)
    assert {name for name, _, _ in results[0]} == wanted
    assert results[0] == sorted(results[0], key=lambda r: (-r[1], r[0]))


# --- repository names -------------------------------------------------------

repo_pieces = st.sampled_from(["ctan", "TLnet", "https://r.example", "x", "/", " "])
repositories = st.one_of(st.text(), st.lists(repo_pieces, max_size=5).map("".join))


def _unstable(repo: str) -> bool:
    """Inputs where stripping the slash exposes an alias or more whitespace."""
    s = repo.strip()
    if s.lower() in {"ctan", "tlnet"}:
        return False
    r = s.rstrip("/")
    return r != r.strip() or r.lower() in {"ctan", "tlnet"}


@given(repo=repositories)
def test_normalize_repository_is_idempotent(repo: str) -> None:
    once = tl.normalize_repository(repo)
    assert not once.endswith("/")
    if _unstable(repo):  # see the "ctan/" xfail
        return
    assert tl.normalize_repository(once) == once


# --- texi cache ---------------------------------------------------------------


@given(value=st.one_of(st.none(), st.text(chars), st.integers(0, 10**18).map(str)))
def test_fmt_size_formats_any_field_value(value: str | None) -> None:
    out = tlpdb_cli._fmt_size(value)
    if value and value.isdigit():
        assert out.endswith(f"({int(value):,} bytes)")
    else:
        assert out == (value or "-")


CLI_DB = (
    "name alpha\ncategory Package\nrevision 3\ncatalogue beta\n"
    "shortdesc A [bold] thing\nrunfiles size=1\n RELOC/tex/latex/alpha/alpha.sty\n"
)


@settings(max_examples=25, deadline=None)
@given(
    query=st.lists(
        st.text("alphbet[]/\\() .*?+", min_size=1, max_size=6), min_size=1, max_size=3
    ),
    flags=st.sets(st.sampled_from(["--file", "--regex", "--exact"])),
)
def test_cache_search_exits_cleanly(query: list[str], flags: set[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", tmp)
        path = Path(tmp, "texlive.tlpdb")
        path.write_text(CLI_DB, encoding="utf-8")
        result = CliRunner().invoke(
            app,
            ["cache", "search", "--local", str(path), *sorted(flags), "--", *query],
        )
    assert result.exit_code in {0, 1}, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --- confirmed bugs -----------------------------------------------------------


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="isdigit() accepts superscript two"
)
def test_a_superscript_runfiles_size_is_ignored() -> None:
    tl.parse_tlpdb(f"name a\nrunfiles size={SUPERSCRIPT_TWO}\n").close()


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="isdigit() accepts superscript two"
)
def test_a_superscript_revision_is_not_numeric() -> None:
    with tl.parse_tlpdb(f"name a\nrevision {SUPERSCRIPT_TWO}\n") as db:
        assert db.revisions() == {}


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="isdigit() accepts superscript two"
)
def test_system_tlpdb_skips_a_superscript_revision(tmp_path: Path) -> None:
    path = tmp_path / "texlive.tlpdb"
    path.write_text(f"name a\nrevision {SUPERSCRIPT_TWO}\n", encoding="utf-8")
    assert tl.read_system_tlpdb(path).revisions == {}


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="int() digit limit is not handled"
)
def test_an_oversized_size_does_not_break_parsing() -> None:
    tl.parse_tlpdb(f"name a\nrunfiles size={'1' * 5000}\n").close()


@pytest.mark.xfail(
    strict=True, raises=ValueError, reason="isdigit() accepts superscript two"
)
def test_fmt_size_keeps_a_superscript_value() -> None:
    assert tlpdb_cli._fmt_size(SUPERSCRIPT_TWO) == SUPERSCRIPT_TWO


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="splitlines() splits a value at U+2028, so it can rename the package",
)
def test_a_line_separator_in_longdesc_does_not_rename_the_package() -> None:
    with tl.parse_tlpdb(f"name a\nlongdesc x{LINE_SEPARATOR}name evil\n") as db:
        assert db.names() == ["a"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="_REVISION_LINE rejects a trailing space that both parsers accept",
)
def test_newest_revision_allows_trailing_whitespace() -> None:
    text = "name a\nrevision 5 \n"
    with tl.parse_tlpdb(text) as db:
        assert tl._newest_revision(text) == max(db.revisions().values())


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="LIKE folds ASCII case only, so short terms miss non-ASCII capitals",
)
def test_short_search_term_is_case_insensitive_beyond_ascii() -> None:
    with tl.parse_tlpdb(f"name a\nshortdesc {E_ACUTE_UPPER}cole\n") as db:
        assert [h.package.name for h in db.search(E_ACUTE + "c")] == ["a"]


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="the LIKE fallback folds ASCII only"
)
def test_search_without_fts_is_case_insensitive_beyond_ascii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tl, "_FTS_ENABLED", False)
    with tl.parse_tlpdb(f"name a\nshortdesc {E_ACUTE_UPPER}cole\n") as db:
        assert [h.package.name for h in db.search(E_ACUTE + "cole")] == ["a"]


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="file search uses ASCII-only LIKE"
)
def test_file_search_is_case_insensitive_beyond_ascii() -> None:
    text = f"name a\nrunfiles\n RELOC/{E_ACUTE_UPPER}cole.tex\n"
    with tl.parse_tlpdb(text) as db:
        hits = db.search(E_ACUTE + "cole", in_files=True)
        assert [h.package.name for h in hits] == ["a"]


@pytest.mark.xfail(
    strict=True,
    raises=sqlite3.OperationalError,
    reason="a NUL in a term of 3+ characters ends the FTS5 string early",
)
def test_search_term_with_nul_does_not_leak_sqlite_errors() -> None:
    with tl.parse_tlpdb("name abc\n") as db:
        db.search("abc\x00")


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a slash is stripped after the alias check, so 'ctan/' becomes 'ctan'",
)
def test_alias_with_a_trailing_slash_is_resolved() -> None:
    assert tl.normalize_repository("ctan/") == tl.DEFAULT_REPOSITORY


@pytest.mark.xfail(
    strict=True,
    raises=MarkupError,
    reason="the section name from the tlpdb is printed as Rich markup",
)
def test_info_prints_a_bracketed_binfiles_arch() -> None:
    with tl.parse_tlpdb('name a\nbinfiles arch="[/]"\n bin/x\n') as db:
        pkg = db.get("a")
        assert pkg is not None
        tlpdb_cli._print_info(db, pkg, show_files=False, all_files=False)
