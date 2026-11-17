"""Conservative replacement of Type 3 fonts with installed or supplied outlines."""

import io
import hashlib
import importlib.util
import os
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

lazy import pymupdf
lazy from fontTools.agl import toUnicode
lazy from fontTools.fontBuilder import FontBuilder
lazy from fontTools.pens.cu2quPen import Cu2QuPen
lazy from fontTools.pens.recordingPen import DecomposingRecordingPen
lazy from fontTools.pens.transformPen import TransformPen
lazy from fontTools.pens.ttGlyphPen import TTGlyphPen
lazy from fontTools.ttLib import TTCollection, TTFont

from texi import pdffonts


@dataclass(frozen=True)
class FontFace:
    path: Path
    index: int = 0


def _name_key(name: str) -> str:
    return re.sub(r"[\s_-]", "", re.sub(r"^[A-Z]{6}\+", "", name)).casefold()


def _face_names(font: TTFont) -> set[str]:
    return {
        _name_key(record.toUnicode())
        for record in font["name"].names
        if record.nameID in {4, 6}
    }


def _faces(path: Path) -> list[FontFace]:
    with path.open("rb") as stream:
        collection = stream.read(4) == b"ttcf"
    if collection:
        with TTCollection(path, lazy=True) as fonts:
            return [FontFace(path, i) for i in range(len(fonts.fonts))]
    return [FontFace(path)]


def open_face(face: FontFace) -> TTFont:
    return TTFont(face.path, fontNumber=face.index)


def _matplotlib_font_dirs() -> list[Path]:
    """Find bundled fonts without importing matplotlib or changing its config."""
    directories: list[Path] = []
    spec = importlib.util.find_spec("matplotlib")
    if spec is not None and spec.origin:
        directories.append(Path(spec.origin).parent / "mpl-data" / "fonts")
    for entry in sys.path:
        directories.append(Path(entry) / "matplotlib" / "mpl-data" / "fonts")
    for environment in {Path(sys.prefix), Path.cwd() / ".venv", Path.cwd() / "venv"}:
        directories.extend(
            environment.glob("lib/python*/site-packages/matplotlib/mpl-data/fonts")
        )
    # uv's unpacked matplotlib wheels are useful even when texi's environment
    # does not itself depend on matplotlib. Never search all user projects.
    caches = {Path.home() / ".cache/uv", Path.home() / "Library/Caches/uv"}
    if os.environ.get("UV_CACHE_DIR"):
        caches.add(Path(os.environ["UV_CACHE_DIR"]))
    for cache in sorted(caches):
        directories.extend(sorted(cache.glob("archive-v*/*/matplotlib/mpl-data/fonts")))
    return list(dict.fromkeys(d.resolve() for d in directories if d.is_dir()))


def _matching_faces(paths: list[Path], wanted: str) -> list[FontFace]:
    matches = []
    seen: set[tuple[bytes, int]] = set()
    for path in sorted(set(paths)):
        for face in _faces(path):
            with open_face(face) as font:
                if wanted not in _face_names(font):
                    continue
            identity = (hashlib.sha256(path.read_bytes()).digest(), face.index)
            if identity not in seen:
                seen.add(identity)
                matches.append(face)
    return matches


def find_font(name: str, font_file: Path | None = None) -> FontFace:
    """Match an exact full/PostScript name in Font Book, then matplotlib bundles.

    A supplied single-face file needs no name match. Collections must match.
    Distinct matching font versions are ambiguous; byte-identical cache copies
    count once. No approximate family or style substitution is performed.
    """
    wanted = _name_key(name)
    if font_file is not None:
        faces = _faces(font_file)
        if len(faces) == 1:
            return faces[0]
        matches = _matching_faces([font_file], wanted)
    else:
        paths = []
        if sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["/usr/sbin/system_profiler", "SPFontsDataType", "-json"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                inventory = json.loads(result.stdout).get("SPFontsDataType", [])
                paths = [
                    Path(item["path"])
                    for item in inventory
                    if item.get("enabled") == "yes"
                    and item.get("path")
                    and any(
                        face.get("enabled") == "yes"
                        and wanted
                        in {_name_key(face.get(k, "")) for k in ("_name", "fullname")}
                        for face in item.get("typefaces", [])
                    )
                ]
            except OSError, subprocess.SubprocessError, ValueError:
                pass  # Font Book unavailable; still try bundled fonts.
        matches = _matching_faces(paths, wanted)
        if not matches:
            paths = [
                p
                for directory in _matplotlib_font_dirs()
                for p in directory.rglob("*")
                if p.suffix.lower() in {".ttf", ".otf", ".ttc"} and p.is_file()
            ]
            matches = _matching_faces(paths, wanted)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one exact font matching {name!r}; found {len(matches)} "
            "in Font Book / matplotlib (or the supplied collection). "
            "Supply --font-file (and --font-name for a collection)."
        )
    return matches[0]


