# fix-pdf

```bash
texi fix-pdf [PDF ...] [--method advise|outline|rebuild]
```

texi can find Type 3 fonts in a PDF and fix or explain them. Venues such as ACM reject Type 3 fonts, because they cannot be checked or substituted like normal fonts. Pass one or more PDFs. With no paths, texi scans the PDF inputs listed in the project’s `.fls` build recorder. Before applying fixes to discovered files, it lists the source files, destinations and backups and asks for confirmation. The default `advise` method only reports findings. The command exits 1 if Type 3 fonts remain.

| option | description |
|---|---|
| `--method`, `-m` | `advise` (default), `outline` or `rebuild`. |
| `--output`, `-o` | Output path for `outline` (default `<name>.fixed.pdf` for each input); requires exactly one PDF. |
| `--in-place` | `outline` only: overwrite the PDF, keeping a `.bak` copy. |
| `--debug` | Show every external command texi runs. |

## Methods

`advise`
:   Lists each Type 3 font with its pages, its classification (vector, bitmap, stroked, empty, mixed or unknown), a guess where it came from, and how to fix it.

`outline`
:   Converts vector Type 3 fonts to embedded TrueType and removes confirmed unused fonts with empty glyph dictionaries. Bitmap outlines are unsupported; bitmap glyphs may instead be substituted using a matching scalable font file.

`rebuild`
:   Checks for Type 1 fonts (cm-super), rebuilds the project and re-checks the PDF.

## Automatic check

`texi build` and `texi preview` warn about Type 3 fonts after building. When Type 3 fonts are found, texi also scans the included PDFs listed in the build’s `.fls` recorder file and reports each affected file and its font names. Turn this off with `build.check_type3 = false` in [`texproject.toml`](../configuration.md#execution). The detection logic is documented in the [pdffonts API](../api/pdffonts.md).

## Font substitution

```bash
# Find the exact enabled face in macOS Font Book (subset prefix is optional).
texi substitute-font figure.pdf 'EUPODS+STIXNonUnicode-Italic'

# Select another installed full/PostScript font name.
texi substitute-font figure.pdf 'BMQQDV+DejaVuSans' --font-name 'DejaVu Sans'

# Supply a scalable font file directly.
texi substitute-font figure.pdf 'EUPODS+STIXNonUnicode-Italic' --font-file STIXNonUniIta.otf
```

`substitute-font` replaces one named Type 3 font, including bitmap or unknown fonts when their glyph mappings can be verified. The default replacement name is the PDF font name without its subset prefix. Automatic lookup first searches enabled Font Book faces on macOS, then matplotlib’s bundled fonts in installed Python environments, the project’s virtual environment, and local uv wheel caches (including `UV_CACHE_DIR`). It requires an exact full or PostScript name; it does not choose an approximate family or style. Byte-identical cached copies count once; distinct matching versions require `--font-file`. Matplotlib discovery also works on other platforms without importing matplotlib.

The output defaults to `<name>.substituted.pdf`. Use `-o` to choose another output, or `--in-place` to replace the input after verification and keep `<name>.pdf.bak`. A font collection requires an unambiguous matching face; select it with `--font-name`.

If the selected font has an empty glyph dictionary and empty encoding, the command can remove its unused resource entries without locating a font file. It lexes page content, default appearance instructions, Form and appearance XObjects, tiling patterns, and Type 3 glyph programs, handling PDF strings, comments and escaped names. Any occurrence of a resource name blocks removal, even a font selection with no painted text. Interactive forms, font references in graphics-state arrays, inline images and ambiguous cases are left unchanged with an explanation. `fix-pdf -m outline` performs the same cleanup.

The command verifies every declared glyph against the replacement's glyph names and Unicode map, rejects missing or conflicting mappings, and probes every encoded character through the PDF renderer to ensure the intended embedded glyph is selected. It preserves content streams, character codes, Unicode mappings, PDF advance widths and positioning instructions, then reopens the result to check text and glyph origins. Undefined, unused character slots may have nonzero widths; their widths are retained. A used character code still needs an explicit glyph mapping. A failed verification leaves the input and existing output unchanged.

Supported inputs use an explicit Differences encoding and a positive uniform FontMatrix scale. Unsupported encodings or transforms, variable fonts, and font embedding restrictions produce an error. Verification establishes glyph mapping and layout preservation; it cannot establish that a supplied font version has identical artwork. Check the resulting figure visually when using a different font or version.

`unknown` means unrecognized glyph drawing instructions, **not bitmap**. `empty` means an empty glyph dictionary or glyph programs with no recognized painted content, `mixed` means different glyph classifications within a font, and `stroked` means stroked paths. These cases have separate diagnostics rather than a generic bitmap warning.
