# logparse

Parsers for the output of one build: TeX engine logs (pdfTeX, XeTeX, LuaTeX), BibTeX and Biber `.blg`, makeindex / xindy / makeglossaries `.ilg` and `.glg`, and latexmk's own stdout/stderr. `texi.logparse` is the stable facade; the work lives in the modules below. No parser raises on bad input (truncated, CRLF, binary, hard-wrapped or huge lines).

`.fls` and `.fdb_latexmk` files are not parsed.

## Facade

::: texi.logparse

## Shared types

::: texi.logparse_core

## TeX engine logs

::: texi.logparse_tex

## BibTeX and Biber

::: texi.logparse_bib

## makeindex, xindy, makeglossaries

::: texi.logparse_index

## latexmk

::: texi.logparse_latexmk
