# texi

An isolated workspace manager for LaTeX projects.

`texi` wraps `tlmgr` and `latexmk` to give each LaTeX project its own local TeX Live tree (`.texenv/`), so package versions are pinned per-project and system-wide TeX Live is never polluted.

## Features

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

1. Add a `texproject.toml` to your LaTeX project root:

```toml
[latexmk]

[latexmk.engines]
pdf_mode = "pdflatex"

[latexmk.directories]
out_dir  = "build/"
aux_dir  = "build/aux/"

dependencies = ["acmart", "biblatex", "geometry"]
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
