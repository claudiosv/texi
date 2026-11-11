"""Detect Type 3 fonts in PDFs and convert vector ones to embedded TrueType.

Pure logic, no CLI. Type 3 fonts are small programs of PDF drawing operators
(one per glyph, the "CharProcs"). Venues such as ACM reject them, because
they cannot be checked or substituted like normal fonts.
"""

import io
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import pymupdf
from fontTools.agl import toUnicode
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

UPEM = 1000
_REF = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R")
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
_TOKEN = re.compile(
    rb"/[^\s/<>\[\]()]*|[-+]?(?:\d+\.?\d*|\.\d+)|[A-Za-z][A-Za-z0-9*'\"]*"
)
_INLINE_IMAGE = re.compile(rb"(?<![A-Za-z0-9])BI(?![A-Za-z0-9])")
_ID_OP = re.compile(rb"(?<![A-Za-z0-9])ID(?![A-Za-z0-9])")
_IMAGE_OP = re.compile(rb"(?<![A-Za-z0-9/])Do(?![A-Za-z0-9])|imagemask|/IM\s+true")
_STROKE_OPS = {b"S", b"s", b"B", b"B*", b"b", b"b*"}
_FILL_OPS = {b"f", b"F", b"f*"}


@dataclass
class Type3Font:
    name: str
    xref: int
    pages: list[int] = field(default_factory=list)  # 1-based
    kind: str = ""  # "vector", "bitmap", "stroked" or "unknown" (set by classify)
    origin: str = ""  # human hint on where it likely came from


@dataclass
class Report:
    output: Path
    converted: list[Type3Font] = field(default_factory=list)
    skipped: list[Type3Font] = field(
        default_factory=list
    )  # left alone, with reason in .kind
    remaining: list[Type3Font] = field(default_factory=list)  # Type 3 in the output
    errors: dict[int, str] = field(
        default_factory=dict
    )  # xref -> why conversion failed


def find_type3(pdf: Path) -> list[Type3Font]:
    """Every distinct Type 3 font in the PDF, with the (1-based) pages using it."""
    found: dict[int, Type3Font] = {}
    with pymupdf.open(pdf) as doc:
        for index, page in enumerate(doc, 1):
            for f in page.get_fonts(full=True):
                if f[2] != "Type3":
                    continue
                font = found.setdefault(f[0], Type3Font(name=f[3], xref=f[0]))
                if index not in font.pages:
                    font.pages.append(index)
    return list(found.values())


def _resolve(doc: pymupdf.Document, xref: int, key: str) -> str:
    """Text of a dict-valued key, following one indirect reference."""
    kind, value = doc.xref_get_key(xref, key)
    if kind == "xref":
        return doc.xref_object(int(value.split()[0]))
    return "" if kind == "null" else value


def _charprocs(doc: pymupdf.Document, xref: int) -> dict[str, int]:
    """Glyph name -> content stream xref."""
    return {m[1]: int(m[2]) for m in _REF.finditer(_resolve(doc, xref, "CharProcs"))}


def _glyph_kind(stream: bytes) -> str:
    if (
        _INLINE_IMAGE.search(stream)
        or _ID_OP.search(stream)
        or _IMAGE_OP.search(stream)
    ):
        return "bitmap"
    ops = set(
        re.findall(rb"(?<![A-Za-z0-9/])([A-Za-z][A-Za-z*]*)(?![A-Za-z0-9])", stream)
    )
    if ops & _STROKE_OPS:
        return "stroked"
    if ops & _FILL_OPS and ops & {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}:
        return "vector"
    return "empty"


def _origin(doc: pymupdf.Document, font: Type3Font, glyphs: dict[str, bytes]) -> str:
    producer = (doc.metadata or {}).get("producer", "") or ""
    creator = (doc.metadata or {}).get("creator", "") or ""
    if "matplotlib" in (producer + creator).lower():
        return "likely matplotlib"
    if font.kind == "bitmap":
        return "likely TeX bitmap (PK) fonts via dvips/ps2pdf"
    bare = font.name.split("+")[-1]
    if re.match(r"(DejaVu|Bitstream|STIX|cmr|cmmi|cmsy)", bare) or (
        "space" in glyphs and any(g.startswith("uni") for g in glyphs)
    ):
        return "likely matplotlib"
    return ""


