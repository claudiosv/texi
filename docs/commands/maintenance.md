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

Rebuild TeX's file-name databases (`ls-R`).

| option | description |
|---|---|
| `--local` | Only index `.texenv/` (no sudo). |
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |

## `texi luaotfload`

Rebuild the LuaTeX font database, for example when `lualatex` can't find a font.

| option | description |
|---|---|
| `--force` | Rebuild the whole database (`--update --force`). |

## `texi update`

Update TeX Live with `tlmgr`, then rebuild formats and font maps.

| option | description |
|---|---|
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |
