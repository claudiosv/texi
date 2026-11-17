"""Public command workflow, source attribution and confirmation safeguards."""

from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

from texi import cli, pdffonts
from texi.config import TexProjectConfig
from test_fontsub import add_empty_font, make_font
from test_pdffonts import BITMAP, VECTOR_A, VECTOR_B, make_pdf


def project(tmp_path: Path, inputs: list[Path]) -> Path:
    (tmp_path / "texproject.toml").write_text("")
    config = TexProjectConfig()
    aux = tmp_path / config.build.aux_dir
    aux.mkdir(parents=True, exist_ok=True)
    recorder = aux / f"{config.jobname}.fls"
    recorder.write_text(f"PWD {tmp_path}\n" + "".join(f"INPUT {p}\n" for p in inputs))
    built = tmp_path / config.build.out_dir / f"{config.jobname}.pdf"
    built.parent.mkdir(parents=True, exist_ok=True)
    return built


def named_font(path: Path, name: str) -> None:
    font = pdffonts.find_type3(path)[0]
    with pymupdf.open(path) as doc:
        doc.xref_set_key(font.xref, "BaseFont", "/" + name)
        doc.xref_set_key(font.xref, "Name", "/" + name)
        doc.saveIncr()


def test_group_help() -> None:
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "--help"])
    assert result.exit_code == 0
    for command in ["check", "outline", "substitute"]:
        assert command in result.output
    assert "rebuild" not in result.output


def test_outline_default_in_place_and_output_override(tmp_path: Path) -> None:
    source = make_pdf(tmp_path / "plot.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    original = source.read_bytes()
    output = tmp_path / "separate.pdf"
    result = CliRunner().invoke(
        cli.app, ["pdf-fonts", "outline", str(source), "-o", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert source.read_bytes() == original
    assert not source.with_suffix(".pdf.bak").exists()
    assert not pdffonts.find_type3(output)
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "outline", str(source)])
    assert result.exit_code == 0, result.output
    assert source.with_suffix(".pdf.bak").read_bytes() == original
    assert not pdffonts.find_type3(source)


def test_substitute_default_in_place(tmp_path: Path) -> None:
    source = make_pdf(tmp_path / "plot.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    original = source.read_bytes()
    font = pdffonts.find_type3(source)[0].name
    replacement = make_font(tmp_path / "font.ttf")
    result = CliRunner().invoke(
        cli.app,
        ["pdf-fonts", "substitute", str(source), font, "--font-file", str(replacement)],
    )
    assert result.exit_code == 0, result.output
    assert source.with_suffix(".pdf.bak").read_bytes() == original
    assert not pdffonts.find_type3(source)


@pytest.mark.parametrize("approve", [True, False])
def test_compact_confirmation_actual_changes_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approve: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    figures = tmp_path / "figures"
    figures.mkdir()
    vector = make_pdf(figures / "plot.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    empty = make_pdf(figures / "empty.pdf", None)
    add_empty_font(empty)
    bitmap = make_pdf(figures / "bitmap.pdf", {"A": BITMAP, "B": BITMAP})
    originals = {p: p.read_bytes() for p in [vector, empty, bitmap]}
    project(tmp_path, [vector, empty, bitmap, vector])

    def confirm(message: str) -> bool:
        assert message == "Apply these changes?"
        assert all(p.read_bytes() == data for p, data in originals.items())
        return approve

    monkeypatch.setattr(cli, "_confirm", confirm)
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "outline"])
    assert result.exit_code == (1 if approve else 0), result.output
    confirmation = result.output.split("Will update", 1)[1]
    assert confirmation.startswith(" 2 PDFs in")
    header = confirmation.split("Backups:", 1)[0].replace("\n", "")
    assert header.count(str(figures)) == 1
    assert "plot.pdf" in confirmation and "empty.pdf" in confirmation
    assert "bitmap.pdf" not in confirmation
    assert "→" not in confirmation and "Backup:" not in confirmation
    assert "Backups: <filename>.bak" in confirmation
    assert "glyphs to convert" in confirmation
    assert "unused empty font" in confirmation
    assert bitmap.read_bytes() == originals[bitmap]
    for path in [vector, empty]:
        assert path.with_suffix(".pdf.bak").exists() == approve
        assert bool(pdffonts.find_type3(path)) != approve
    assert sorted(p.name for p in figures.glob("*.pdf")) == [
        "bitmap.pdf",
        "empty.pdf",
        "plot.pdf",
    ]


def test_change_during_confirmation_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    pdf = make_pdf(tmp_path / "plot.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    project(tmp_path, [pdf])

    def confirm(_: str) -> bool:
        pdf.write_bytes(b"external edit")
        return True

    monkeypatch.setattr(cli, "_confirm", confirm)
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "outline"])
    assert result.exit_code == 1
    assert "changed during preparation" in result.output
    assert pdf.read_bytes() == b"external edit"
    assert not pdf.with_suffix(".pdf.bak").exists()


