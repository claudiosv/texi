"""Font substitution must fail closed and leave PDF text geometry intact."""

import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from typer.testing import CliRunner

from texi import fontsub, pdffonts
from texi.cli import app
from test_pdffonts import VECTOR_A, VECTOR_B, make_pdf


def make_font(
    path: Path, names: tuple[str, ...] = ("A", "B"), family: str = "Replacement"
) -> Path:
    fb = FontBuilder(1000, isTTF=True)
    order = [".notdef", *names]
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name != ".notdef":
            pen.moveTo((0, 0))
            pen.lineTo((350, 0))
            pen.lineTo((175, 650))
            pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({ord(n): n for n in names if len(n) == 1})
    fb.setupGlyf(glyphs)
    # Deliberately different from the PDF's 500-unit advances.
    fb.setupHorizontalMetrics({n: (900, 0) for n in order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({
        "familyName": family,
        "styleName": "Regular",
        "psName": family,
        "fullName": family,
    })
    fb.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    fb.setupPost(keepGlyphNames=True)
    fb.save(path)
    return path


def test_substitute_preserves_geometry_and_content(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    face = fontsub.FontFace(make_font(tmp_path / "replacement.ttf"))
    dst = tmp_path / "output.pdf"
    old = pdffonts.find_type3(src)[0]
    with pymupdf.open(src) as doc:
        before = fontsub._text_positions(doc)
        contents = doc[0].read_contents()
        encoding = doc.xref_get_key(old.xref, "Encoding")
    assert fontsub.substitute_font(src, dst, old.name, face) == 2
    assert not pdffonts.find_type3(dst)
    with pymupdf.open(dst) as doc:
        assert fontsub._text_positions(doc) == before
        assert doc[0].read_contents() == contents
        assert doc.xref_get_key(old.xref, "Encoding") == encoding
        assert pdffonts._numbers(pdffonts._resolve(doc, old.xref, "Widths")) == [
            500,
            500,
        ]
    assert pdffonts.find_type3(src)


def test_missing_glyph_does_not_write(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    dst = tmp_path / "output.pdf"
    dst.write_bytes(b"existing output")
    face = fontsub.FontFace(make_font(tmp_path / "replacement.ttf", ("A",)))
    with pytest.raises(ValueError, match="replacement glyph for B"):
        fontsub.substitute_font(src, dst, pdffonts.find_type3(src)[0].name, face)
    assert dst.read_bytes() == b"existing output"


def test_fontbook_exact_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_font(tmp_path / "replacement.ttf")
    monkeypatch.setattr(fontsub.sys, "platform", "darwin")

    def run(*args, **kwargs):
        return SimpleNamespace(
            stdout=json.dumps({
                "SPFontsDataType": [
                    {
                        "path": str(path),
                        "enabled": "yes",
                        "typefaces": [
                            {
                                "_name": "Replacement",
                                "fullname": "Replacement",
                                "enabled": "yes",
                            }
                        ],
                    }
                ]
            })
        )

    monkeypatch.setattr(fontsub.subprocess, "run", run)
    assert fontsub.find_font("ABCDEF+Replacement").path == path
    with pytest.raises(ValueError, match="found 0"):
        fontsub.find_font("Replacement Italic")


def test_cli_substitution_file(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    original = src.read_bytes()
    replacement = make_font(tmp_path / "replacement.ttf")
    result = CliRunner().invoke(
        app,
        [
            "pdf-fonts",
            "substitute",
            str(src),
            pdffonts.find_type3(src)[0].name,
            "--font-file",
            str(replacement),
            "--in-place",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Verified 2 glyph(s)" in result.output
    assert src.with_suffix(".pdf.bak").read_bytes() == original
    assert not pdffonts.find_type3(src)


@pytest.mark.parametrize(
    "stream, kind",
    [
        (b"500 0 d0", "empty"),
        (b"500 0 d0 /Form1 Do", "unknown"),
        (b"500 0 d0 unsupported", "unknown"),
        (b"500 0 d0 0 0 m 10 10 l S", "stroked"),
    ],
)
def test_explicit_classifier(stream: bytes, kind: str) -> None:
    assert pdffonts._glyph_kind(stream) == kind


def test_unknown_is_not_reported_as_bitmap(tmp_path: Path) -> None:
    src = make_pdf(
        tmp_path / "source.pdf", {"A": b"500 0 d0 /Form1 Do", "B": b"500 0 d0"}
    )
    fonts = pdffonts.classify(src)
    assert fonts[0].kind == "unknown"
    result = CliRunner().invoke(app, ["pdf-fonts", "outline", str(src)])
    assert "does not mean bitmap" in " ".join(result.output.split())
    assert "bitmap fonts cannot be converted" not in result.output


def test_bitmap_can_be_substituted(tmp_path: Path) -> None:
    from test_pdffonts import BITMAP

    src = make_pdf(tmp_path / "bitmap.pdf", {"A": BITMAP, "B": BITMAP})
    replacement = make_font(tmp_path / "replacement.ttf")
    out = tmp_path / "replaced.pdf"
    old = pdffonts.find_type3(src)[0]
    assert pdffonts.classify(src)[0].kind == "bitmap"
    assert (
        fontsub.substitute_font(src, out, old.name, fontsub.FontFace(replacement)) == 2
    )
    assert not pdffonts.find_type3(out)


def test_unsupported_matrix_leaves_input_and_output_unchanged(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    old = pdffonts.find_type3(src)[0]
    with pymupdf.open(src) as doc:
        doc.xref_set_key(old.xref, "FontMatrix", "[0.001 0 0.0001 0.001 0 0]")
        doc.saveIncr()
    original = src.read_bytes()
    out = tmp_path / "out.pdf"
    out.write_bytes(b"previous")
    with pytest.raises(ValueError, match="FontMatrix"):
        fontsub.substitute_font(
            src,
            out,
            old.name,
            fontsub.FontFace(make_font(tmp_path / "replacement.ttf")),
        )
    assert src.read_bytes() == original
    assert out.read_bytes() == b"previous"


def test_font_collection_selects_exact_face(tmp_path: Path) -> None:
    from fontTools.ttLib import TTCollection, TTFont

    collection = TTCollection()
    one = TTFont(make_font(tmp_path / "regular.ttf", family="RegularFace"))
    two = TTFont(make_font(tmp_path / "italic.ttf", family="ItalicFace"))
    collection.fonts = [one, two]
    path = tmp_path / "fonts.ttc"
    collection.save(path)
    collection.close()
    assert fontsub.find_font("ItalicFace", path).index == 1
    with pytest.raises(ValueError, match="found 0"):
        fontsub.find_font("MissingFace", path)


def test_private_unicode_cff_glyphs(tmp_path: Path) -> None:
    from fontTools.pens.t2CharStringPen import T2CharStringPen

    names = [".notdef", "uniE001", "uniE002"]
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({0xE001: "uniE001", 0xE002: "uniE002"})
    charstrings = {}
    for name in names:
        pen = T2CharStringPen(500, None)
        if name != ".notdef":
            pen.moveTo((0, 0))
            pen.curveTo((0, 500), (400, 500), (400, 0))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    fb.setupCFF(
        "PrivateItalic",
        {
            "FullName": "PrivateItalic",
            "FamilyName": "PrivateItalic",
            "Weight": "Regular",
        },
        charstrings,
        {},
    )
    fb.setupHorizontalMetrics({name: (500, 0) for name in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "PrivateItalic", "styleName": "Regular"})
    fb.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    fb.setupPost()
    replacement = tmp_path / "private.otf"
    fb.save(replacement)
    src = make_pdf(tmp_path / "private.pdf", {"uniE001": VECTOR_A, "uniE002": VECTOR_B})
    old = pdffonts.find_type3(src)[0]
    with pymupdf.open(src) as doc:
        doc.xref_set_key(
            old.xref, "Encoding", "<</Type/Encoding/Differences[65/uniE001/uniE002]>>"
        )
        doc.saveIncr()
    out = tmp_path / "out.pdf"
    assert (
        fontsub.substitute_font(src, out, old.name, fontsub.FontFace(replacement)) == 2
    )
    assert not pdffonts.find_type3(out)


def test_failed_cli_substitution_does_not_create_backup(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    original = src.read_bytes()
    replacement = make_font(tmp_path / "incomplete.ttf", ("A",))
    result = CliRunner().invoke(
        app,
        [
            "pdf-fonts",
            "substitute",
            str(src),
            pdffonts.find_type3(src)[0].name,
            "--font-file",
            str(replacement),
            "--in-place",
        ],
    )
    assert result.exit_code == 1
    assert src.read_bytes() == original
    assert not src.with_suffix(".pdf.bak").exists()


def test_late_verification_failure_preserves_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    original = src.read_bytes()
    dst = tmp_path / "out.pdf"
    dst.write_bytes(b"existing output")
    real_positions = fontsub._text_positions
    calls = 0

    def changed_positions(doc):
        nonlocal calls
        calls += 1
        return real_positions(doc) if calls == 1 else []

    monkeypatch.setattr(fontsub, "_text_positions", changed_positions)
    with pytest.raises(ValueError, match="positions changed"):
        fontsub.substitute_font(
            src,
            dst,
            pdffonts.find_type3(src)[0].name,
            fontsub.FontFace(make_font(tmp_path / "replacement.ttf")),
        )
    assert src.read_bytes() == original
    assert dst.read_bytes() == b"existing output"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "out.pdf",
        "replacement.ttf",
        "source.pdf",
    ]


def add_empty_font(path: Path) -> str:
    with pymupdf.open(path) as doc:
        font = doc.get_new_xref()
        doc.update_object(
            font,
            "<</Type/Font/Subtype/Type3/BaseFont/EmptyTest"
            "/FirstChar 0/LastChar 255/FontBBox[0 0 0 0]"
            "/FontMatrix[.001 0 0 .001 0 0]/CharProcs<<>>"
            "/Encoding<</Type/Encoding/Differences[]>>"
            "/Widths[" + " ".join(["500"] * 256) + "]>>",
        )
        resource = int(doc.xref_get_key(doc[0].xref, "Resources")[1].split()[0])
        doc.xref_set_key(resource, "Font/Unused", f"{font} 0 R")
        doc.saveIncr()
    return "EmptyTest"


def test_nonzero_undefined_widths_allowed_but_used_codes_rejected(
    tmp_path: Path,
) -> None:
    src = make_pdf(tmp_path / "source.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    font = pdffonts.find_type3(src)[0]
    with pymupdf.open(src) as doc:
        doc.xref_set_key(font.xref, "FirstChar", "0")
        doc.xref_set_key(font.xref, "LastChar", "255")
        doc.xref_set_key(font.xref, "Widths", "[" + " ".join(["500"] * 256) + "]")
        doc.saveIncr()
    face = fontsub.FontFace(make_font(tmp_path / "replacement.ttf"))
    out = tmp_path / "out.pdf"
    assert fontsub.substitute_font(src, out, font.name, face) == 2
    with pymupdf.open(out) as doc:
        assert (
            pdffonts._numbers(pdffonts._resolve(doc, font.xref, "Widths"))
            == [500] * 256
        )
    with pymupdf.open(src) as doc:
        # The width is legal when unused, but code 67 has no explicit mapping.
        content = doc[0].get_contents()[0]
        doc.update_stream(content, doc.xref_stream(content).replace(b"4142", b"414243"))
        doc.saveIncr()
    previous = out.read_bytes()
    with pytest.raises(ValueError, match="Used character code 67"):
        fontsub.substitute_font(src, out, font.name, face)
    assert out.read_bytes() == previous


def test_unused_empty_resource_cleanup_without_font_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make_pdf(tmp_path / "source.pdf", None)
    name = add_empty_font(src)
    assert pdffonts.classify(src)[0].kind == "empty"
    with pymupdf.open(src) as doc:
        before = doc[0].get_pixmap().samples

    def no_lookup(*args, **kwargs):
        raise AssertionError("Empty resource cleanup must not look up a font")

    monkeypatch.setattr(fontsub, "find_font", no_lookup)
    out = tmp_path / "out.pdf"
    result = CliRunner().invoke(
        app, ["pdf-fonts", "substitute", str(src), name, "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "Removed unused empty font resource" in result.output
    assert not pdffonts.find_type3(out)
    with pymupdf.open(out) as doc:
        assert doc[0].get_pixmap().samples == before
    assert pdffonts.find_type3(src)


@pytest.mark.parametrize(
    "location",
    [
        "page",
        "form",
        "pattern",
        "graphics-state",
        "graphics-state-indirect",
        "default-appearance",
    ],
)
def test_empty_resource_reference_blocks_cleanup(tmp_path: Path, location: str) -> None:
    src = make_pdf(tmp_path / "source.pdf", None)
    name = add_empty_font(src)
    font = pdffonts.find_type3(src)[0]
    with pymupdf.open(src) as doc:
        content = b"BT /Un#75sed 12 Tf ET"
        if location == "page":
            xref = doc[0].get_contents()[0]
            doc.update_stream(xref, doc.xref_stream(xref) + b"\n" + content)
        else:
            xref = doc.get_new_xref()
            if location == "default-appearance":
                doc.update_object(xref, "<</DA(BT /Unused 12 Tf ET)>>")
            elif location == "graphics-state-indirect":
                array = doc.get_new_xref()
                doc.update_object(array, f"[{font.xref} 0 R 12]")
                doc.update_object(xref, f"<</Type/ExtGState/Font {array} 0 R>>")
            elif location == "graphics-state":
                doc.update_object(xref, f"<</Type/ExtGState/Font[{font.xref} 0 R 12]>>")
            else:
                kind = "/Subtype/Form" if location == "form" else "/PatternType 1"
                doc.update_object(
                    xref,
                    f"<<{kind}/BBox[0 0 10 10]/Resources<</Font<</Unused {font.xref} 0 R>>>>>>",
                )
                doc.update_stream(xref, content)
        doc.saveIncr()
    out = tmp_path / "out.pdf"
    with pytest.raises(ValueError, match="referenced"):
        fontsub.substitute_font(src, out, name)
    assert not out.exists()


def test_content_name_lexer_skips_strings_and_comments() -> None:
    assert pdffonts._content_names(
        b"% /Unused\n(escaped \\( /Unused nested (string)) <2f556e75736564> "
        b"<< /Actual /Un#75sed >> /Other Do"
    ) == {b"Actual", b"Unused", b"Other"}
    with pytest.raises(ValueError, match="inline images"):
        pdffonts._content_names(b"BI /W 1 ID binary EI")


def test_outline_removes_unused_empty_resource(tmp_path: Path) -> None:
    src = make_pdf(tmp_path / "source.pdf", None)
    add_empty_font(src)
    result = CliRunner().invoke(app, ["pdf-fonts", "outline", str(src)])
    assert result.exit_code == 0, result.output
    assert "Removed unused empty font resource" in result.output
    assert not pdffonts.find_type3(src)
    assert src.with_suffix(".pdf.bak").exists()


def test_matplotlib_lookup_exact_and_deduplicates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    path = make_font(one / "regular.ttf", family="MatplotlibFace")
    shutil.copy2(path, two / "regular.ttf")
    monkeypatch.setattr(fontsub.sys, "platform", "linux")
    monkeypatch.setattr(fontsub, "_matplotlib_font_dirs", lambda: [one, two])
    assert fontsub.find_font("ABCDEF+MatplotlibFace").path == path
    with pytest.raises(ValueError, match="found 0"):
        fontsub.find_font("MatplotlibFace Italic")
    # A different version with the same face name must not be silently chosen.
    from fontTools.ttLib import TTFont

    with TTFont(two / "regular.ttf") as font:
        font["hmtx"].metrics["A"] = (901, 0)
        font.save(two / "regular.ttf")
    with pytest.raises(ValueError, match="found 2"):
        fontsub.find_font("MatplotlibFace")