def classify(pdf: Path, fonts: list[Type3Font] | None = None) -> list[Type3Font]:
    """Copies of the Type 3 fonts, with `kind` (vector/bitmap/...) and `origin` filled in."""
    fonts = find_type3(pdf) if fonts is None else fonts
    out: list[Type3Font] = []
    with pymupdf.open(pdf) as doc:
        for font in fonts:
            glyphs = {
                name: doc.xref_stream(x) or b""
                for name, x in _charprocs(doc, font.xref).items()
            }
            kinds = {_glyph_kind(s) for s in glyphs.values()}
            kind = next(
                (k for k in ("bitmap", "stroked", "vector") if k in kinds), "unknown"
            )
            typed = replace(font, kind=kind)
            out.append(replace(typed, origin=_origin(doc, typed, glyphs)))
    return out


# --- outline conversion ------------------------------------------------------


def _mul(m: tuple, n: tuple) -> tuple:
    """Affine product m x n (PDF row-vector convention: apply m first)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def _draw_glyph(stream: bytes, scale: float) -> tuple[TTGlyphPen, float]:
    """Replay a glyph proc's path operators into a quadratic TrueType pen.

    Returns the pen and the advance width, both scaled to UPEM units.
    """
    pen = TTGlyphPen(None)
    cu = Cu2QuPen(pen, max_err=1.0)
    ctm = (scale, 0.0, 0.0, scale, 0.0, 0.0)
    stack: list[tuple] = []
    nums: list[float] = []
    cur: tuple[float, float] | None = None
    open_ = False
    width = 0.0

    def pt(x: float, y: float) -> tuple[int, int]:
        a, b, c, d, e, f = ctm
        return round(a * x + c * y + e), round(b * x + d * y + f)

    def close() -> None:
        nonlocal open_
        if open_:
            cu.closePath()
            open_ = False

    for tok in _TOKEN.findall(stream):
        if tok[:1] == b"/":
            nums.clear()
            continue
        if tok[:1] in b"+-." or tok[:1].isdigit():
            nums.append(float(tok))
            continue
        a = nums
        match tok:
            case b"d0" | b"d1" if len(a) >= 2:
                width = a[0] * scale
            case b"q":
                stack.append(ctm)
            case b"Q":
                ctm = stack.pop() if stack else ctm
            case b"cm" if len(a) >= 6:
                ctm = _mul(tuple(a[-6:]), ctm)
            case b"m" if len(a) >= 2:
                close()
                cu.moveTo(pt(a[-2], a[-1]))
                cur, open_ = (a[-2], a[-1]), True
            case b"l" if len(a) >= 2 and cur:
                cu.lineTo(pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"c" if len(a) >= 6:
                cu.curveTo(pt(a[-6], a[-5]), pt(a[-4], a[-3]), pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"v" if len(a) >= 4 and cur:
                cu.curveTo(pt(*cur), pt(a[-4], a[-3]), pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"y" if len(a) >= 4:
                cu.curveTo(pt(a[-4], a[-3]), pt(a[-2], a[-1]), pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"re" if len(a) >= 4:
                close()
                x, y, w, h = a[-4:]
                cu.moveTo(pt(x, y))
                cu.lineTo(pt(x + w, y))
                cu.lineTo(pt(x + w, y + h))
                cu.lineTo(pt(x, y + h))
                cu.closePath()
                cur = (x, y)
            case b"h":
                close()
            case _:
                pass
        nums = []
    close()
    return pen, width


def _glyph_name(raw: str) -> str:
    """PDF name with #xx escapes decoded, as a valid font glyph name."""
    name = re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m[1], 16)), raw)
    return re.sub(r"[^A-Za-z0-9._]", "_", name) or "g"


