"""No parser may raise on truncated, CRLF, binary or huge input."""

import random
import time

import pytest
from logfixtures import all_fixtures

from texi.logparse import (
    parse_bib,
    parse_blg,
    parse_build,
    parse_index,
    parse_latexmk,
    parse_tex,
    parse_tex_log,
)


def run_all(text: str) -> None:
    parse_tex(text, include_info=True)
    parse_tex(text, max_print_line=79)
    parse_tex_log(text)
    parse_bib(text)
    parse_blg(text, "bibtex")
    parse_index(text)
    parse_latexmk(text)
    parse_latexmk(text, exit_code=12, engine_output=True)
    parse_build(
        tex_log=text, blg=text, ilg=text, latexmk_output=text, latexmk_exit_code=1
    )


FIXTURES = all_fixtures()


def test_fixtures_exist():
    assert len(FIXTURES) > 40


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_truncated_and_crlf(path):
    data = path.read_bytes()
    rng = random.Random(path.name)
    for _ in range(12):
        cut = rng.randrange(len(data) + 1)
        run_all(data[:cut].decode("utf-8", errors="replace"))
    text = data.decode("utf-8", errors="replace")
    run_all(text.replace("\n", "\r\n"))
    run_all(text.replace("\n", "\r"))
    run_all(text)


@pytest.mark.parametrize("path", FIXTURES[::5], ids=lambda p: p.name)
def test_every_prefix_line(path):
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    for k in range(0, len(lines), max(1, len(lines) // 15)):
        run_all(
            "".join(lines[:k])
            + (lines[k][: len(lines[k]) // 2] if k < len(lines) else "")
        )


def test_garbage():
    rng = random.Random(1)
    for _ in range(20):
        run_all(
            bytes(rng.randrange(256) for _ in range(rng.randrange(2000))).decode(
                "latin-1"
            )
        )
    run_all("")
    run_all("\n" * 1000)
    run_all("(" * 5000 + ")" * 10)
    run_all("! " * 3000)


@pytest.mark.parametrize(
    "line",
    [
        "x" * 1_000_000,
        "(" + "a/" * 500_000,
        "! " + "x" * 1_000_000,
        "LaTeX Warning: " + "Reference `a' " * 70_000,
        "Overfull \\hbox (" + "1" * 1_000_000 + "pt too wide)",
        "l." + "9" * 1_000_000,
        "Run number 1 of rule '" + "a" * 1_000_000,
        "Warning--" + "e" * 1_000_000 + "---line 1 of file " + "f" * 1000,
        "[1] " + "A" * 500_000 + "> ERROR - " + "b" * 500_000,
    ],
    ids=range(9),
)
def test_huge_single_line_is_fast(line):
    t = time.perf_counter()
    run_all(line)
    run_all("! Undefined control sequence.\n" + line + "\nl.3 foo\n")
    assert time.perf_counter() - t < 5