def _encoding_widths(
    doc: pymupdf.Document, font: pdffonts.Type3Font
) -> tuple[dict[str, float], dict[int, str]]:
    x = font.xref
    encoding = pdffonts._resolve(doc, x, "Encoding")
    differences = re.search(r"/Differences\s*\[([^]]*)\]", encoding)
    if differences is None or "/BaseEncoding" in encoding:
        raise ValueError(
            "Only explicit Differences encodings without BaseEncoding are supported."
        )
    first = int(doc.xref_get_key(x, "FirstChar")[1])
    last = int(doc.xref_get_key(x, "LastChar")[1])
    widths = pdffonts._numbers(pdffonts._resolve(doc, x, "Widths"))
    if not 0 <= first <= last <= 255 or len(widths) != last - first + 1:
        raise ValueError("Invalid character range or Widths table.")
    tokens = re.findall(r"/[^\s/<>\[\]()]+|\d+", differences[1])
    if re.sub(r"/[^\s/<>\[\]()]+|\d+|\s+", "", differences[1]):
        raise ValueError("Unsupported Differences syntax.")
    code = None
    mapped: dict[int, str] = {}
    for token in tokens:
        if not token.startswith("/"):
            code = int(token)
            continue
        if code is None or code > 255 or code in mapped:
            raise ValueError("Ambiguous character encoding.")
        mapped[code] = token[1:]
        code += 1
    procs = pdffonts._charprocs(doc, x)
    result: dict[str, float] = {}
    for code in range(first, last + 1):
        raw = mapped.get(code)
        # Width tables may cover unencoded slots. Preserve those widths, but
        # verify actual uses separately rather than demanding zero advances.
        if raw is None:
            continue
        if raw not in procs or raw == ".notdef":
            raise ValueError(f"Character code {code} has no verifiable glyph: {raw}.")
        width = widths[code - first]
        if raw in result and result[raw] != width:
            raise ValueError(f"Conflicting widths for glyph {raw}.")
        result[raw] = width
    if any(code < first or code > last for code in mapped):
        raise ValueError("Encoding entries outside the Widths table are unsupported.")
    return result, mapped


