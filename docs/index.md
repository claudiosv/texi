# texi

An isolated workspace & package manager for LaTeX projects, bringing isolated `.texenv` environments akin to Python's `.venv`. Texi is a replacement for `tlmgr` in the spirit of `uv` for Python.

Texi installs a minimal, standalone TeX Live tree for each LaTeX project, so no system TeX environment is needed. The tree is built from a shared cache, meaning that packages are downloaded once and reused across projects. It also wraps `latexmk` to provide consistent build and preview experiences, and formats/lints with `tex-fmt`. One `texproject.toml` describes the project: its package dependencies, engine and build options.

## Why texi

- **Per-project packages.** Dependencies install into `.texenv/`, so one paper's package updates never break another, and the system TeX Live stays untouched.
- **No tlmgr, no system TeX Live.** texi never calls `tlmgr`, never reads or writes the system TeX Live, and needs only `perl` from the system. It installs `.texenv/` itself from the CTAN `tlnet` containers; see [texi sync](commands/project.md#texi-sync).
- **Shared cache, instant installs.** Packages are downloaded once (`~/.cache/texi/archive`) and copy-on-write cloned into each project's `.texenv/` (like uv does for Python), so a second project that needs the same package installs in milliseconds and offline.
- **Resolves package name mismatches.** The name TeX loads in `\usepackage` directive is a file name, which is not always the name of the package that ships it. For example, `binhex` is provided by the `kastrup` package, so `tlmgr install binhex` fails. Texi first looks the name up in its local package index, if a package by that name doesn't exist, it falls back to the CTAN API or asks you to pick one (showing each short description).
- **Reproducible.** `texi sync` writes `texi.lock` with the TeX Live year and the installed revision of every package. Commit it, and texi lists what changed when a later sync differs. Set `repository` to a frozen tlnet to pin versions for real.
- **Everything in one `.toml` file.** All project configuration is in `texproject.toml`, including dependencies, build options, formatting,and more. That means no more hand-written `.latexmkrc` files, no separate `tex-fmt.toml`.
- **Readable build output.** `texi build` parses latexmk's logs into a short list of errors, warnings and overfull boxes. `--raw` gives the full latexmk output.
- **Formatting.** `texi fmt` formats your `.tex` files with `tex-fmt`, using the `[tex-fmt]` settings in `texproject.toml`. `--check` only reports. tex-fmt's log is shown like build diagnostics, with clickable file and line.
- **Exporting.** `texi export` converts the document to Markdown or HTML (pandoc, TeX4ht, LaTeXML), or `texi export expand` inlines every `\input` into one `.tex` file, for each file in `targets`.
- **Live preview.** `texi preview` rebuilds on every change and reports diagnostics after each cycle.
- **Venue checks.** After a build, texi warns about Type 3 fonts, which ACM rejects, and [`texi fix-pdf`](commands/fix-pdf.md) explains or repairs them.

## Install

```bash
uv tool install texi-cli
```

Run `texi doctor` to check your setup.

## Quick start

From a directory containing your `.tex` files:

```bash
texi init      # create texproject.toml
texi sync      # install dependencies into .texenv/
texi build     # compile
```

`texi init` creates `texproject.toml` and seeds `dependencies` by scanning your `.tex` files for `\usepackage` and `\RequirePackage`. Add packages later with `texi add <pkg>`.

## Example `texproject.toml`

```toml
[project]
name = "my-paper"
src_dir = "src"
targets = ["main.tex"]
texlive = "2026"
dependencies = ["acmart", "biblatex", "biber", "geometry", "microtype"]

[build]
pdf_mode = "pdflatex"
out_dir = "build"
aux_dir = "build"
synctex = true
bibtex_use = "always"
```

Every key is optional. See the [full reference](configuration.md).

## Project layout

Default layout (`src_dir = "src"`):

```
texproject.toml
texi.lock           # written by texi sync, commit it
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
