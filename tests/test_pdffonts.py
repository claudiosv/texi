from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

from texi import pdffonts
from texi.cli import app

VECTOR_A = b"500 0 0 0 400 700 d1\n0 0 m\n400 0 l\n200 700 l\nh\nf"
VECTOR_B = b"500 0 0 0 400 700 d1\n0 0 m\n400 0 l\n400 700 l\n0 700 l\nh\nf"
BITMAP = b"500 0 0 0 8 8 d1\nq 8 0 0 8 0 0 cm\nBI /IM true /W 8 /H 8 /BPC 1 ID \xff\x81\x81\x81\x81\x81\x81\xff\nEI Q"


def make_pdf(path: Path, procs: dict[str, bytes] | None) -> Path:
    """One-page PDF with the text "AB" in a hand-made Type 3 font (or in Helvetica)."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    if procs is None:
        page.insert_text((10, 50), "AB", fontname="helv")
    else:
        page.insert_text(
            (10, 50), "AB", fontname="helv"
        )  # creates /Resources + /Contents
        # swap Helvetica for a Type 3 font with the same resource name
        res = page.get_fonts(full=True)[0]
        names = list(procs)
        proc_xrefs = []
        for data in procs.values():
            x = doc.get_new_xref()
            doc.update_object(x, "<<>>")
            doc.update_stream(x, data)
            proc_xrefs.append(x)
        cp = doc.get_new_xref()
        doc.update_object(
            cp,
            "<<" + "".join(f"/{n} {x} 0 R" for n, x in zip(names, proc_xrefs)) + ">>",
        )
        font = res[0]
        doc.update_object(
            font,
            "<</Type/Font/Subtype/Type3/Name/F1/FontBBox[0 0 500 700]"
            "/FontMatrix[0.001 0 0 0.001 0 0]"
            f"/CharProcs {cp} 0 R/FirstChar 65/LastChar 66/Widths[500 500]"
            "/Encoding<</Type/Encoding/Differences[65/A/B]>>>>",
        )
    doc.save(path)
    return path


@pytest.fixture
def vector_pdf(tmp_path: Path) -> Path:
    return make_pdf(tmp_path / "vector.pdf", {"A": VECTOR_A, "B": VECTOR_B})


@pytest.fixture
def bitmap_pdf(tmp_path: Path) -> Path:
    return make_pdf(tmp_path / "bitmap.pdf", {"A": BITMAP, "B": BITMAP})


@pytest.fixture
def plain_pdf(tmp_path: Path) -> Path:
    return make_pdf(tmp_path / "plain.pdf", None)


def test_find_type3(vector_pdf: Path, plain_pdf: Path) -> None:
    fonts = pdffonts.find_type3(vector_pdf)
    assert len(fonts) == 1
    assert fonts[0].pages == [1]
    assert pdffonts.find_type3(plain_pdf) == []


def test_classify(vector_pdf: Path, bitmap_pdf: Path) -> None:
    assert pdffonts.classify(vector_pdf)[0].kind == "vector"
    bitmap = pdffonts.classify(bitmap_pdf)[0]
    assert bitmap.kind == "bitmap"
    assert "bitmap" in bitmap.origin


def test_convert_outlines(vector_pdf: Path, tmp_path: Path) -> None:
    with pymupdf.open(vector_pdf) as doc:
        before = doc[0].get_text()
    assert "AB" in before
    out = tmp_path / "fixed.pdf"
    report = pdffonts.convert_outlines(vector_pdf, out)
    assert len(report.converted) == 1
    assert report.remaining == []
    assert report.errors == {}
    assert pdffonts.find_type3(out) == []
    with pymupdf.open(out) as doc:
        assert doc[0].get_text() == before
        assert doc[0].get_fonts(full=True)[0][2] == "TrueType"
        assert doc[0].get_pixmap(dpi=72).samples.count(0) > 0  # glyphs are drawn


def test_bitmap_left_alone(bitmap_pdf: Path, tmp_path: Path) -> None:
    report = pdffonts.convert_outlines(bitmap_pdf, tmp_path / "out.pdf")
    assert report.converted == []
    assert [f.kind for f in report.skipped] == ["bitmap"]
    assert len(report.remaining) == 1


def test_suggestion_text() -> None:
    text = "\n".join(pdffonts.suggestion_text())
    assert "pdf.fonttype" in text
    assert "cm-super" in text
    assert "fix-pdf" in text


def test_cli_advise(vector_pdf: Path) -> None:
    result = CliRunner().invoke(app, ["fix-pdf", str(vector_pdf)])
    assert result.exit_code == 0, result.output
    assert "Type 3" in result.output
    assert "vector" in result.output
    assert "pdf.fonttype" in result.output


def test_cli_outline(vector_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.pdf"
    result = CliRunner().invoke(
        app, ["fix-pdf", str(vector_pdf), "-m", "outline", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert pdffonts.find_type3(out) == []


def test_cli_outline_bitmap_fails(bitmap_pdf: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["fix-pdf", str(bitmap_pdf), "-m", "outline", "-o", str(tmp_path / "o.pdf")],
    )
    assert result.exit_code == 1


def test_cli_in_place(vector_pdf: Path) -> None:
    result = CliRunner().invoke(
        app, ["fix-pdf", str(vector_pdf), "-m", "outline", "--in-place"]
    )
    assert result.exit_code == 0, result.output
    assert pdffonts.find_type3(vector_pdf) == []
    assert len(pdffonts.find_type3(vector_pdf.with_suffix(".pdf.bak"))) == 1


def test_warn_type3(
    vector_pdf: Path, plain_pdf: Path, capsys: pytest.CaptureFixture
) -> None:
    from texi.cli import _warn_type3

    _warn_type3(plain_pdf)
    assert capsys.readouterr().out == ""
    _warn_type3(vector_pdf)
    assert "Type 3 font" in capsys.readouterr().out
    bad = vector_pdf.with_name("bad.pdf")
    bad.write_bytes(b"not a pdf")
    _warn_type3(bad)  # must not raise


MPL_FIXTURE = Path(__file__).parent / "fixtures" / "mpl_type3.pdf"


def test_real_matplotlib_pdf(tmp_path: Path) -> None:
    """Figure saved by matplotlib with pdf.fonttype=3."""
    fonts = pdffonts.classify(MPL_FIXTURE)
    assert fonts
    assert all(f.kind == "vector" for f in fonts)
    assert any("matplotlib" in f.origin for f in fonts)
    with pymupdf.open(MPL_FIXTURE) as doc:
        before = doc[0].get_text()
    assert "Hello type3" in before
    out = tmp_path / "fixed.pdf"
    report = pdffonts.convert_outlines(MPL_FIXTURE, out)
    assert report.remaining == []
    assert pdffonts.find_type3(out) == []
    with pymupdf.open(out) as doc:
        assert doc[0].get_text() == before
