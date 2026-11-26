"""Detect Type 3 fonts in PDFs and convert vector ones to embedded TrueType.

Pure logic, no CLI. Type 3 fonts are small programs of PDF drawing operators
(one per glyph, the "CharProcs"). Venues such as ACM reject them, because
they cannot be checked or substituted like normal fonts.
"""

import io
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

lazy import pymupdf
lazy from fontTools.agl import toUnicode
lazy from fontTools.fontBuilder import FontBuilder
lazy from fontTools.pens.cu2quPen import Cu2QuPen
lazy from fontTools.pens.ttGlyphPen import TTGlyphPen

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
    kind: str = ""  # vector, bitmap, stroked, empty, mixed or unknown
    glyph_kinds: dict[str, str] = field(default_factory=dict)
    origin: str = ""  # human hint on where it likely came from


@dataclass
class Report:
    output: Path
    converted: list[Type3Font] = field(default_factory=list)
    skipped: list[Type3Font] = field(
        default_factory=list
    )  # left alone, with reason in .kind
    removed: list[Type3Font] = field(default_factory=list)  # empty, unused resources
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
    if _INLINE_IMAGE.search(stream) or _ID_OP.search(stream):
        return "bitmap"
    ops = set(
        re.findall(rb"(?<![A-Za-z0-9/])([A-Za-z][A-Za-z*]*)(?![A-Za-z0-9])", stream)
    )
    if _IMAGE_OP.search(stream):
        return "unknown"  # Do can invoke a Form XObject, not necessarily an image.
    if ops & _STROKE_OPS:
        return "stroked"
    if ops & _FILL_OPS and ops & {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}:
        return "vector"
    return (
        "empty"
        if not (ops - {b"d0", b"d1", b"q", b"Q", b"cm"} - _FILL_OPS)
        else "unknown"
    )


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
            glyph_kinds = {name: _glyph_kind(stream) for name, stream in glyphs.items()}
            kinds = set(glyph_kinds.values()) - {"empty"}
            kind = (
                (
                    "empty"
                    if re.fullmatch(r"<<\s*>>", _resolve(doc, font.xref, "CharProcs"))
                    else "unknown"
                )
                if not glyphs
                else "empty"
                if not kinds
                else next(iter(kinds))
                if len(kinds) == 1
                else "mixed"
            )
            typed = replace(font, kind=kind, glyph_kinds=glyph_kinds)
            out.append(replace(typed, origin=_origin(doc, typed, glyphs)))
    return out


def _content_names(stream: bytes) -> set[bytes]:
    """Lex PDF content names, skipping comments and literal/hex strings.

    This is deliberately conservative: inline images and malformed tokens stop
    unused-font cleanup rather than guessing where binary data ends.
    """
    names: set[bytes] = set()
    whitespace = b"\x00\t\n\f\r "
    delimiters = whitespace + b"()<>[]{}/%"
    index = 0
    while index < len(stream):
        byte = stream[index]
        if byte in whitespace:
            index += 1
        elif byte == ord("%"):
            while index < len(stream) and stream[index] not in b"\r\n":
                index += 1
        elif byte == ord("("):
            depth = 1
            index += 1
            while index < len(stream) and depth:
                byte = stream[index]
                if byte == ord("\\"):
                    index += 2
                    continue
                if byte == ord("("):
                    depth += 1
                elif byte == ord(")"):
                    depth -= 1
                index += 1
            if depth:
                raise ValueError("Cannot prove font unused: unterminated PDF string.")
        elif byte == ord("<") and stream[index : index + 2] != b"<<":
            end = stream.find(b">", index + 1)
            if end < 0:
                raise ValueError("Cannot prove font unused: unterminated hex string.")
            index = end + 1
        elif stream[index : index + 2] == b"<<":
            index += 2
        elif byte == ord("/"):
            end = index + 1
            while end < len(stream) and stream[end] not in delimiters:
                end += 1
            name = re.sub(
                rb"#([0-9a-fA-F]{2})",
                lambda m: bytes([int(m[1], 16)]),
                stream[index + 1 : end],
            )
            names.add(name)
            index = end
        elif byte in b")>[]{":
            index += 1
        else:
            end = index + 1
            while end < len(stream) and stream[end] not in delimiters:
                end += 1
            if stream[index:end] == b"BI":
                raise ValueError(
                    "Cannot prove font unused in content containing inline images."
                )
            index = end
    return names


