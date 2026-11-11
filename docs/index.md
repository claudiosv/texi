# texi

An isolated workspace manager for LaTeX projects, in the spirit of `uv` for Python.

`texi` wraps `tlmgr` and `latexmk` and gives each LaTeX project its own local TeX Live tree (`.texenv/`). One `texproject.toml` describes the project: its packages, engine and build options.

The local tree is created in one of three ways, chosen with `texlive_env`:

- **`usermode`** (default): a tlmgr user tree layered over the system TeX Live. Packages that exist only on the system, such as `latexmk`, are reused.
- **`copy`**: a standalone tree copied from an existing TeX Live install, with a minimal package set.
- **`install`**: a fresh, minimal TeX Live tree (scheme minimal plus latex and latexmk) installed with `install-tl`.

## Why texi

- **Per-project packages.** Dependencies install into `.texenv/`, so one paper's package updates never break another, and the system TeX Live stays untouched.
- **Resolves package name mismatches.** The name TeX loads is a file name, which is not always the name of the package that ships it. For example, `binhex` is provided by the `kastrup` package, so `tlmgr install binhex` fails. texi runs `tlmgr search --file --global --word` for packages that contain the exact file (`.sty` and `.cls` for names found by `texi init` and `texi scan`; `texi add` also accepts `.tex`). One hit is used directly. If several packages ship the file, texi asks the CTAN API (`ctan.org/json/3.0`) and reads the `texlive` field of the package record; if that names one of the candidates, it is chosen. Otherwise `texi add` asks you to pick one (showing each short description), and `texi init` and `texi scan` take the first. If `tlmgr` finds nothing, the CTAN answer is used on its own, and the raw name is kept if CTAN has none. Results are cached in `~/.cache/texi/`; a lookup that needed CTAN while offline is not cached.
- **Reproducible.** `texi sync` writes `texi.lock` with the TeX Live year and the installed revision of every package. Commit it, and texi lists what changed when a later sync differs. Set `repository` to a frozen tlnet to pin versions for real.
- **No hand-written `.latexmkrc`.** texi generates it from `texproject.toml` on every build: engine, output directories, `synctex`, bibliography paths and more.
- **Readable build output.** `texi build` parses latexmk's logs into a short list of errors, warnings and overfull boxes. `--raw` gives the full latexmk output.
- **Live preview.** `texi preview` rebuilds on every change and reports diagnostics after each cycle.
- **Venue checks.** After a build, texi warns about Type 3 fonts, which ACM rejects, and [`texi fix-pdf`](commands/fix-pdf.md) explains or repairs them.

## Install

```bash
uv tool install texi-cli
```

The `usermode` and `copy` modes need an existing system TeX Live. The `install` mode needs none: it downloads a fresh minimal tree into `.texenv/`. Run `texi doctor` to check your setup.

## Quick start

From a directory containing your `.tex` files:

```bash
texi init      # create texproject.toml
texi sync      # install dependencies into .texenv/
texi build     # compile
```

`texi init` creates `texproject.toml` (`name` set to the directory name, `src_dir = "src"`) and seeds `dependencies` by scanning your `.tex` files for `\usepackage` and `\RequirePackage`. Add packages later with `texi add <pkg>`.

## Example `texproject.toml`

```toml
[project]
name = "my-paper"
src_dir = "src"
main = "main.tex"
texlive = "2026"
dependencies = ["acmart", "biblatex", "biber", "geometry", "microtype"]

[build]
pdf_mode = "lualatex"
out_dir = "build"
aux_dir = "build"
synctex = true
file_line_error = true
bibtex_use = "always"
```

Every key is optional. See the [full reference](configuration.md).

## Project layout

Default layout (`src_dir = "src"`):

```
texproject.toml
texi.lock           # written by texi sync, commit it
.latexmkrc          # generated, keep at the project root
.texenv/            # local TeX tree
build/              # PDF and aux files
src/main.tex
src/references.bib
```

`texi build` regenerates `.latexmkrc` on every run, so edit `texproject.toml`, not the rc file.

## Lockfile

`texi sync` and `texi add` write `texi.lock` next to `texproject.toml`: the TeX Live year, the repository (if set) and the installed revision of every dependency. Commit it. When a later sync changes a revision, texi lists the differences. tlmgr can only install what its repository currently offers, so to get the same revisions again, set `repository` to a frozen tlnet (for example a historic TeX Live archive for that year).

## Next

See the [command reference](commands/index.md) and the [`texproject.toml` reference](configuration.md). Most commands accept `--debug`, which shows every external command texi runs, along with its output.
