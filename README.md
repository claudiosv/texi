# texi

An isolated workspace manager for LaTeX projects.

`texi` wraps `tlmgr` and `latexmk` to give each LaTeX project its own local TeX Live tree (`.texenv/`), so package versions are pinned per-project and system-wide TeX Live is never polluted.

## Commands

| command | what it does |
|---|---|
| `texi init` | Creates `texproject.toml` (`[project]` with `name` = directory name, `src_dir = "src"`), seeding `dependencies` by scanning `.tex` files for `\usepackage`/`\RequirePackage`. |
| `texi add <pkg>...` | Adds packages to `dependencies` and installs them. A name that isn't a TeX Live package (e.g. `binhex`) is resolved to the package that ships it (`kastrup`), asking you to confirm or choose. `--sync` runs a full sync afterwards. |
| `texi sync` | Installs all `dependencies` into `.texenv/`, then updates them (`--no-update` to skip), and records the installed revisions in `texi.lock`. |
| `texi build` | Generates `.latexmkrc` from the config and runs `latexmk`. `--strict` stops at the first error, `--full` forces a full rebuild (`go_mode = "full-rebuild"`), `--rm-pdf` deletes the old PDF first. |
| `texi preview` | Like `build`, but opens the viewer and rebuilds on every change (`latexmk -pvc`). |
| `texi export <markdown\|html\|tex4ht\|latexml>` | Converts to `build/export/<format>/`: Markdown or HTML via pandoc (with citeproc and MathJax), HTML via TeX4ht (`make4ht`, needs `luaxml`) or LaTeXML (`latexmlc`). |
| `texi fmt [files]` | Formats `.tex` files under `src_dir` with `tex-fmt`, using the settings in `[tex-fmt]`. `--check` only reports. |
| `texi scan` | Lists the packages your sources use and their `tlmgr` dependencies. |
| `texi tree` | Shows which files the last build actually read. |
| `texi doctor` | Checks the TeX install and flags stale user-level formats. |
| `texi formats` / `texi updmap` / `texi mktexlsr` / `texi luaotfload` / `texi update` | Rebuild formats, font maps, the `ls-R` filename databases (`--local` for `.texenv/` only) and the LuaTeX font database (`--force` for a full rebuild); update TeX Live. |

All commands accept `--debug` where it makes sense.

## Installation

```bash
uv tool install texi-cli
```

## Quick start

```bash
texi init      # create texproject.toml
texi sync      # install dependencies into .texenv/
texi build     # compile
```

Default layout (`src_dir = "src"`):

```
texproject.toml
.latexmkrc          # generated, keep at the project root
.texenv/            # local TeX tree
build/              # PDF and aux files
src/main.tex        # built via $do_cd + @default_files in .latexmkrc
src/references.bib
```

`texi build` regenerates `.latexmkrc` on every run, so edit `texproject.toml`, not the rc file.

## `texproject.toml` reference

Every key is optional. Keys outside the tables below are rejected.

```toml
[project]
name = "my-paper"
src_dir = "src"
dependencies = ["acmart", "biblatex", "geometry"]

[build]
jobname = "my_paper"
pdf_mode = "lualatex"
out_dir = "build"
aux_dir = "build"
lualatex = "lualatex -interaction=nonstopmode -synctex=1 %O %S"
bibtex_use = "always"
```

### `[project]`

| key | default | description |
|---|---|---|
| `name` | none | Project name. Used as the latexmk `$jobname` (so the PDF is `<name>.pdf`) unless `build.jobname` is set. `texi init` sets it to the directory name. |
| `main` | `"main.tex"` | The main `.tex` file, relative to `src_dir`. `build.default_files` overrides it (and allows several). |
| `texlive` | none | Expected TeX Live year, e.g. `"2026"`. texi warns if the installed year differs. |
| `repository` | none | tlmgr repository to install from, passed as `tlmgr --repository`. Point it at a frozen tlnet to really pin versions. |
| `src_dir` | `"src"` | Directory holding the `.tex` sources. the generated `.latexmkrc` sets `$do_cd = 1` and `@default_files`, so latexmk runs inside `src_dir`. Use `"."` for sources in the project root (then latexmk picks up `*.tex` itself). |
| `dependencies` | `[]` | `tlmgr` package names installed into `.texenv/` by `texi sync` / `texi add`. |