def _delete_dict_key(doc: pymupdf.Document, owner: int, key: str) -> None:
    """Delete the entry itself, not a PDF null value (rejected by pdfTeX).

    xref_set_key(..., "null") can leave nested font dictionary entries behind.
    Use MuPDF's dictionary API so direct and shared indirect dictionaries both
    lose the key entirely, without editing serialized PDF syntax.
    """
    mu = pymupdf.mupdf
    pdf = mu.PdfDocument(doc.this)
    obj = mu.pdf_load_object(pdf, owner)
    parent_path, _, raw_name = key.rpartition("/")
    parent = mu.pdf_dict_getp(obj, parent_path) if parent_path else obj
    decoded_name = re.sub(
        rb"#([0-9a-fA-F]{2})", lambda m: bytes([int(m[1], 16)]), raw_name.encode()
    ).decode("utf-8")
    mu.pdf_dict_dels(parent, decoded_name)
    mu.pdf_update_object(pdf, owner, obj)


def remove_unused_empty_font(doc: pymupdf.Document, font: Type3Font) -> None:
    """Remove only a truly empty font whose resource names never occur in content.

    Inspect all pages, Form/appearance XObjects, tiling patterns and Type 3
    CharProcs, including unused streams. A same-named resource in another scope
    also blocks removal. Font selections alone count as usage. Interactive
    forms and graphics-state font arrays are refused, not inferred from text
    extraction (which cannot detect all font uses).
    """
    if not re.fullmatch(r"<<\s*>>", _resolve(doc, font.xref, "CharProcs")):
        raise ValueError("Font does not have a verifiably empty CharProcs dictionary.")
    encoding = _resolve(doc, font.xref, "Encoding")
    if (
        not re.search(r"/Differences\s*\[\s*\]", encoding)
        or "/BaseEncoding" in encoding
    ):
        raise ValueError("Empty font has a nonempty or unsupported encoding.")
    if doc.is_repaired or doc.is_encrypted:
        raise ValueError(
            "Cannot prove empty font unused in an encrypted or repaired PDF."
        )
    if doc.xref_get_key(doc.pdf_catalog(), "AcroForm")[0] != "null":
        raise ValueError("Cannot prove empty font unused in an interactive form PDF.")
    references: set[tuple[int, str, str]] = set()
    streams: set[int] = set()
    used_names: set[bytes] = set()
    for page in doc:
        used_names.update(_content_names(page.read_contents()))
    for xref in range(1, doc.xref_length()):
        kind, appearance = doc.xref_get_key(xref, "DA")
        if kind == "string":
            used_names.update(_content_names(appearance.encode("utf-8")))
        elif kind != "null":
            raise ValueError(
                "Cannot prove font unused with an indirect default appearance."
            )
        for path in ("Resources/Font", "Font"):
            kind, value = doc.xref_get_key(xref, path)
            if (
                kind in {"array", "xref"}
                and _resolve(doc, xref, path).lstrip().startswith("[")
                and re.search(
                    rf"(?<![0-9]){font.xref}\s+\d+\s+R", _resolve(doc, xref, path)
                )
            ):
                raise ValueError(
                    "Cannot remove font referenced by a graphics-state font array."
                )
            if kind in {"dict", "xref"}:
                for match in _REF.finditer(_resolve(doc, xref, path)):
                    if int(match[2]) == font.xref:
                        references.add((xref, path, match[1]))
        if doc.xref_is_stream(xref) and (
            doc.xref_get_key(xref, "Subtype")[1] == "/Form"
            or doc.xref_get_key(xref, "PatternType")[1] == "1"
        ):
            streams.add(xref)
        if doc.xref_get_key(xref, "Subtype")[1] == "/Type3":
            streams.update(_charprocs(doc, xref).values())
    for xref in streams:
        used_names.update(_content_names(doc.xref_stream(xref) or b""))
    if not references:
        raise ValueError("Cannot locate empty font's resource references.")
    for _, _, raw_name in references:
        name = re.sub(
            rb"#([0-9a-fA-F]{2})", lambda m: bytes([int(m[1], 16)]), raw_name.encode()
        )
        if name in used_names:
            raise ValueError(
                f"Empty font resource /{raw_name} is referenced in content; left unchanged."
            )
    # Resolve indirect font dictionaries once. Multiple owners may share them.
    edits: set[tuple[int, str]] = set()
    for owner, path, raw_name in references:
        prefix = ""
        for part in path.split("/"):
            prefix = prefix + "/" + part if prefix else part
            kind, value = doc.xref_get_key(owner, prefix)
            if kind == "xref":
                owner = int(value.split()[0])
                prefix = ""
        edits.add((owner, prefix + "/" + raw_name if prefix else raw_name))
    for owner, key in edits:
        _delete_dict_key(doc, owner, key)


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
    start: tuple[float, float] | None = None  # first point of the subpath
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

    def reopen() -> None:
        # After h or re a segment begins a new subpath at the current point.
        nonlocal open_, start
        if not open_ and cur:
            cu.moveTo(pt(*cur))
            start, open_ = cur, True

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
                start = cur
            case b"l" if len(a) >= 2 and cur:
                reopen()
                cu.lineTo(pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"c" if len(a) >= 6 and cur:
                reopen()
                cu.curveTo(pt(a[-6], a[-5]), pt(a[-4], a[-3]), pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"v" if len(a) >= 4 and cur:
                reopen()
                cu.curveTo(pt(*cur), pt(a[-4], a[-3]), pt(a[-2], a[-1]))
                cur = (a[-2], a[-1])
            case b"y" if len(a) >= 4 and cur:
                reopen()
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
                cur = start = (x, y)
            case b"h":
                close()
                cur = start
            case _:
                pass
        nums = []
    close()
    return pen, width


def _glyph_name(raw: str) -> str:
    """PDF name with #xx escapes decoded, as a font glyph name.

    Nothing else is changed: viewers find the glyph of an /Encoding name by this
    name, and different PDF names must stay different glyphs.
    """
    return re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m[1], 16)), raw) or "g"


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
    _embed_ttf(doc, font, data, bbox, matrix)


