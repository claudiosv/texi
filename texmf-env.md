# TeX environment

No `TEXMF*` environment variables are set in the shell. The `TEXMF*` values below are resolved by `kpsewhich -var-value=NAME` from `texmf.cnf`.

| variable | value |
|---|---|
| `HOME` | `/Users/claudio` |
| `USER` | `claudio` |
| `TEXMFHOME` | `/Users/claudio/Library/texmf` |
| `TEXMFVAR` | `/Users/claudio/Library/texlive/2026basic/texmf-var` |
| `TEXMFCONFIG` | `/Users/claudio/Library/texlive/2026basic/texmf-config` |
| `TEXMFLOCAL` | `/usr/local/texlive/2026basic/texmf-local` |
| `TEXMFSYSVAR` | `/usr/local/texlive/2026basic/texmf-var` |
| `TEXMFSYSCONFIG` | `/usr/local/texlive/2026basic/texmf-config` |
| `TEXMFROOT` | `/usr/local/texlive/2026basic` |
| `TEXMFDIST` | `/usr/local/texlive/2026basic/texmf-dist` |

Captured from Claude Code's shell. A normal terminal may differ.

texi overrides `TEXMFHOME`, `TEXMFVAR` and `TEXMFCONFIG` to point into `.texenv/` for builds in a project that has one.
