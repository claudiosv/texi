"""Property tests of PDF content lexing and Type 3 outline conversion."""

import io
import re
import string
import tempfile
from pathlib import Path

import pymupdf
import pytest
from fontTools.pens.basePen import PenError
from fontTools.ttLib import TTFont
from hypothesis import example, given, settings
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
    assert name
    # Without a "#" left to decode there is nothing more to change.
    if "#" not in name:
        assert pdffonts._glyph_name(name) == name


# Any byte but NUL, which a PDF name cannot hold; each byte may be written as #xx.
name_bytes = st.lists(st.integers(1, 255), min_size=1, max_size=5).map(bytes)


@given(a=name_bytes, b=name_bytes)
def test_glyph_name_keeps_different_names_apart(a: bytes, b: bytes) -> None:
    names = [pdffonts._glyph_name("".join(f"#{c:02X}" for c in n)) for n in (a, b)]
    assert names == [a.decode("latin-1"), b.decode("latin-1")]


@given(name=valid_names, data=st.data())
def test_glyph_name_decodes_hex_escapes(name: str, data: st.DataObject) -> None:
    written = "".join(f"#{ord(c):02x}" if data.draw(st.booleans()) else c for c in name)
    assert pdffonts._glyph_name(written) == name


def test_outline_conversion_keeps_glyphs_with_similar_names(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "t.pdf", {"a-b": VECTOR_A, "a_b": VECTOR_B})
    (font,) = pdffonts.classify(pdf)
    with pymupdf.open(pdf) as doc:
        data, _ = pdffonts._build_ttf(doc, font, [0.001, 0, 0, 0.001, 0, 0])
    # .notdef and one glyph per CharProc.
    assert len(TTFont(io.BytesIO(data)).getGlyphOrder()) == 3


# Regular PDF name characters, many of them unusual in a font glyph name.
pdf_glyph_names = st.text("abAB019._-+=|:!@*", min_size=1, max_size=6).filter(
    lambda n: n != ".notdef"
)


@settings(max_examples=25, deadline=None)
@given(st.lists(pdf_glyph_names, min_size=2, max_size=2, unique=True))
@example(["x-y", "z+w"])
def test_outline_conversion_keeps_the_page_rendering(names: list[str]) -> None:
    def render(path: Path) -> bytes:
        with pymupdf.open(path) as doc:
            return doc[0].get_pixmap().samples

    def ink(samples: bytes) -> int:
        return sum(v < 128 for v in samples)

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.pdf"
        make_pdf(Path(tmp) / "t.pdf", dict(zip(names, [VECTOR_A, VECTOR_B])))
        with pymupdf.open(Path(tmp) / "t.pdf") as doc:
            (font,) = pdffonts.find_type3(Path(tmp) / "t.pdf")
            doc.xref_set_key(
                font.xref, "Encoding/Differences", "[65/{}/{}]".format(*names)
            )
            doc.save(src)
        report = pdffonts.convert_outlines(src, Path(tmp) / "out.pdf")
        assert len(report.converted) == 1
        before = ink(render(src))
        # Antialiasing may differ a little; a lost glyph loses far more ink.
        assert before > 0
        assert abs(ink(render(report.output)) - before) <= before * 0.1


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
# Every path starts with m or re. PDF also lets a segment follow h or re
# directly, which starts a new subpath at the current point.
segments_after = st.lists(segment, max_size=5)
moved = st.builds(
    lambda start, segments, end, more: " ".join([
        _op("m", start),
        *segments,
        end,
        *(more if end else []),
    ]),
    point,
    segments_after,
    st.sampled_from(["", "h", "h h"]),
    segments_after,
)
rectangle = st.builds(
    lambda rect, more: " ".join([rect, *more]),
    st.builds(_op, st.just("re"), point, point),
    segments_after,
)
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
    # at most two contours per path: the second one after h or re
    assert 0 <= pen.glyph().numberOfContours <= 2 * len(paths)


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


@pytest.mark.parametrize(
    "stream",
    [b"0 0 m 100 0 l h 200 200 l f", b"0 0 10 10 re 200 200 l f"],
    ids=["after-h", "after-re"],
)
def test_draw_glyph_segment_after_closed_subpath(stream: bytes) -> None:
    glyph = pdffonts._draw_glyph(stream, 1.0)[0].glyph()
    # The new subpath runs from the first point of the closed one, (0, 0).
    assert glyph.numberOfContours == 2
    assert list(glyph.coordinates)[-2:] == [(0, 0), (200, 200)]


@pytest.mark.parametrize("op", [b"l", b"c", b"v", b"y"])
def test_draw_glyph_ignores_a_segment_without_a_current_point(op: bytes) -> None:
    glyph = pdffonts._draw_glyph(b"1 2 3 4 5 6 " + op + b" f", 1.0)[0].glyph()
    assert glyph.numberOfContours == 0


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
