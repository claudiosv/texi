# TeX Live maintenance

Commands that act on the TeX Live install itself.

## `texi doctor`

Check your TeX install and warn about stale user-level formats.

| option | description |
|---|---|
| `--engine <str>` | Engine to check (default `pdflatex`). |

## `texi formats`

Rebuild LaTeX format files (`fmtutil`). Offers to fix TeX Live permissions if sudo would be needed.

| option | description |
|---|---|
| `--user` | Rebuild user formats (`fmtutil-user`). |
| `--missing` | Only build formats that don't exist yet. |
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |

## `texi updmap`

Rebuild font maps (`updmap`). Run this after installing fonts.

| option | description |
|---|---|
| `--user` | Regenerate user font maps (`updmap-user`). |
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |

## `texi mktexlsr`

Rebuild TeX's file-name databases (`ls-R`) with texi's own implementation (the `mktexlsr` program is not used). Without arguments it indexes the search-path trees of `.texenv/`: `texmf-dist`, `texmf-var` and `texmf-local`. texi also does this itself after every install.

| argument | description |
|---|---|
| `DIR...` | Index these directories instead. |

## `texi luaotfload`

Rebuild the LuaTeX font database, for example when `lualatex` can't find a font.

| option | description |
|---|---|
| `--force` | Rebuild the whole database (`--update --force`). |
