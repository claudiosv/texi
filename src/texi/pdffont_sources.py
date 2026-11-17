"""Conservative build provenance for Type 3 fonts and actionable advice."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from texi import latexmk_files, pdffonts


@dataclass
class BuildInputs:
    pdf: Path
    inputs: set[Path]

    @classmethod
    def read(cls, fls_path: Path, pdf: Path, base: Path) -> "BuildInputs":
        fls = latexmk_files.load_fls(fls_path)
        if not fls.pwd:
            fls.pwd = str(base)
        return cls(pdf.resolve(), {Path(p).resolve() for p in fls.resolved_inputs()})

    @property
    def pdfs(self) -> list[Path]:
        return sorted(
            p for p in self.inputs if p.suffix.lower() == ".pdf" and p != self.pdf
        )


@dataclass(frozen=True)
class Source:
    kind: str  # included, native, empty, unknown
    files: tuple[Path, ...] = ()
    reason: str = ""


def _bare(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name).casefold()


def _font_names(doc: pymupdf.Document, font: pdffonts.Type3Font) -> frozenset[str]:
    names = {_bare(font.name)}
    for key in ("BaseFont", "FontDescriptor/FontName"):
        value = doc.xref_get_key(font.xref, key)[1]
        if value.startswith("/"):
            names.add(_bare(value[1:]))
    return frozenset(names)


def _glyph_evidence(
    doc: pymupdf.Document, font: pdffonts.Type3Font
) -> dict[tuple, bytes]:
    """Compare self-contained glyph programs, matrices, names and encoded widths.

    Exact decompressed bytes are intentionally conservative. Resource-dependent
    programs cannot be attributed by comparing their streams alone.
    """
    matrix = tuple(pdffonts._numbers(pdffonts._resolve(doc, font.xref, "FontMatrix")))
    encoding = pdffonts._resolve(doc, font.xref, "Encoding")
    first = doc.xref_get_key(font.xref, "FirstChar")[1]
    widths = tuple(pdffonts._numbers(pdffonts._resolve(doc, font.xref, "Widths")))
    if not matrix or "/BaseEncoding" in encoding:
        return {}
    # The encoding/width fingerprint also prevents equal paths with different
    # character semantics from being considered the same font definition.
    definition = (matrix, re.sub(r"\s+", "", encoding), first, widths)
    result = {}
    for name, xref in pdffonts._charprocs(doc, font.xref).items():
        stream = doc.xref_stream(xref) or b""
        if re.search(
            rb"(?<![A-Za-z0-9/])(?:Do|gs|Tf|sh|scn|SCN)(?![A-Za-z0-9])", stream
        ):
            return {}
        result[(definition, name)] = hashlib.sha256(stream).digest()
    return result


class FontSources:
    """Scan recorded PDF inputs once; missing evidence never implies native TeX."""

    def __init__(self, build: BuildInputs | None):
        self.build = build
        self.complete = build is not None
        self.index: list[tuple[Path, frozenset[str], dict]] = []
        if build is None:
            return
        # External PostScript sources can also bring fonts into a TeX build.
        if any(p.suffix.lower() in {".eps", ".ps"} for p in build.inputs):
            self.complete = False
        for path in build.pdfs:
            try:
                fonts = pdffonts.find_type3(path)
                with pymupdf.open(path) as doc:
                    for font in fonts:
                        evidence = _glyph_evidence(doc, font)
                        if not evidence and pdffonts._charprocs(doc, font.xref):
                            self.complete = False
                        self.index.append((path, _font_names(doc, font), evidence))
            except Exception:
                self.complete = False

    def attribute(
        self, pdf: Path, fonts: list[pdffonts.Type3Font]
    ) -> dict[int, Source]:
        pdf = pdf.resolve()
        result = {}
        with pymupdf.open(pdf) as doc:
            for font in fonts:
                if font.kind == "empty" and not font.glyph_kinds:
                    result[font.xref] = Source(
                        "empty", reason="empty font resource; no glyph programs"
                    )
                    continue
                if self.build is not None and pdf in self.build.pdfs:
                    result[font.xref] = Source(
                        "included", (pdf,), "PDF input recorded by TeX"
                    )
                    continue
                evidence = _glyph_evidence(doc, font)
                files = set()
                covered = set()
                possible = False
                names = _font_names(doc, font)
                for path, candidate_names, candidate in self.index:
                    possible = possible or bool(names & candidate_names)
                    overlap = {
                        key
                        for key, value in evidence.items()
                        if candidate.get(key) == value
                    }
                    if overlap:
                        covered.update(overlap)
                        files.add(path)
                if evidence and covered == set(evidence):
                    result[font.xref] = Source(
                        "included",
                        tuple(sorted(files)),
                        "matching font definitions and glyph programs",
                    )
                    continue
                if files:
                    result[font.xref] = Source(
                        "unknown",
                        tuple(sorted(files)),
                        "only some glyph programs match included PDFs",
                    )
                    continue
                # Positive native evidence: this is the project output, every
                # PDF input was inspected, no source font could explain it,
                # and its name agrees with a recorded TeX PK input.
                pk_files = []
                if self.build is not None:
                    for path in self.build.inputs:
                        if re.search(r"(?:\.\d+)?\.?pk$", path.name, re.I):
                            name = re.sub(
                                r"(?:\.\d+)?\.?pk$", "", path.name, flags=re.I
                            )
                            if _bare(name) in names:
                                pk_files.append(path)
                if (
                    self.build is not None
                    and pdf == self.build.pdf
                    and self.complete
                    and not possible
                    and font.kind == "bitmap"
                    and pk_files
                ):
                    result[font.xref] = Source(
                        "native",
                        tuple(sorted(pk_files)),
                        "font name matches a recorded TeX PK input",
                    )
                else:
                    result[font.xref] = Source(
                        "unknown", reason="native TeX origin has not been established"
                    )
        return result


def advice(source: Source, font: pdffonts.Type3Font) -> list[str]:
    """Scalable TeX font advice is restricted to positively attributed native fonts."""
    if source.kind == "native":
        lines = ["Use scalable TeX fonts for this document, then run texi build."]
        if any(
            re.match(r"(?:cm[a-z]|ec[a-z]|tc[a-z]|sfrm)", path.name, re.I)
            for path in source.files
        ):
            lines.append(
                "For Computer Modern/EC fonts: texi add cm-super; use \\usepackage[T1]{fontenc}."
            )
        lines.append(
            "Prefer pdflatex or lualatex to the dvips/ps2pdf bitmap-font path."
        )
        return lines
    if source.kind == "empty":
        return [
            "texi pdf-fonts outline can remove this resource if no content references it."
        ]
    lines = []
    if source.kind == "included":
        lines.append(
            "Re-export or repair the included PDF; changing the document’s TeX fonts will not repair it."
        )
        if "matplotlib" in font.origin:
            lines.append(
                'matplotlib: plt.rcParams["pdf.fonttype"] = 42; re-export the figure.'
            )
    else:
        lines.append(
            "Font origin is uncertain; inspect the source before choosing a font replacement."
        )
    lines.append(
        "Use texi pdf-fonts outline for vector glyphs, or texi pdf-fonts substitute with a matching scalable font file."
    )
    return lines