def _replacement_ttf(
    doc: pymupdf.Document,
    old: pdffonts.Type3Font,
    replacement: TTFont,
) -> tuple[bytes, tuple[int, int, int, int], list[float], int, dict[int, int]]:
    matrix = pdffonts._numbers(pdffonts._resolve(doc, old.xref, "FontMatrix"))
    if (
        len(matrix) != 6
        or matrix[0] <= 0
        or matrix != [matrix[0], 0, 0, matrix[0], 0, 0]
    ):
        raise ValueError("Only positive, uniform FontMatrix scaling is supported.")
    if "fvar" in replacement:
        raise ValueError("Supply a static font instance instead of a variable font.")
    flags = getattr(replacement["OS/2"], "fsType") if "OS/2" in replacement else 0
    if flags & (0x0002 | 0x0100 | 0x0200):
        raise ValueError("The font embedding flags prohibit this outline subset.")
    widths, encoding = _encoding_widths(doc, old)
    source = replacement.getGlyphSet()
    cmap = replacement.getBestCmap() or {}
    scale = pdffonts.UPEM / getattr(replacement["head"], "unitsPerEm")
    glyphs = {".notdef": TTGlyphPen(None).glyph()}
    metrics = {".notdef": (0, 0)}
    unicode_map = {}
    verified = 0
    for raw in pdffonts._charprocs(doc, old.xref):
        if raw == ".notdef":
            continue
        name = pdffonts._glyph_name(raw)
        if name in glyphs:
            raise ValueError(f"Glyph name collision: {raw}.")
        try:
            unicode = toUnicode(name)
        except Exception:
            unicode = ""
        by_unicode = cmap.get(ord(unicode)) if len(unicode) == 1 else None
        by_name = (
            name
            if name in source and not re.fullmatch(r"gid\d+|glyph\d+", name)
            else None
        )
        if by_name and by_unicode and by_name != by_unicode:
            raise ValueError(f"Conflicting name and Unicode mappings for {raw}.")
        target = by_name or by_unicode
        if target is None or target == ".notdef":
            raise ValueError(f"No unambiguous replacement glyph for {raw}.")
        # Decompose components before scaling, including CFF cubic outlines.
        recording = DecomposingRecordingPen(source)
        source[target].draw(recording)
        pen = TTGlyphPen(None)
        recording.replay(
            TransformPen(Cu2QuPen(pen, max_err=0.5), (scale, 0, 0, scale, 0, 0))
        )
        glyph = pen.glyph()
        if glyph.numberOfContours:
            glyph.recalcBounds(None)
        elif old.glyph_kinds.get(raw) not in {"empty"} and name not in {
            "space",
            "nbspace",
            "nonbreakingspace",
        }:
            raise ValueError(f"Replacement glyph {raw} is unexpectedly empty.")
        glyphs[name] = glyph
        width = widths.get(raw)
        advance = (
            round(width * matrix[0] * 1000)
            if width is not None
            else round(source[target].width * scale)
        )
        if not 0 <= advance <= 65535:
            raise ValueError(f"Unsupported advance width for {raw}.")
        metrics[name] = (advance, getattr(glyph, "xMin", 0))
        if len(unicode) == 1:
            unicode_map[ord(unicode)] = name
        verified += 1
    for page_number in old.pages:
        for span in doc[page_number - 1].get_texttrace():
            if _name_key(span["font"]) == _name_key(old.name):
                for char in span["chars"]:
                    if char[1] >= 0 and char[1] not in encoding:
                        raise ValueError(
                            f"Used character code {char[1]} has no explicit glyph mapping."
                        )
    if not verified:
        raise ValueError("No glyphs available to verify.")
    boxes = [
        (g.xMin, g.yMin, g.xMax, g.yMax) for g in glyphs.values() if g.numberOfContours
    ]
    bbox = (
        min((b[0] for b in boxes), default=0),
        min((b[1] for b in boxes), default=0),
        max((b[2] for b in boxes), default=0),
        max((b[3] for b in boxes), default=0),
    )
    fb = FontBuilder(pdffonts.UPEM, isTTF=True)
    fb.setupGlyphOrder(list(glyphs))
    fb.setupCharacterMap(unicode_map)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=max(bbox[3], 1), descent=min(bbox[1], 0))
    fb.setupNameTable({
        "familyName": "TexiSubstitution",
        "styleName": "Regular",
        "psName": old.name,
    })
    fb.setupOS2(
        sTypoAscender=max(bbox[3], 1),
        sTypoDescender=min(bbox[1], 0),
        usWinAscent=max(bbox[3], 1),
        usWinDescent=abs(min(bbox[1], 0)),
    )
    fb.setupPost(keepGlyphNames=True)
    buffer = io.BytesIO()
    fb.save(buffer)
    order = list(glyphs)
    encoded_gids = {
        code: order.index(pdffonts._glyph_name(raw)) for code, raw in encoding.items()
    }
    return buffer.getvalue(), bbox, matrix, verified, encoded_gids


