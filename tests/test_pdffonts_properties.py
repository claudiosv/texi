"""Property tests of PDF content lexing and Type 3 outline conversion."""

import io
import re
import string
from pathlib import Path

import pymupdf
import pytest
from fontTools.pens.basePen import PenError
from fontTools.ttLib import TTFont
from hypothesis import given, settings
from hypothesis import strategies as st
from test_pdffonts import VECTOR_A, VECTOR_B, make_pdf

from texi import fontsub, pdffonts

small = st.integers(-1000, 1000)
matrices = st.tuples(small, small, small, small, small, small)


@given(m=matrices, n=matrices, k=matrices)
def test_mul_is_associative_with_identity(m: tuple, n: tuple, k: tuple) -> None:
    mul = pdffonts._mul
    assert mul(mul(m, n), k) == mul(m, mul(n, k))
    identity = (1, 0, 0, 1, 0, 0)
    assert mul(m, identity) == m == mul(identity, m)


@given(m=matrices, n=matrices, point=st.tuples(small, small))
def test_mul_applies_the_left_matrix_first(m: tuple, n: tuple, point: tuple) -> None:
    def apply(t: tuple, p: tuple) -> tuple:
        a, b, c, d, e, f = t
        return a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f

    assert apply(pdffonts._mul(m, n), point) == apply(n, apply(m, point))


# --- _content_names ----------------------------------------------------------

REGULAR = bytes(b for b in range(33, 127) if b not in b"()<>[]{}/%#")


@st.composite
def pdf_names(draw: st.DrawFn) -> tuple[bytes, bytes]:
    """(as written, decoded): any byte may be written as #xx."""
    written, decoded = b"/", b""
    for byte in draw(st.binary(max_size=6)):
        if byte in REGULAR and draw(st.booleans()):
            written += bytes([byte])
        else:
            written += b"#%02X" % byte
        decoded += bytes([byte])
    return written, decoded


# Strings and comments hold things that look like names and must not count.
string_text = st.lists(
    st.sampled_from([b"/No", b" ", b"x", b"\\(", b"\\)", b"\\\\", b"%", b"<"]),
    max_size=5,
).map(b"".join)
literal = st.recursive(
    string_text,
    lambda inner: st.tuples(inner, inner).map(lambda p: p[0] + b"(" + p[1] + b")"),
    max_leaves=3,
).map(lambda body: (b"(" + body + b")", None))
other = st.sampled_from([
    b"<<",
    b">>",
    b"[",
    b"]",
    b"<4F2F>",
    b"<>",
    b"12",
    b"-.5",
    b"Tf",
    b"Do",
    b"BIx",
]).map(lambda t: (t, None))
comment = string_text.map(lambda body: (b"% /No ( " + body + b"\n", None))
gap = st.sampled_from([b" ", b"\n", b"\r", b"\t", b"\x00", b"\x0c"])
tokens = st.lists(
    st.tuples(st.one_of(pdf_names(), literal, other, comment), gap), max_size=12
)


@given(tokens)
def test_content_names_finds_exactly_the_names(items: list) -> None:
    stream = b"".join(written + space for (written, _), space in items)
    expected = {name for (_, name), _ in items if name is not None}
    assert pdffonts._content_names(stream) == expected


@given(
    st.lists(
        st.one_of(
            st.sampled_from([b"(", b")", b"<", b">", b"/", b"%", b"\\", b"#", b"BI "]),
            st.binary(max_size=4),
        ),
        max_size=20,
    ).map(b"".join)
)
def test_content_names_only_refuses_with_value_error(stream: bytes) -> None:
    try:
        pdffonts._content_names(stream)
    except ValueError as e:
        assert "Cannot prove font unused" in str(e)


# --- glyph names -------------------------------------------------------------

GLYPH_NAME = re.compile(r"[A-Za-z0-9._]+")
valid_names = st.from_regex(GLYPH_NAME, fullmatch=True)


@given(st.text())
def test_glyph_name_is_always_valid_and_stable(raw: str) -> None:
    name = pdffonts._glyph_name(raw)
    assert GLYPH_NAME.fullmatch(name)
    assert pdffonts._glyph_name(name) == name


