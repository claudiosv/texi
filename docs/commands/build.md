# Build, preview, clean

## `texi build`

Compile the project to PDF with latexmk, using the settings in `texproject.toml`. texi pipes a `.latexmkrc` generated from the config to `latexmk` on every run. texi parses latexmk's log output and prints a pretty summary of errors, warnings and overfull boxes. It also intelligently guesses the line numbers for warnings and errors.

| option | description |
|---|---|
| `--strict` | Stop at the first error instead of pressing on to produce a PDF. |
| `--full` | Rebuild everything, even if latexmk thinks nothing changed (`go_mode = "full-rebuild"`). |
| `--silent`, `-s` | Reduce output to errors and the final status. |
| `--diagnostics` | Show latexmk's internal decisions (very verbose). |
| `--raw` | Show latexmk's full output instead of a cleaned-up summary. |
| `--guess-lines` | Estimate a line (marked `~`) for warnings that TeX gives none. |
| `--rm-pdf` | Delete the old PDF first, so a failed build leaves none behind. |
| `--debug` | Show every external command texi runs, along with its output. |

After a successful build or preview, texi warns if the PDF contains Type 3 fonts (disable with `build.check_type3 = false`). See [fix-pdf](fix-pdf.md).

### Build output

`texi build` hides latexmk's output behind a progress line and, when it finishes, prints what matters from the TeX and biber/bibtex logs: errors with `file:line` and the offending source, warnings (undefined references, missing characters, ...) de-duplicated, and a count of over/underfull boxes. A missing package (`File 'x.sty' not found`) comes with a `texi add x` hint. If a build fails without a parseable error, the tail of latexmk's output is shown.

`--guess-lines` fills in a line (shown as `~12`) for warnings that TeX gives none, using the last line seen earlier in the same file, so treat it as "at or after". `--raw` prints latexmk's complete output instead.

File names for warnings come from tracking `(file` / `)` in the log, which is a heuristic: it can be off when a package prints unbalanced parentheses.

## `texi preview`

Build, open the PDF viewer, and rebuild automatically whenever you save (`latexmk -pvc`). Press Ctrl-C to stop. It takes the same options as `texi build` and prints the same summary after every rebuild, followed by "Watching for changes".

On macOS, if Skim is installed and `build.pdf_previewer` is unset, texi uses Skim. See the viewer keys in the [configuration](../configuration.md#viewers).

## `texi clean`

Delete generated build files (`latexmk -c`). The PDF stays, and the `clean_ext` extensions are removed.

| option | description |
|---|---|
| `--all` | Also delete the PDF and other final outputs (`latexmk -C`). |
| `--debug` | Show every external command texi runs. |
