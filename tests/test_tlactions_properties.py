"""Property tests of the execute-line parsers and files of texi.tlactions.

No external tools: nothing here may start a process.
"""

import json
import shlex
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from texi import tlactions as ta

FEW = settings(max_examples=40, deadline=None)


@contextmanager
def tree() -> Generator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


# --- execute lines ------------------------------------------------------------

# "/" is not allowed in format names and engines (parse_add_format rejects it): see
# the test further down
idents = st.from_regex(r"[a-z][a-z0-9.-]{0,8}", fullmatch=True)
# one line, like the tlpdb line it comes from
free = st.text(st.characters(codec="utf-8", exclude_characters="\n"), max_size=15)
junk = st.one_of(
    st.text(max_size=30),
    st.lists(
        st.sampled_from([
            "name=x",
            "engine=y",
            "lefthyphenmin=2",
            "righthyphenmin=33",
            "synonyms=",
            "databases=dat",
            "mode",
            "=",
            "'",
            '"',
            "\\",
            "name",
        ]),
        max_size=6,
    ).map(" ".join),
)


def words(**values: str) -> str:
    return " ".join(shlex.quote(f"{k}={v}") for k, v in values.items())


@given(line=junk)
@example("'")
@example("\\")
def test_parsers_raise_only_execute_error(line: str) -> None:
    for parse in (ta.parse_add_format, ta.parse_add_hyphen):
        with suppress(ta.ExecuteError):
            parse(line)


@given(
    name=idents,
    engine=idents,
    patterns=free,
    options=free,
    disabled=st.booleans(),
    triggers=st.lists(idents, max_size=3),
)
def test_parse_add_format(
    name: str,
    engine: str,
    patterns: str,
    options: str,
    disabled: bool,
    triggers: list[str],
) -> None:
    line = words(
        name=name,
        engine=engine,
        patterns=patterns,
        options=options,
        mode="disabled" if disabled else "enabled",
        fmttriggers=",".join(triggers),
    )
    assert ta.parse_add_format(line) == ta.FormatDef(
        name, engine, patterns or "-", options, not disabled, tuple(triggers)
    )


@given(
    name=idents,
    left=st.integers(0, 9),
    right=st.integers(0, 9),
    file=idents,
    synonyms=st.lists(idents, max_size=3),
    databases=st.lists(st.sampled_from(["dat", "def", "lua"]), unique=True),
    patterns=st.one_of(st.none(), free),
    comment=st.one_of(st.none(), free),
)
def test_parse_add_hyphen_and_database_filter(
    name: str,
    left: int,
    right: int,
    file: str,
    synonyms: list[str],
    databases: list[str],
    patterns: str | None,
    comment: str | None,
) -> None:
    values = {
        "name": name,
        "lefthyphenmin": str(left),
        "righthyphenmin": str(right),
        "file": file,
    }
    if synonyms:
        values["synonyms"] = ",".join(synonyms)
    if databases:
        values["databases"] = ",".join(databases)
    if patterns is not None:
        values["file_patterns"] = patterns
    if comment is not None:
        values["comment"] = comment
    line = words(**values)
    used = tuple(databases) or ("dat", "def", "lua")
    expected = ta.HyphenDef(
        name,
        str(left),
        str(right),
        file,
        tuple(synonyms),
        used,
        patterns,
        None,
        None,
        comment,
    )
    assert ta.parse_add_hyphen(line) == expected
    executes = {"p": [f"AddHyphen {line}"]}
    for database in ("dat", "def", "lua"):
        found = [("p", expected)] if database in used else []
        assert ta.hyphen_defs(executes, database) == found


# a bare "AddFormat"/"AddHyphen" is included: see also the tests below
kinds = st.sampled_from(["AddFormat", "AddHyphen", "addMap", "other", ""])
execute_lines = st.one_of(
    kinds, st.builds(lambda kind, rest: f"{kind} {rest}", kinds, junk)
)
executes_maps = st.dictionaries(idents, st.lists(execute_lines, max_size=4), max_size=3)


@given(executes=executes_maps, database=st.sampled_from(["dat", "def", "lua"]))
def test_defs_raise_only_execute_error(
    executes: dict[str, list[str]], database: str
) -> None:
    with suppress(ta.ExecuteError):
        ta.format_defs(executes)
    with suppress(ta.ExecuteError):
        ta.hyphen_defs(executes, database)


def test_format_defs_bare_add_format() -> None:
    with pytest.raises(ta.ExecuteError):
        ta.format_defs({"p": ["AddFormat"]})


def test_hyphen_defs_bare_add_hyphen() -> None:
    with pytest.raises(ta.ExecuteError):
        ta.hyphen_defs({"p": ["AddHyphen"]}, "dat")


# --- reading the tlpdb --------------------------------------------------------

# characters that str.splitlines() splits at but a tlpdb does not
line_breaks = st.sampled_from("\x0b\x0c\x1c\x1d\x1e\x85" + chr(0x2028) + chr(0x2029))


@FEW
@given(sep=line_breaks)
@example(chr(0x2028))
def test_a_longdesc_cannot_add_execute_lines_or_options(sep: str) -> None:
    forged = ["name evil", "execute addMap evil.map", "depend opt_create_formats:0"]
    text = "name good\nlongdesc x" + sep + sep.join(forged) + "\n\n"
    with tree() as root:
        db = root / "texlive.tlpdb"
        db.write_text(text, encoding="utf-8")
        assert ta.read_executes(db) == {}
        assert ta.read_installation_option(db, "create_formats") is None