@pytest.mark.parametrize("renamed", [False, True])
def test_included_font_overrides_pk_name_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, renamed: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    figure = make_pdf(tmp_path / "figure.pdf", {"A": BITMAP, "B": BITMAP})
    named_font(figure, "OriginalFigureFace" if renamed else "ABCDEF+cmr10")
    pk = tmp_path / "cmr10.600pk"
    pk.write_bytes(b"recorded font")
    built = project(tmp_path, [figure, pk])
    with pymupdf.open(figure) as source, pymupdf.open() as final:
        page = final.new_page(width=200, height=100)
        # Imports the font through a nested Form XObject.
        page.show_pdf_page(page.rect, source, 0)
        final.save(built)
    if renamed:
        named_font(built, "cmr10")
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check"])
    assert result.exit_code == 1
    assert "matching font definitions and glyph programs" in " ".join(
        result.output.split()
    )
    assert "included PDF" in result.output and "figure.pdf" in result.output
    assert (
        "cm-super" not in result.output
        and "Use scalable TeX fonts" not in result.output
    )
    assert "native TeX font" not in result.output


def test_native_font_requires_positive_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    pk = tmp_path / "cmr10.600pk"
    pk.write_bytes(b"recorded font")
    built = project(tmp_path, [pk])
    make_pdf(built, {"A": BITMAP, "B": BITMAP})
    named_font(built, "ABCDEF+cmr10")
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check"])
    assert result.exit_code == 1
    assert "native TeX font" in result.output
    assert "cm-super" in result.output
    # Without the recorder, the same font is no longer established as native.
    config = TexProjectConfig()
    (tmp_path / config.build.aux_dir / f"{config.jobname}.fls").unlink()
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check", str(built)])
    assert result.exit_code == 1
    assert "Source: uncertain" not in result.output
    assert "native TeX origin has not been established" not in result.output
    assert "Font origin is uncertain" not in result.output
    assert "cm-super" not in result.output


@pytest.mark.parametrize(
    "scenario", ["same-name-different-glyphs", "missing-pdf", "postscript"]
)
def test_ambiguous_sources_do_not_recommend_tex_fonts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    monkeypatch.chdir(tmp_path)
    pk = tmp_path / "cmr10.pk"
    pk.write_bytes(b"font")
    other = tmp_path / (
        "missing.pdf"
        if scenario == "missing-pdf"
        else "other.eps"
        if scenario == "postscript"
        else "other.pdf"
    )
    if scenario == "same-name-different-glyphs":
        make_pdf(other, {"A": VECTOR_A, "B": VECTOR_B})
        named_font(other, "cmr10")
    elif scenario == "postscript":
        other.write_bytes(b"%!PS")
    built = project(tmp_path, [pk, other])
    make_pdf(built, {"A": BITMAP, "B": BITMAP})
    named_font(built, "cmr10")
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check", str(built)])
    assert result.exit_code == 1
    assert "Source: uncertain" not in result.output
    assert "native TeX origin has not been established" not in result.output
    assert "Font origin is uncertain" not in result.output
    assert "cm-super" not in result.output


