# Log fixtures

Ground truth for `tests/test_logparse_*.py`. Run `generate.sh` to rebuild the
real ones (it works in `/tmp/texi_corpus/work` and copies the results here).

Real output of TeX Live 2026 basic (pdfTeX 1.40.29, XeTeX 0.999998, LuaHBTeX 1.24,
BibTeX 0.99e, makeindex 2.18, Biber 2.22, Latexmk 4.89) with
`max_print_line=100000 error_line=254 half_error_line=238` (except `*_wrapped79.log`,
which keeps the 79-column default). `-file-line-error` is on unless the name ends in
`_nofle`.

| file | what |
|---|---|
| `pdflatex_undefcs.log`, `_nofle` | undefined control sequence, missing $ |
| `pdflatex_missingfile.log`, `pdflatex_missingpkg*.log` | file / package not found |
| `pdflatex_runaway.log`, `pdflatex_fileended.log`, `pdflatex_emergency*.log` | runaway argument, File ended, Emergency stop |
| `pdflatex_refs.log`, `pdflatex_undefcs_cite.log` | undefined reference / citation, rerun |
| `pdflatex_boxes.log` | over/underfull h/v boxes, `\output is active` |
| `pdflatex_fontshape.log`, `pdflatex_missingchar.log`, `pdflatex_unicode.log` | font shape warning, missing character, inputenc error |
| `pdflatex_babel.log`, `pdflatex_pkgerror.log` | package errors, `\PackageError`, `\GenericError`, `\ClassError`, warnings |
| `pdflatex_math.log` | double superscript, missing $, file ended |
| `pdflatex_shellescape.log`, `pdflatex_pdftexwarn.log` | `\write18`, `\openout`, pdfTeX warning |
| `pdflatex_wrapped79.log` | classic hard-wrapped log |
| `pdflatex_ok.log`, `xelatex_ok.log`, `lualatex_ok.log` | clean runs, engine banners |
| `lualatex_luaerror.log`, `lualatex_luawarn.log` | `\directlua` errors, luaotfload, fontspec |
| `xelatex_xetexerr.log`, `xelatex_fontmissing.log` | XeTeX + fontspec errors |
| `xelatex_xdvipdfmx.log`, `.stdout.txt` | xdvipdfmx fatal (terminal output, not in the .log) |
| `bibtex_*.blg` | missing database/style, syntax error, extra `\bibstyle`, warnings |
| `makeindex_*.ilg`, `makeindex_missing.txt` | makeindex accepted/rejected, warnings, missing input |
| `biber_nocontrol.blg` | biber, missing `.bcf` |
| `latexmk_*.txt` (+ `.exit`) | latexmk stdout/stderr: success with passes, up-to-date, failed, `-f` after failure, missing source, max runs, rc error, bad option, bibtex+makeindex rules |

## Hand-written (no biblatex / xindy / glossaries in this TeX Live)

These were written from knowledge of the tools' output formats, not captured:

- `biber_warnings.blg`, `biber_errors.blg` (Biber 2.19 style)
- `makeglossaries_ok.glg` (makeindex run by makeglossaries)
- `xindy_warn.glg` (xindy was not installed)