When `src_dir` is not `"."`, the generated `.latexmkrc` stays at the project root and:

- anchors `out_dir`/`aux_dir` at the project root (otherwise `-cd` would put them inside `src/`),
- prepends `<root>/<src_dir>//` to `BIBINPUTS` and `TEXINPUTS`, so biber/bibtex running from the aux dir still find your `.bib` files.

The main file is `build.default_files[0]` if set, else `project.main` (both relative to `src_dir`).

### `[build]`

One flat table. The keys are grouped below by what they control.

#### Engine

| key | default | description |
|---|---|---|
| `pdf_mode` | `"pdflatex"` | How the PDF is produced (latexmk `$pdf_mode`), see below. |
| `dvi_mode` | `false` | Also produce a DVI (`$dvi_mode`). |
| `postscript_mode` | `false` | Also produce PostScript (`$postscript_mode`). |

`pdf_mode` values:

| value | `$pdf_mode` | pipeline |
|---|---|---|
| `"off"` | 0 | no PDF |
| `"pdflatex"` | 1 | pdflatex straight to PDF |
| `"ps2pdf"` | 2 | latex → dvips → ps2pdf (for PostScript-dependent packages like pstricks) |
| `"dvi2pdf"` | 3 | latex → dvipdf |
| `"lualatex"` | 4 | lualatex straight to PDF; best Unicode and OpenType font support |
| `"xelatex"` | 5 | xelatex; Unicode and system fonts |

#### Engine options

| key | default | description |
|---|---|---|
| `synctex` | `true` | Add `-synctex=1` to every engine command (pdflatex, lualatex, xelatex, latex). |
| `shell_escape` | `false` | Add `-shell-escape`. |
| `extra_args` | `[]` | Extra arguments for every engine command, e.g. `["-file-line-error"]`. |
| `env` | `{}` | Extra environment variables for the build, as a `[build.env]` table. |
| `bib_dirs` | `[]` | Extra directories (relative to the project root, or absolute) searched recursively for `.bib` files (`BIBINPUTS`). |
| `tex_inputs` | `[]` | Same, for TeX files (`TEXINPUTS`). `src_dir` is always included. |

Flags already present in a `lualatex = "…"` style override aren't repeated.

#### Directories

| key | default | description |
|---|---|---|
| `out_dir` | `"build/"` | Where the final PDF goes (`$out_dir`). |
| `aux_dir` | `"build/aux/"` | Where `.aux`, `.log`, `.fls` etc. go (`$aux_dir`). Set equal to `out_dir` to keep one folder. |

| `out2_dir` | none | If set, the final files (`out2_exts`) are copied here, while `out_dir` keeps the intermediate output. |
| `out2_exts` | latexmk's: `hnt`, `pdf`, `ps`, `synctex`, `synctex.gz` | Extensions copied to `out2_dir` (without the dot). |
| `emulate_aux` | latexmk's default | Emulate `aux_dir` by moving files out of `out_dir`, for engines without `--aux-directory`. |
| `allow_subdir_creation` | latexmk's default | May latexmk create subdirectories of `aux_dir`: `"none"`, `"aux"` (only for aux files, e.g. `\include{chapters/chap}`) or `"all"`. |

A trailing `/` is added to the directories if missing. Relative paths are relative to the project root, even with a `src_dir` (texi writes `$do_cd = 1` and anchors them).

#### Programs

Override the command latexmk runs for a tool. Use `%O` for latexmk's options and `%S` for the source file. All default to latexmk's own commands (unset).

`pdflatex`, `lualatex`, `xelatex`, `latex`, `bibtex`, `biber`, `makeindex`, `makeglossaries`, `dvips`, `ps2pdf`, `dvipdf`, `gs`

and the quiet-mode switches `pdflatex_silent_switch`, `lualatex_silent_switch`, `xelatex_silent_switch`, `latex_silent_switch`, `bibtex_silent_switch`, `biber_silent_switch`.

```toml
[build]
pdflatex = "pdflatex -shell-escape %O %S"
```

#### Execution