@given(name=valid_names, data=st.data())
def test_glyph_name_decodes_hex_escapes(name: str, data: st.DataObject) -> None:
    written = "".join(f"#{ord(c):02x}" if data.draw(st.booleans()) else c for c in name)
    assert pdffonts._glyph_name(written) == name


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="_glyph_name maps 'a-b' and 'a_b' to one name; _build_ttf drops a glyph",
)
def test_outline_conversion_keeps_glyphs_with_similar_names(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "t.pdf", {"a-b": VECTOR_A, "a_b": VECTOR_B})
    (font,) = pdffonts.classify(pdf)
    with pymupdf.open(pdf) as doc:
        data, _ = pdffonts._build_ttf(doc, font, [0.001, 0, 0, 0.001, 0, 0])
    # .notdef and one glyph per CharProc.
    assert len(TTFont(io.BytesIO(data)).getGlyphOrder()) == 3


# --- _draw_glyph -------------------------------------------------------------

coord = st.integers(-1000, 1000)
point = st.tuples(coord, coord)


def _op(name: str, *points: tuple[int, int]) -> str:
    return " ".join([*(f"{x} {y}" for x, y in points), name])


segment = st.one_of(
    st.builds(_op, st.just("l"), point),
    st.builds(_op, st.just("c"), point, point, point),
    st.builds(_op, st.just("v"), point, point),
    st.builds(_op, st.just("y"), point, point),
)
# Every subpath starts with m or re. PDF also lets a segment follow h or re
# directly; that fails (see the xfail test), so it is not generated.
moved = st.builds(
    lambda start, segments, end: " ".join([_op("m", start), *segments, end]),
    point,
    st.lists(segment, max_size=5),
    st.sampled_from(["", "h", "h h"]),
)
rectangle = st.builds(_op, st.just("re"), point, point)
subpaths = st.lists(st.one_of(moved, rectangle), max_size=4)


@settings(max_examples=80, deadline=None)
@given(
    paths=subpaths,
    advance=st.integers(0, 2000),
    scale=st.sampled_from([0.001, 0.5, 1.0, 2.0]),
    shift=point,
    paint=st.sampled_from(["f", "f*", "F"]),
)
def test_draw_glyph_replays_valid_paths(
    paths: list[str],
    advance: int,
    scale: float,
    shift: tuple[int, int],
    paint: str,
) -> None:
    stream = " ".join([
        f"{advance} 0 d0",
        f"q 1 0 0 1 {shift[0]} {shift[1]} cm",
        *paths,
        paint,
        "Q",
    ])
    pen, width = pdffonts._draw_glyph(stream.encode(), scale)
    assert width == advance * scale
    assert 0 <= pen.glyph().numberOfContours <= len(paths)


@given(
    x=coord,
    y=coord,
    w=st.integers(1, 1000),
    h=st.integers(1, 1000),
    shift=point,
    scale=st.integers(1, 3),
)
def test_draw_glyph_rectangle_bounds_follow_cm_and_scale(
    x: int, y: int, w: int, h: int, shift: tuple[int, int], scale: int
) -> None:
    dx, dy = shift
    stream = f"q 1 0 0 1 {dx} {dy} cm {x} {y} {w} {h} re f Q"
    glyph = pdffonts._draw_glyph(stream.encode(), float(scale))[0].glyph()
    glyph.recalcBounds(None)
    assert (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax) == (
        (x + dx) * scale,
        (y + dy) * scale,
        (x + dx + w) * scale,
        (y + dy + h) * scale,
    )


@pytest.mark.xfail(
    strict=True,
    raises=PenError,
    reason="_draw_glyph: a segment after h or re does not start a new contour",
)
@pytest.mark.parametrize(
    "stream",
    [b"0 0 m 100 0 l h 200 200 l f", b"0 0 10 10 re 200 200 l f"],
    ids=["after-h", "after-re"],
)
def test_draw_glyph_segment_after_closed_subpath(stream: bytes) -> None:
    pdffonts._draw_glyph(stream, 1.0)[0].glyph()


# --- fontsub -----------------------------------------------------------------

words = st.lists(
    st.text("abcdefXYZ0189", min_size=1, max_size=6), min_size=1, max_size=3
)


@given(
    parts=words,
    tag=st.text(string.ascii_uppercase, min_size=6, max_size=6),
    data=st.data(),
)
def test_font_name_key_ignores_subset_tag_case_and_separators(
    parts: list[str], tag: str, data: st.DataObject
) -> None:
    separator = st.sampled_from(["", " ", "-", "_"])
    spelled = parts[0] + "".join(data.draw(separator) + p for p in parts[1:])
    key = "".join(parts).casefold()
    assert fontsub._name_key(spelled) == key
    assert fontsub._name_key(f"{tag}+{spelled}") == key
    assert fontsub._name_key(spelled.swapcase()) == key