def test_standalone_check_is_read_only_and_no_native_advice(tmp_path: Path) -> None:
    source = make_pdf(tmp_path / "plot.pdf", {"A": BITMAP, "B": BITMAP})
    data = source.read_bytes()
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check", str(source)])
    assert result.exit_code == 1
    assert source.read_bytes() == data
    assert "cm-super" not in result.output
    assert not source.with_suffix(".pdf.bak").exists()


@pytest.mark.parametrize("command", ["fix-pdf", "substitute-font"])
def test_removed_commands_are_unavailable(command: str) -> None:
    result = CliRunner().invoke(cli.app, [command, "--help"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_check_groups_subsets_files_pages_and_passing_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    first = make_pdf(tmp_path / "first.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    second = make_pdf(tmp_path / "b.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    named_font(first, "BMQQDV+DejaVuSans")
    named_font(second, "GCWXDV+DejaVuSans")
    clean_one = make_pdf(tmp_path / "clean-one.pdf", None)
    clean_two = make_pdf(tmp_path / "clean-two.pdf", None)
    combined = tmp_path / "a.pdf"
    with (
        pymupdf.open() as doc,
        pymupdf.open(first) as used,
        pymupdf.open(clean_one) as clean,
    ):
        doc.insert_pdf(used)
        doc.insert_pdf(clean)
        doc.insert_pdf(used)
        doc.save(combined)
    result = CliRunner().invoke(
        cli.app,
        [
            "pdf-fonts",
            "check",
            str(combined),
            str(second),
            str(clean_one),
            str(clean_two),
            str(clean_one),
        ],
    )
    assert result.exit_code == 1, result.output
    assert result.output.count("DejaVuSans") == 1
    assert "BMQQDV+" not in result.output and "GCWXDV+" not in result.output
    assert "a.pdf (pages 1, 3), b.pdf (page 1)" in result.output
    assert "glyphs: 2 vector" in result.output
    assert "Checked 2 other PDFs and found no Type 3 fonts" in result.output
    assert "clean-one.pdf" not in result.output and "clean-two.pdf" not in result.output
    assert "Source: uncertain" not in result.output
    assert "native TeX origin has not been established" not in result.output
    assert "Font origin is uncertain" not in result.output
    hint = " ".join(result.output.split())
    assert hint.index("texi pdf-fonts substitute") < hint.index(
        "texi pdf-fonts outline"
    )


@pytest.mark.parametrize("count", [1, 2])
def test_check_all_passing_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    paths = [make_pdf(tmp_path / f"clean-{i}.pdf", None) for i in range(count)]
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check", *map(str, paths)])
    assert result.exit_code == 0, result.output
    plural = "s" if count != 1 else ""
    assert f"Checked {count} PDF{plural} and found no Type 3 fonts" in result.output
    assert "other" not in result.output
    assert all(path.name not in result.output for path in paths)


def test_check_one_hint_after_all_fonts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first = make_pdf(tmp_path / "one.pdf", {"A": VECTOR_A, "B": VECTOR_B})
    second = make_pdf(tmp_path / "two.pdf", None)
    named_font(first, "ABCDEF+FirstFont")
    add_empty_font(second)
    clean = make_pdf(tmp_path / "clean.pdf", None)
    result = CliRunner().invoke(
        cli.app, ["pdf-fonts", "check", str(first), str(second), str(clean)]
    )
    assert result.exit_code == 1, result.output
    output = " ".join(result.output.split())
    assert output.count("Hint:") == 1
    assert output.index("Hint:") > output.index("EmptyTest")
    assert output.index("Hint:") < output.index("Checked 1 other PDF")
    assert "Source: empty resource" not in output
    assert output.count("texi pdf-fonts substitute") == 1
    assert output.count("texi pdf-fonts outline") == 1
    assert (
        "Run texi pdf-fonts substitute to substitute with matching scalable fonts, followed by texi pdf-fonts outline to convert vector glyphs and remove unreferenced resources."
        in output
    )


def test_check_clean_pdfs_have_no_hint(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "clean.pdf", None)
    result = CliRunner().invoke(cli.app, ["pdf-fonts", "check", str(path)])
    assert result.exit_code == 0
    assert "Hint:" not in result.output
