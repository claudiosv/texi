"""Property tests of the source scanner and the run-away argument locator."""
# ruff: file-ignore[implicit-namespace-package]

import re
import time

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from texi import runaway, texscan

# Tree tests only run with a cached grammar, so they never download one.
needs_grammar = pytest.mark.skipif(
    not texscan.is_cached(), reason="tree-sitter LaTeX grammar not cached"
)
BACKENDS = ["regex", pytest.param("tree", marks=needs_grammar)]

FRAGMENTS = [
    "{",
    "}",
    "[",
    "]",
    "\\",
    "%",
    " ",
    "\n",
    "\r",
    "a",
    "é",
    "😀",
    "$",
    "#",
    "\\usepackage",
    "\\documentclass",
    "\\LoadClass",
    "\\newcommand",
    "\\begin{",
    "\\end{",
    "verbatim",
    "\\iffalse",
    "\\fi",
    "\\verb|",
    "\0",
    "\\LoadClassWithOptions",
    "\\RequirePackageWithOptions",
]
tex = st.lists(
    st.one_of(st.sampled_from(FRAGMENTS), st.text(max_size=3)), max_size=30
).map("".join)
# a Latin-1 byte right after a backslash, in a source that is then not UTF-8
latin1 = st.integers(0x80, 0xFF).map(lambda b: b"\\" + bytes([b]))
sources_bytes = st.one_of(
    st.binary(max_size=80),
    st.lists(
        st.one_of(tex.map(str.encode), st.binary(min_size=1, max_size=2), latin1),
        max_size=6,
    ).map(b"".join),
)
names = st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True)
macros = st.from_regex(r"[A-Za-z@]{1,12}", fullmatch=True)


def _use(backend: str, mp: pytest.MonkeyPatch) -> None:
    if backend == "regex":
        mp.setattr(texscan, "_parser", lambda: None)


@pytest.mark.parametrize("backend", BACKENDS)
@settings(deadline=None)
@given(src=sources_bytes)
def test_scanning_bytes_never_raises(backend: str, src: bytes) -> None:
    with pytest.MonkeyPatch.context() as mp:
        _use(backend, mp)
        assert isinstance(texscan.has_documentclass(src), bool)
        for name in texscan.loaded_names(src):
            assert re.fullmatch(r"[A-Za-z0-9][\w.+/-]*", name)


@pytest.mark.parametrize("backend", BACKENDS)
@settings(deadline=None)
@given(
    loads=st.lists(
        st.tuples(
            st.sampled_from(["usepackage", "RequirePackage", "LoadClass"]),
            st.sampled_from([
                "",
                "[x]",
                "[a=1,b]",
                " [12pt] ",
                "[opt={a,b}]",
                "% c\n[x]% d ] {no}\n",
                "[a,% ]\n b]",
            ]),
            st.lists(names, min_size=1, max_size=3),
        ),
        max_size=5,
    )
)
def test_loaded_names_finds_every_load(
    backend: str, loads: list[tuple[str, str, list[str]]]
) -> None:
    src = "".join(f"\\{cmd}{opt}{{{', '.join(n)}}}\n" for cmd, opt, n in loads)
    # the regex fallback knows no comments around or inside the options
    assume(backend == "tree" or not any("%" in o for _, o, _ in loads))
    with pytest.MonkeyPatch.context() as mp:
        _use(backend, mp)
        assert texscan.loaded_names(src.encode()) == {
            name for *_, n in loads for name in n
        }


one_line = st.lists(
    st.one_of(
        st.just("\0"),
        st.characters(exclude_characters="\r\n", exclude_categories=["Cs"]),
    ),
    max_size=30,
).map("".join)


@needs_grammar
@settings(deadline=None)
@given(comments=st.lists(one_line, max_size=4), real=names, hidden=names)
def test_tree_does_not_count_comments(
    comments: list[str], real: str, hidden: str
) -> None:
    # The regex fallback does count them; test_texscan.test_regex_fallback pins that.
    src = "".join(f"% {c} \\usepackage{{{hidden}}}\n" for c in comments)
    src += f"\\usepackage{{{real}}}\n"
    assert texscan.loaded_names(src.encode()) == {real}


@pytest.mark.parametrize("backend", BACKENDS)
@settings(deadline=None)
@given(comments=st.lists(one_line, max_size=4), name=names)
def test_documentclass_only_counts_outside_comments(
    backend: str, comments: list[str], name: str
) -> None:
    hidden = "".join(f"% {c} \\documentclass{{{name}}}\n" for c in comments)
    with pytest.MonkeyPatch.context() as mp:
        _use(backend, mp)
        assert not texscan.has_documentclass(hidden.encode())
        real = f"{hidden}\\documentclass{{{name}}}\n"
        assert texscan.has_documentclass(real.encode())


# lone surrogates included: unclosed_braces encodes with surrogatepass
any_tex = st.lists(
    st.one_of(st.sampled_from([*FRAGMENTS, chr(0xDC80)]), st.text(max_size=3)),
    max_size=30,
).map("".join)


