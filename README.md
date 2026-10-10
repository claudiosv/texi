# texi

An isolated workspace manager for LaTeX projects.

`texi` wraps `tlmgr` and `latexmk` to give each LaTeX project its own local TeX Live tree (`.texenv/`), so package versions are pinned per-project and system-wide TeX Live is never polluted.

## Features

- **`texi init`** — creates a `texproject.toml`. If `.tex` files exist in the current directory tree, scans them for `\usepackage`/`\RequirePackage` and seeds `dependencies` with the resolved `tlmgr` package names.
- **`texi sync`** — reads `texproject.toml`, initialises a local `tlmgr` user-tree in `.texenv/`, and installs declared dependencies into it.
- **`texi build`** — compiles the project with `latexmk`, pointing it at the local `.texenv/` tree via `TEXMFHOME`.

## Installation

```bash
uv tool install texi
```

Or, inside a project with `uv`:

```bash
uv add texi
```

## Usage

1. Add a `texproject.toml` to your LaTeX project root, either by hand or by running `texi init` (which scans any existing `.tex` files for dependencies):

```bash
texi init
```

```toml
[latexmk]

[latexmk.engines]
pdf_mode = "pdflatex"

[latexmk.directories]
out_dir  = "build/"
aux_dir  = "build/aux/"

dependencies = ["acmart", "biblatex", "geometry"]
```

To shrink pdflatex's PDF output, set `optimize_pdf = true` under `[latexmk.execution]`. This injects `\pdfminorversion=5 \pdfcompresslevel=9 \pdfobjcompresslevel=3` before the document is read:

```toml
[latexmk.execution]
optimize_pdf = true
```

2. Install dependencies into the local tree:

```bash
texi sync
```

3. Build the project:

```bash
texi build
```

## Development

```bash
uv sync
uv run texi --help
```

Linting and type-checking:

```bash
uv run ruff check .
uv run ruff format .
uv run ty check .
```
