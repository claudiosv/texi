# latexmk fixtures

Real outputs (TeX Live 2026 basic scheme, `latexmk -recorder -f`), with the
home directory rewritten to `/home/user` and the build directory to `/tmp/proj`:

- `bibtex.*`: article + bibtex run (pdflatex and `bibtex main` rules).
- `makeindex.*`: article + makeidx (`makeindex main.idx` rule).
- `space_in_path.*`: project directory `sp ace`, input file `my sec.tex`.
- `missing_pkg.*`: graphicx + `example-image` not installed (failed run, nonexistent dep with size -1).
- `xelatex_failed.*`: xelatex (`xdvipdfmx` rule) run that ended with an error.
- `biblatex_missing.*`: truncated-by-failure build (`biblatex.sty` not installed).

Hand-written (not produced by a tool, written from the documented formats):

- `biber_handwritten.*`: biber + lualatex, `build/` output dir, a project path
  with a space, a texmf path outside TeX Live, and an out-of-project input.
  biber and lualatex produce no usable output in the corpus machine (biblatex
  and the lualatex fixture, ~140 KB, were missing/too large).