| key | default | description |
|---|---|---|
| `halt_on_error` | `true` | Stop at the first error. `texi build`/`preview` override it with `--strict`. |
| `force_mode` | `false` | Keep going past errors (`-f`). Overridden by `--strict` (on unless strict). |
| `go_mode` | `"rebuild"` | `"rebuild"` redo only what changed; `"full-rebuild"` redo everything (`-g`); `"clean"` delete generated files first (`-gg`); `"force-main"` rerun only the main compile, then continue (`-gt`). `texi build --full` sets `"full-rebuild"`. |
| `bibtex_use` | `"always"` | `"never"` (0), `"bib_exists"` (1), `"even_without_bib"` (1.5), `"always"` (2: run bibtex/biber as needed and delete `.bbl` on clean). |
| `max_repeat` | `5` | Maximum latex reruns (1–20). |
| `preview_continuous_mode` | `false` | `-pvc`. Set by `texi preview`. |
| `preview_mode` | `false` | `-p`, one-shot preview. Always off for texi commands. |
| `sleep_time` | `2` | Seconds between file checks in continuous mode. |
| `recorder` | `true` | Pass `-recorder` so the `.fls` file list is written (needed by `texi tree`). |
| `warnings_as_errors` | `false` | Treat warnings as errors. |
| `optimize_pdf` | `false` | pdflatex only: inject `\pdfminorversion=5 \pdfcompresslevel=9 \pdfobjcompresslevel=3` to shrink the PDF. |

#### Files

| key | default | description |
|---|---|---|
| `default_files` | `[]` | Main `.tex` files, relative to `src_dir`. Empty means `main.tex`. |
| `jobname` | none | Output name (`$jobname`). Falls back to `project.name`; if neither is set, no `$jobname` is written and latexmk uses the main file's name. |
| `clean_ext` | see below | Extra extensions removed by `latexmk -c`. |
| `clean_full_ext` | `[]` | Extra extensions removed by `latexmk -C`. |
| `generated_exts` | `[]` | Extensions latexmk treats as generated (affects rerun detection). |

`clean_ext` default: `synctex.gz`, `synctex.gz(busy)`, `run.xml`, `tex.bak`, `bbl`, `bcf`, `fdb_latexmk`, `fls`, `lof`, `lot`, `out`, `toc`, `nav`, `snm`, `vrb`. Setting it replaces the list.

#### Viewers

All default to unset (latexmk's own defaults).

| key | description |
|---|---|
| `pdf_previewer`, `dvi_previewer`, `ps_previewer` | Viewer command for each output type. |
| `pdf_update_method`, `dvi_update_method`, `ps_update_method` | How the viewer shows a rebuilt file: `"auto"` (viewer watches the file), `"manual"` (you refresh it), `"signal"` (latexmk signals it), `"none"` (viewer locks the file, can't update), `"command"` (latexmk runs the `*_update_command`). |
| `pdf_update_command`, `dvi_update_command`, `ps_update_command` | Command to run when the method is `"command"`. |
| `pdf_update_signal`, `dvi_update_signal`, `ps_update_signal` | Signal number used with `"signal"` (1–64). |

### `[tex-fmt]`

Settings for `texi fmt`, rendered as a tex-fmt config and piped to `tex-fmt --config /dev/stdin` on every run (no temp file). It overrides any `tex-fmt.toml` on disk.

| key | default | description |
|---|---|---|
| `wrap` | `true` | Wrap long lines. |
| `wraplen` | `80` | Line length for wrapping. |
| `wrapmin` | tex-fmt's own (`wraplen` − 10) | Minimum line length before wrapping. |
| `tabsize` | `2` | Indent width. |
| `tabchar` | `"space"` | `"space"` or `"tab"`. |
| `lists` | `[]` | Extra list environments (added to tex-fmt's built-ins). |
| `verbatims` | `[]` | Extra environments left untouched (added to the built-ins). |
| `no_indent_envs` | `[]` | Extra environments whose contents aren't indented. |
| `wrap_chars` | `[]` | Extra characters lines may be broken after. |
| `format_tables` | `false` | Format tables. |

## Lockfile

`texi sync` and `texi add` write `texi.lock` next to `texproject.toml`: the TeX Live year, the repository (if set) and the installed revision of every dependency. Commit it. When a later sync changes a revision, texi lists the differences. tlmgr can only install what its repository currently offers, so to get the same revisions again, set `repository` to a frozen tlnet (for example a historic TeX Live archive for that year).

## Development

```bash
uv sync
uv run texi --help
uv run ruff check .
uv run ruff format .
uv run ty check .
```