@needs_grammar
@settings(deadline=None)
@given(text=any_tex)
def test_unclosed_braces_are_offsets_of_braces(text: str) -> None:
    offsets = texscan.unclosed_braces(text)
    assert offsets is not None
    assert offsets == sorted(set(offsets))
    assert all(text[o] == "{" for o in offsets)


@needs_grammar
@settings(deadline=None)
@given(text=any_tex)
def test_unclosed_braces_counts_characters_not_bytes(text: str) -> None:
    # same parse, same character offsets; only the UTF-8 length of a comment differs
    assert texscan.unclosed_braces("% éé\n" + text) == texscan.unclosed_braces(
        "% ee\n" + text
    )


def test_unclosed_braces_without_a_grammar() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(texscan, "_parser", lambda: None)
        assert texscan.unclosed_braces("{") is None


@given(
    lead=st.sampled_from([
        "File ended while scanning use of",
        "Paragraph ended before",
    ]),
    slashes=st.integers(0, 3),
    gap=st.sampled_from(["", " ", "  "]),
    name=macros,
    tail=st.sampled_from(["", ".", " was complete."]),
)
def test_macro_of_round_trip(
    lead: str, slashes: int, gap: str, name: str, tail: str
) -> None:
    assert runaway.macro_of(f"{lead} {'\\' * slashes}{gap}{name}{tail}") == name


@given(
    before=st.lists(st.text(max_size=10), max_size=3),
    text=st.from_regex(r"[^\r\n]{0,30}", fullmatch=True),
)
def test_runaway_text_returns_the_text_of_the_first_runaway_line(
    before: list[str], text: str
) -> None:
    stack = [*(x for x in before if not x.startswith("Runaway ")), "<argument>"]
    assert runaway.runaway_text(stack) is None
    stack += [f"Runaway argument: {text}", "Runaway text: other"]
    assert runaway.runaway_text(stack) == text.strip()


@pytest.mark.parametrize("backend", BACKENDS)
@settings(deadline=None)
@given(
    runaway_arg=st.one_of(tex, tex.map("{".__add__)),
    macro=st.one_of(st.none(), macros),
    sources=st.dictionaries(st.sampled_from(["a.tex", "b.tex"]), tex),
)
def test_locate_points_into_a_source(
    backend: str, runaway_arg: str, macro: str | None, sources: dict[str, str]
) -> None:
    with pytest.MonkeyPatch.context() as mp:
        _use(backend, mp)
        found = runaway.locate(runaway_arg, macro, sources)
    if found is None:
        return
    lines = sources[found.file].split("\n")
    assert found.snippet == runaway_arg.split(maxsplit=1)[0]
    assert "{" in lines[found.line - 1]
    if found.blank_line is not None:
        assert found.line < found.blank_line <= len(lines)
        assert not lines[found.blank_line - 1].strip(" \t")
    assert found.hint()


@pytest.mark.parametrize("backend", BACKENDS)
@settings(max_examples=50, deadline=None)
@given(
    filler=st.lists(st.sampled_from(["text", "", "% {x", "é"]), max_size=5),
    macro=st.from_regex(r"zz[a-z]{1,8}", fullmatch=True),
    word=st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True),
)
def test_locate_finds_the_open_argument(
    backend: str, filler: list[str], macro: str, word: str
) -> None:
    src = "".join(f"{x}\n" for x in filler) + f"\\{macro}{{{word} x\n\nrest\n"
    with pytest.MonkeyPatch.context() as mp:
        _use(backend, mp)
        found = runaway.locate(f"{{{word} \\par", macro, {"m.tex": src})
    assert found is not None
    assert (found.line, found.blank_line) == (len(filler) + 1, len(filler) + 2)


@needs_grammar
def test_latin1_byte_after_a_backslash() -> None:
    texscan.loaded_names(b"\\\xe9")


@needs_grammar
def test_load_class_with_options() -> None:
    assert texscan.loaded_names(rb"\LoadClass[12pt]{article}") == {"article"}


@needs_grammar
def test_nul_in_a_comment() -> None:
    assert texscan.loaded_names(b"% \0 \\usepackage{hidden}\n") == set()


@needs_grammar
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (rb"\LoadClass [a=1,b] {x}[2020/01/01]", {"x"}),
        (b"\\LoadClass% c\n[12pt]% d\n{article}", {"article"}),
        (rb"\LoadClass[opt={]}][more]{article}", {"article"}),
        (rb"\LoadClass[x] text {prose}", set()),
        (rb"\LoadClass[x]\foo{bar}", set()),
        (b"\\LoadClass" + b"%" * 5000, set()),
        (b"\\LoadClass[" + b"%\n" * 2000, set()),
    ],
)
def test_load_class_option_forms(source: bytes, expected: set[str]) -> None:
    assert texscan.loaded_names(source) == expected


def test_macro_of_many_backslashes_is_fast() -> None:
    # linear: about a millisecond; quadratic: several seconds
    message = "Paragraph ended before " + "\\" * 20_000 + " "
    start = time.process_time()
    assert runaway.macro_of(message) is None
    assert time.process_time() - start < 0.5
