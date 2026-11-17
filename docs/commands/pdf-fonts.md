# PDF fonts

```bash
texi pdf-fonts check [PDF ...]
texi pdf-fonts outline [PDF ...]
texi pdf-fonts substitute PDF FONT [--font-file PATH | --font-name NAME]
```

Type 3 fonts are rejected by venues such as ACM. These commands inspect their source, convert supported glyph outlines, or replace a font using a matching scalable font file.

## Check

`check` reports each Type 3 font's name, pages, classification, glyph counts and source evidence. It is read-only and exits 1 when Type 3 fonts remain or a PDF cannot be inspected; otherwise it exits 0.

With no paths, texi reads the build's `.fls` recorder, checks the built project PDF when present, and checks its included PDF inputs. Explicit paths can be checked without a project. When a project recorder is available, texi uses it for source attribution even with explicit paths.

Advice depends on evidence:

- **Included PDF:** report the source figure and recommend re-exporting or repairing it. For matplotlib figures, use `plt.rcParams["pdf.fonttype"] = 42` before re-exporting. Changing the document's TeX fonts does not repair an included figure.
- **Native TeX font:** recommend scalable TeX fonts and rebuilding only when the font is in the built document, its name matches a recorded TeX PK input, and the PDF input scan can rule out an included source. An included match takes precedence over a matching PK name.
- **Uncertain:** report the uncertainty without recommending scalable TeX fonts. A missing recorder, unreadable PDF inputs, external PostScript inputs, a name-only match or an unsupported glyph definition does not establish native origin.

Included-font attribution compares font definitions, character encoding, advance widths, matrices and decompressed glyph programs, not just names. Matching is intentionally conservative; rewritten or resource-dependent glyph programs can remain uncertain.

`unknown` means unrecognized glyph drawing instructions, **not bitmap**. Other classifications are `vector`, `bitmap`, `stroked`, `empty` and `mixed`. An empty glyph dictionary is reported explicitly.

## Outline

```bash
# Update one or more explicit PDFs, saving <filename>.bak backups.
texi pdf-fonts outline figure.pdf other.pdf

# Discover included PDFs from .fls, preview actual changes and confirm.
texi pdf-fonts outline

# Write a separate output instead.
texi pdf-fonts outline figure.pdf -o figure.fixed.pdf
texi pdf-fonts outline figure.pdf other.pdf --no-in-place
```

`outline` converts vector glyphs to embedded TrueType and removes confirmed unused font resources whose glyph dictionary and encoding are empty. Unsupported glyphs get specific explanations. It exits 1 if Type 3 fonts remain or conversion fails.

**In-place is the default**, with `<filename>.bak` backups. `--output` overrides in-place behavior and requires one PDF. `--no-in-place` writes `<name>.fixed.pdf` for each changed PDF. Inputs with no supported changes are left untouched.

Without explicit paths, texi stages conversions before asking for confirmation. It lists only files that actually have changes, showing a shared directory once, relative filenames and action summaries. Backups are described once; source → destination appears only when paths differ. Declining leaves inputs untouched. If an input changes during preparation or confirmation, the command aborts before applying the staged files.

```text
Will update 3 PDFs in /path/to/figures
Backups: <filename>.bak

auc.pdf       56 glyphs to convert
codex.pdf     remove 1 unused empty font(s)
postage.pdf   46 glyphs to convert

Apply these changes? [y/N]
```

Empty-resource cleanup lexes pages, default appearances, Form and appearance XObjects, tiling patterns and Type 3 glyph programs. Any reference to the resource name blocks removal, including a font selection with no painted text. Ambiguous cases, interactive forms, graphics-state font arrays and inline images are left unchanged with an explanation.

## Substitute

```bash
# Find the matching font automatically; update the PDF with a .bak backup.
texi pdf-fonts substitute figure.pdf 'EUPODS+STIXNonUnicode-Italic'

# Choose another exact installed face, or supply a font file.
texi pdf-fonts substitute figure.pdf 'BMQQDV+DejaVuSans' --font-name 'DejaVu Sans'
texi pdf-fonts substitute figure.pdf 'EUPODS+STIXNonUnicode-Italic' --font-file STIXNonUniIta.otf

# Preserve the input and write a separate output.
texi pdf-fonts substitute figure.pdf FONT -o figure.substituted.pdf
```

`substitute` replaces a named Type 3 font, including bitmap or unknown fonts when their mappings can be verified. The subset prefix is optional when unambiguous. It also removes confirmed unused empty resources without needing a font file.

**In-place is the default**, with a `.bak` backup created after verification. `--output` writes separately; `--no-in-place` uses `<name>.substituted.pdf`.

Automatic lookup tries enabled macOS Font Book faces, then matplotlib bundles in installed Python environments, project virtual environments and local uv wheel caches (including `UV_CACHE_DIR`). It requires an exact full or PostScript name. Identical cached copies count once; distinct matching versions require `--font-file`. Matplotlib discovery works on other platforms without importing matplotlib. Use `--font-name` to select an unambiguous face from a collection.

The command verifies every declared glyph mapping, rejects missing or conflicting glyphs, and probes each encoded character through the PDF renderer to check that the intended embedded glyph is selected. It preserves character codes, Unicode mappings, content streams and advance widths, then reopens the output to check text and glyph positions. Undefined, unused character slots may have nonzero widths; those widths are retained. A used code still requires an explicit mapping.

Supported replacements use explicit Differences encodings and positive uniform FontMatrix scaling. Unsupported encodings, transforms, variable fonts or embedding restrictions cause an error. Failed verification leaves the input and existing output unchanged. Verification establishes mapping and layout preservation, not identical artwork across font versions; visually inspect the result when replacing a font or version.

## Rebuilding

Use `texi build` to rebuild the document after changing native TeX fonts or repairing included figures. PDF font operations are available only through `texi pdf-fonts check`, `outline` and `substitute`.

## Automatic build check

`texi build` and `texi preview` warn about Type 3 fonts and scan included PDFs from the `.fls` recorder. Run `texi pdf-fonts check` for source attribution and appropriate advice. Disable the automatic warning with `build.check_type3 = false` in [`texproject.toml`](../configuration.md#execution).
