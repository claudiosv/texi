# fix-pdf

```bash
texi fix-pdf [PDF] [--method advise|outline|rebuild]
```

texi can find Type 3 fonts in a PDF and fix or explain them. Venues such as ACM reject Type 3 fonts, because they cannot be checked or substituted like normal fonts. `PDF` defaults to the project's built PDF. The command exits 1 if Type 3 fonts remain.

| option | description |
|---|---|
| `--method`, `-m` | `advise` (default), `outline` or `rebuild`. |
| `--output`, `-o` | Output path for `outline` (default `<name>.fixed.pdf`). |
| `--in-place` | `outline` only: overwrite the PDF, keeping a `.bak` copy. |
| `--debug` | Show every external command texi runs. |

## Methods

`advise`
:   Lists each Type 3 font with its pages, whether it is vector or bitmap, a guess where it came from, and how to fix it.

`outline`
:   Converts vector Type 3 fonts to embedded TrueType. Bitmap fonts cannot be converted.

`rebuild`
:   Checks for Type 1 fonts (cm-super), rebuilds the project and re-checks the PDF.

## Automatic check

`texi build` and `texi preview` warn about Type 3 fonts after building. Turn this off with `build.check_type3 = false` in [`texproject.toml`](../configuration.md#execution). The detection logic is documented in the [pdffonts API](../api/pdffonts.md).