def _embed_ttf(
    doc: pymupdf.Document,
    font: Type3Font,
    data: bytes,
    bbox: tuple[int, int, int, int],
    matrix: list[float],
) -> None:
    x = font.xref
    base_kind, base = doc.xref_get_key(x, "BaseFont")
    if base_kind != "name":
        base = "/" + "".join(
            chr(b)
            if chr(b)
            in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.+-"
            else f"#{b:02X}"
            for b in font.name.encode("utf-8")
        )

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
            if font.kind == "empty" and not font.glyph_kinds:
                try:
                    remove_unused_empty_font(doc, font)
                except ValueError as e:
                    report.errors[font.xref] = str(e)
                    report.skipped.append(font)
                else:
                    report.removed.append(font)
                continue
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


def suggestion_text(*, native_tex: bool = False) -> list[str]:
    """How to get rid of Type 3 fonts, as plain lines."""
    lines = [
        "ACM does not accept Type 3 fonts.",
        'For matplotlib figures: plt.rcParams["pdf.fonttype"] = 42, then re-export the figure.',
        "Use texi pdf-fonts substitute with a matching scalable font file, or texi pdf-fonts outline for vector glyphs.",
    ]
    if native_tex:
        lines.append(
            "For confirmed native TeX PK fonts: texi add cm-super; use \\usepackage[T1]{fontenc}, then texi build."
        )
    return lines