# --- pending actions ----------------------------------------------------------

names = st.sets(st.text(max_size=8), max_size=4)
parts = st.text(
    st.characters(codec="utf-8", exclude_characters="/"), min_size=1, max_size=8
)
pendings = st.builds(
    ta._Pending,
    maps=st.booleans(),
    format_config=st.booleans(),
    hyphens=st.booleans(),
    index=st.booleans(),
    formats=st.sets(st.builds("{}/{}".format, parts, parts), max_size=4),
    changed=names,
    new_formats=names,
    scripts=st.lists(
        st.tuples(
            st.text(max_size=8),
            st.sampled_from(["install", "remove"]),
            st.text(max_size=8),
        ),
        max_size=4,
    ),
)


@FEW
@given(pending=pendings)
@example(ta._Pending())
def test_pending_round_trip(pending: ta._Pending) -> None:
    with tree() as root:
        pending.save(root)
        assert (root / ta.PENDING_FILE).exists() == (pending != ta._Pending())
        assert ta._Pending.read(root) == pending


json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(),
        st.sampled_from(["", "a", "a/b", "/", "install"]),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.text(max_size=5), inner, max_size=3),
    ),
    max_leaves=8,
)
keys = ["maps", "format_config", "hyphens", "index", "formats", "changed"]
records = st.fixed_dictionaries(
    {"version": st.sampled_from([1, 2])},
    optional=dict.fromkeys([*keys, "new_formats", "scripts"], json_values),
)
contents = st.one_of(
    st.binary(max_size=40),
    st.one_of(json_values, records).map(lambda v: json.dumps(v).encode()),
)


@FEW
@given(content=contents)
@example(b"\xff")
@example(b"[]")
@example(b"[" * 200_000)
def test_pending_read_raises_only_execute_error(content: bytes) -> None:
    with tree() as root:
        path = root / ta.PENDING_FILE
        path.parent.mkdir()
        path.write_bytes(content)
        try:
            result = ta._Pending.read(root)
        except ta.ExecuteError:
            return
        assert isinstance(result, ta._Pending)


def test_pending_format_name_with_slash() -> None:
    for line in ("name=a/b engine=tex", "name=a engine=t/x"):
        with pytest.raises(ta.ExecuteError):
            ta.parse_add_format(line)


format_words = st.text(st.sampled_from("ab/. -"), min_size=1, max_size=5)


@FEW
@given(name=format_words, engine=format_words)
@example("a/b", "tex")
def test_an_accepted_format_survives_the_pending_file(name: str, engine: str) -> None:
    try:
        f = ta.parse_add_format(words(name=name, engine=engine))
    except ta.ExecuteError:
        assert "/" in name + engine
        return
    pending = ta._Pending(formats={f"{f.name}/{f.engine}"})
    with tree() as root:
        pending.save(root)
        assert ta._Pending.read(root) == pending


@given(executes=executes_maps)
@example({"p": ["AddFormat", "AddHyphen"]})
def test_batch_note_never_raises(executes: dict[str, list[str]]) -> None:
    batch = ta.Batch()
    for name, lines in executes.items():
        batch.note(name, "install", lines, [])
    assert all("/" not in f.name + f.engine for f in batch.new_formats.values())


# --- config files -------------------------------------------------------------

# "\r" is left out: files are read back with universal newlines
line_text = st.text(st.characters(codec="utf-8", exclude_characters="\r"), max_size=15)
file_lines = st.lists(line_text.map(lambda s: s + "\n"), max_size=4)


@FEW
@given(
    body=st.lists(line_text, max_size=4),
    header=st.one_of(st.none(), file_lines),
    local=st.one_of(st.none(), file_lines),
    post=file_lines,
    old=st.one_of(st.none(), line_text),
    comment_char=st.sampled_from(["#", "%", "--"]),
    stamp=st.booleans(),
)
def test_write_config_is_idempotent(
    body: list[str],
    header: list[str] | None,
    local: list[str] | None,
    post: list[str],
    old: str | None,
    comment_char: str,
    stamp: bool,
) -> None:
    def never(*_args: object, **_kwargs: object) -> None:
        pytest.fail("write_config started a process")

    with tree() as root, pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", never)
        paths: dict[str, Path | None] = {"header": None, "local": None}
        for key, lines in (("header", header), ("local", local)):
            if lines is not None:
                path = root / key
                path.write_text("".join(lines), encoding="utf-8")
                paths[key] = path
        dest = root / "web2c" / "updmap.cfg"
        if old is not None:
            dest.parent.mkdir()
            dest.write_text(old, encoding="utf-8")
        ctx = ta._Run(root, False, None, None, None)

        def write() -> bool:
            return ctx.write_config(
                dest,
                paths["header"],
                list(body),
                comment_char,
                stamp=stamp,
                local=paths["local"],
                post=post,
            )

        def snapshot() -> dict[str, bytes]:
            return {p.name: p.read_bytes() for p in dest.parent.iterdir()}

        changed = write()
        first = snapshot()
        written = list(ctx.out.written)
        assert write() is False
        assert snapshot() == first
        assert ctx.out.written == written == ([dest] if changed else [])
        parts = [*(header or []), *body, *(local or []), *post]
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        text = dest.read_text(encoding="utf-8")
        if stamp and changed:
            first_line, text = text.split("\n", 1)
            assert first_line.startswith(f"{comment_char} Generated by texi on ")
        if changed:
            assert text == "".join(parts)