def _build_ttf(
    doc: pymupdf.Document, font: Type3Font, matrix: list[float]
) -> tuple[bytes, tuple[int, int, int, int]]:
    scale = matrix[0] * UPEM
    procs = _charprocs(doc, font.xref)
    order = [".notdef"]
    glyphs = {}
    metrics = {".notdef": (UPEM // 2, 0)}
    for raw, x in procs.items():
        name = _glyph_name(raw)
        if name in glyphs or name == ".notdef":
            continue
        pen, width = _draw_glyph(doc.xref_stream(x) or b"", scale)
        glyph = pen.glyph()
        if glyph.numberOfContours:
            glyph.recalcBounds(None)
        glyphs[name] = glyph
        order.append(name)
        metrics[name] = (max(0, round(width)), getattr(glyph, "xMin", 0) or 0)
    glyphs[".notdef"] = TTGlyphPen(None).glyph()

    cmap: dict[int, str] = {}
    for name in order[1:]:
        try:
            uni = toUnicode(name)
        except Exception:
            uni = ""
        if len(uni) == 1 and ord(uni) not in cmap:
            cmap[ord(uni)] = name

    boxes = [
        (g.xMin, g.yMin, g.xMax, g.yMax) for g in glyphs.values() if g.numberOfContours
    ]
    bbox = (
        min((b[0] for b in boxes), default=0),
        min((b[1] for b in boxes), default=-200),
        max((b[2] for b in boxes), default=UPEM),
        max((b[3] for b in boxes), default=800),
    )
    fb = FontBuilder(UPEM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf({n: glyphs[n] for n in order})
    fb.setupHorizontalMetrics({n: metrics[n] for n in order})
    fb.setupHorizontalHeader(ascent=max(bbox[3], 1), descent=min(bbox[1], 0))
    fb.setupNameTable({"familyName": "T3Converted", "styleName": "Regular"})
    fb.setupOS2(
        sTypoAscender=bbox[3],
        sTypoDescender=bbox[1],
        usWinAscent=max(bbox[3], 1),
        usWinDescent=abs(min(bbox[1], 0)),
    )
    fb.setupPost(keepGlyphNames=True)
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue(), bbox


def _numbers(text: str) -> list[float]:
    return [float(n) for n in re.findall(_NUM, text)]


def _convert_font(doc: pymupdf.Document, font: Type3Font) -> None:
    x = font.xref
    matrix = _numbers(_resolve(doc, x, "FontMatrix")) or [0.001, 0, 0, 0.001, 0, 0]
    data, bbox = _build_ttf(doc, font, matrix)
    base = doc.xref_get_key(x, "BaseFont")[1] or f"/{font.name}"
    if not base.startswith("/"):
        base = "/" + base

    file_xref = doc.get_new_xref()
    doc.update_object(file_xref, f"<</Length1 {len(data)}>>")
    doc.update_stream(file_xref, data, compress=True)
    desc_xref = doc.get_new_xref()
    ascent, descent = max(bbox[3], 1), min(bbox[1], 0)
    doc.update_object(
        desc_xref,
        f"<</Type/FontDescriptor/FontName{base}/Flags 32"
        f"/FontBBox[{bbox[0]} {bbox[1]} {bbox[2]} {bbox[3]}]/ItalicAngle 0"
        f"/Ascent {ascent}/Descent {descent}/CapHeight {ascent}/StemV 80"
        f"/FontFile2 {file_xref} 0 R>>",
    )
    widths = _numbers(_resolve(doc, x, "Widths"))
    factor = matrix[0] * 1000
    doc.xref_set_key(
        x, "Widths", "[" + " ".join(f"{w * factor:g}" for w in widths) + "]"
    )
    doc.xref_set_key(x, "Subtype", "/TrueType")
    doc.xref_set_key(x, "BaseFont", base)
    doc.xref_set_key(x, "FontDescriptor", f"{desc_xref} 0 R")
    for key in ("CharProcs", "FontMatrix", "FontBBox", "Resources", "Name"):
        doc.xref_set_key(x, key, "null")


def convert_outlines(src: Path, dst: Path) -> Report:
    """Replace vector Type 3 fonts by embedded TrueType fonts; write the result to `dst`.

    Glyph names, /Encoding and /ToUnicode are kept, so content streams and text
    extraction stay valid. Bitmap (and stroked or empty) fonts are left alone.
    """
    report = Report(output=dst)
    fonts = classify(src)
    with pymupdf.open(src) as doc:
        for font in fonts:
            if font.kind != "vector":
                report.skipped.append(font)
                continue
            try:
                _convert_font(doc, font)
            except Exception as e:  # keep going; the font stays Type 3
                report.errors[font.xref] = f"{type(e).__name__}: {e}"
                report.skipped.append(font)
                continue
            report.converted.append(font)
        doc.save(dst, garbage=4, deflate=True)
    report.remaining = classify(dst)
    return report


def suggestion_text() -> list[str]:
    """How to get rid of Type 3 fonts, as plain lines."""
    return [
        "ACM does not accept Type 3 fonts.",
        '(a) matplotlib: set plt.rcParams["pdf.fonttype"] = 42 and '
        'plt.rcParams["ps.fonttype"] = 42 (or `pdf.fonttype : 42` in matplotlibrc), '
        "then re-export the figure.",
        "(b) bitmap/PK fonts: install Type 1 fonts (tlmgr install cm-super), use "
        "\\usepackage[T1]{fontenc}, and set pdf_mode to pdflatex or lualatex "
        "instead of ps2pdf/dvi.",
        "(c) vector Type 3 fonts in an existing PDF: run texi fix-pdf -m outline.",
    ]
