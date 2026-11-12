r"""Locate the unclosed argument behind "File ended while scanning use of \\x"."""

from texi import runaway

SRC = (
    "\\documentclass{acmart}\n"
    "\\received{2026-02-15}\n"
    "\\received[accepted]{2026-03-28\n"
    "\n"
    "\\AtBeginDocument{%\n"
    "  \\providecommand\\BibTeX{BibTeX}}\n"
    "\\begin{document}\n"
)


def test_macro_names() -> None:
    assert (
        runaway.macro_of("File ended while scanning use of \\\\received.") == "received"
    )
    assert runaway.macro_of("File ended while scanning use of \\foo.") == "foo"
    assert runaway.macro_of("Paragraph ended before \\@cmd was complete.") == "@cmd"
    assert runaway.macro_of("Undefined control sequence.") is None


def test_locates_the_start_and_the_first_blank_line() -> None:
    stack = ["Runaway argument: {2026-03-28 \\par \\AtBeginDocument {\\providecommand"]
    text = runaway.runaway_text(stack)
    assert text
    assert text.startswith("{2026-03-28")
    found = runaway.locate(text, "received", {"main.tex": SRC})
    assert found is not None
    assert (found.file, found.line, found.blank_line) == ("main.tex", 3, 4)
    hint = found.hint()
    assert "\\received" in hint
    assert "main.tex:3" in hint
    assert "line 4" in hint


def test_picks_the_occurrence_with_the_same_start() -> None:
    # the first \received is closed and starts differently
    found = runaway.locate("{2026-03-28 \\par x", "received", {"a.tex": SRC})
    assert found
    assert found.line == 3
    assert runaway.locate("{nope \\par", "received", {"a.tex": SRC}) is None


def test_without_a_macro_or_a_brace() -> None:
    assert runaway.locate("{2026-03-28 \\par", None, {"a.tex": SRC}).line == 3
    assert runaway.locate("plain text", "received", {"a.tex": SRC}) is None
    assert runaway.runaway_text(["<argument> x"]) is None