def _verify_glyphs(doc: pymupdf.Document, xref: int, encoding: dict[int, int]) -> None:
    """Probe every encoded character, including ones unused on document pages.

    Coverage in a font file alone is insufficient: the PDF Encoding must select
    the intended embedded glyph rather than .notdef or another character.
    """
    with pymupdf.open(stream=doc.tobytes(), filetype="pdf") as probe:
        for code, expected_gid in encoding.items():
            page = probe.new_page()
            resources = probe.get_new_xref()
            probe.update_object(resources, f"<</Font<</Probe {xref} 0 R>>>>")
            probe.xref_set_key(page.xref, "Resources", f"{resources} 0 R")
            contents = probe.get_new_xref()
            probe.update_object(contents, "<<>>")
            probe.update_stream(
                contents, f"BT /Probe 12 Tf 10 100 Td <{code:02x}> Tj ET".encode()
            )
            page.set_contents(contents)
            chars = [
                char
                for span in page.get_texttrace()
                for char in span["chars"]
                if char[1] >= 0
            ]
            if len(chars) != 1 or chars[0][1] != expected_gid:
                raise ValueError(
                    f"Glyph verification failed for character code {code}: PDF encoding selects the wrong glyph."
                )


def _text_positions(doc: pymupdf.Document) -> list:
    """Unicode and origins in painting order; glyph IDs and ink boxes may change."""
    return [
        [
            (
                span["dir"],
                span["size"],
                [(char[0], tuple(char[2])) for char in span["chars"]],
            )
            for span in page.get_texttrace()
        ]
        for page in doc
    ]


def matching_fonts(src: Path, font_name: str) -> list[pdffonts.Type3Font]:
    fonts = pdffonts.classify(src)
    matches = [font for font in fonts if font.name == font_name]
    if not matches:
        matches = [
            font for font in fonts if _name_key(font.name) == _name_key(font_name)
        ]
        if len({font.name for font in matches}) > 1:
            raise ValueError("Multiple subsets match; specify the full PDF font name.")
    if not matches:
        raise ValueError(f"No Type 3 font matching {font_name!r}.")
    return matches


def substitute_font(
    src: Path, dst: Path, font_name: str, face: FontFace | None = None
) -> int:
    """Replace a named Type 3 subset, verifying all mappings before atomic output.

    Keeps content streams, Encoding, ToUnicode and PDF advances; checks Unicode
    and origins after reopening. This verifies mapping and layout, not that the
    supplied font is visually identical to the original font version.
    """
    matches = matching_fonts(src, font_name)
    with pymupdf.open(src) as doc:
        if doc.is_encrypted or doc.is_repaired:
            raise ValueError("Encrypted or repaired PDFs are unsupported.")
        before = _text_positions(doc)
        verified = 0
        removed = set()
        for font in matches:
            if font.kind == "empty" and not font.glyph_kinds:
                pdffonts.remove_unused_empty_font(doc, font)
                removed.add(font.xref)
                continue
            if face is None:
                face = find_font(font_name)
            with open_face(face) as replacement:
                data, bbox, matrix, count, encoding = _replacement_ttf(
                    doc, font, replacement
                )
            pdffonts._embed_ttf(doc, font, data, bbox, matrix)
            _verify_glyphs(doc, font.xref, encoding)
            verified += count
        # Save beside the destination so final replacement is atomic.
        with tempfile.NamedTemporaryFile(
            dir=dst.parent, suffix=".pdf", delete=False
        ) as temp:
            temporary = Path(temp.name)
        try:
            doc.save(temporary, garbage=1, deflate=True)
            with pymupdf.open(temporary) as check:
                if _text_positions(check) != before:
                    raise ValueError(
                        "Verification failed: text or glyph positions changed."
                    )
                if any(
                    check.xref_get_key(font.xref, "Subtype")[1] != "/TrueType"
                    for font in matches
                    if font.xref not in removed
                ):
                    raise ValueError(
                        "Verification failed: font replacement did not persist."
                    )
                if removed & {font.xref for font in pdffonts.find_type3(temporary)}:
                    raise ValueError(
                        "Verification failed: empty font still referenced."
                    )
            temporary.replace(dst)
        finally:
            temporary.unlink(missing_ok=True)
    return verified
