---
hide:
  - navigation
---

# Configuration

Everything lives in `texproject.toml` at the project root.


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

## `[project]`

| key | default | description |
|---|---|---|
| `name` | none | Project name. Used as the latexmk `$jobname` (so the PDF is `<name>.pdf`) unless `build.jobname` is set. `texi init` sets it to the directory name. |
| `targets` | `["main.tex"]` | The top-level `.tex` files to build, relative to `src_dir`. `build.default_files` overrides it. |
| `verify_checksums` | `true` | Check every downloaded package against the sha512 in the package index. Containers are first downloaded from the mirror the index came from (`mirror.ctan.org` picks a different mirror per request, and mirrors sync at different times); on a mismatch texi retries through the redirector, then fails and suggests pinning `repository`. Set `false` to only warn (`package hash mismatched … likely a stale mirror`) and install anyway; the file is then kept in the store under the sha512 it really has. |
| `texlive` | none | Expected TeX Live year, e.g. `"2026"`. texi warns if the installed year differs. |
| `repository` | none | TeX Live repository (`tlnet`) to install from. Point it at a frozen tlnet to really pin versions. |
| `src_dir` | `"src"` | Directory holding the `.tex` sources. The generated `.latexmkrc` sets `$do_cd = 1` and `@default_files`, so latexmk runs inside `src_dir`. Use `"."` for sources in the project root (then latexmk picks up `*.tex` itself). |
| `dependencies` | `[]` | TeX Live package names installed into `.texenv/` by `texi sync` / `texi add`. |

When `src_dir` is not `"."`, texi configures `latexmk`:

- anchors `out_dir`/`aux_dir` at the project root (otherwise `-cd` would put them inside `src/`),
- prepends `<root>/<src_dir>//` to `BIBINPUTS` and `TEXINPUTS`, so biber/bibtex running from the aux dir still find your `.bib` files.

The main file is the first file in `build.default_files` if set, else the first of `project.targets` (both relative to `src_dir`).

## `[build]`

One flat table. The keys are grouped below by what they control.

### Engine

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

### Engine options

| key | default | description |
|---|---|---|
| `synctex` | `true` | Add `-synctex=1` to every engine command (pdflatex, lualatex, xelatex, latex). SyncTeX makes the TeX engine write a `.synctex.gz` file next to the PDF. The file maps positions in the PDF to source file and line numbers. A PDF viewer that supports it can then jump from the source to the matching spot in the PDF (forward search). It can also jump from a click in the PDF back to the source line (inverse search). |
| `shell_escape` | `false` | Add `-shell-escape`. This is needed when the document runs external programs during compilation, e.g., for the `minted` package (Pygments syntax highlighting). |
| `file_line_error` | `true` | Add `-file-line-error`, so errors read `file.tex:12: message` (editors and CI can parse that). Texi always forces `-file-line-error` where needed. |
| `extra_args` | `[]` | Extra arguments for every engine command, e.g. `["-file-line-error"]`. |
| `env` | `{}` | Extra environment variables for the build, as a `[build.env]` table. |
| `bib_dirs` | `[]` | Extra directories (relative to the project root, or absolute) searched recursively for `.bib` files (`BIBINPUTS`). |
| `tex_inputs` | `[]` | Same, for TeX files (`TEXINPUTS`). `src_dir` is always included. |

Flags already present in a `lualatex = "…"` style override aren't repeated.

### Directories

| key | default | description |
|---|---|---|
| `out_dir` | `"build/"` | Where the final PDF goes (`$out_dir`). |
| `aux_dir` | `"build/aux/"` | Where `.aux`, `.log`, `.fls` etc. go (`$aux_dir`). Set equal to `out_dir` to keep one folder. |
| `out2_dir` | none | If set, the final files (`out2_exts`) are copied here, while `out_dir` keeps the intermediate output. |
| `out2_exts` | latexmk's: `hnt`, `pdf`, `ps`, `synctex`, `synctex.gz` | Extensions copied to `out2_dir` (without the dot). |
| `emulate_aux` | `true` | Emulate `aux_dir` by moving files out of `out_dir`, for engines without `--aux-directory`. |
| `allow_subdir_creation` | `"aux"` | May latexmk create subdirectories of `aux_dir`: `"none"`, `"aux"` (only for aux files, e.g. `\include{chapters/chap}`) or `"all"`. |

A trailing `/` is added to the directories if missing. Relative paths are relative to the project root, even with a `src_dir` (texi writes `$do_cd = 1` and anchors them).

### Programs

Override the command latexmk runs for a tool. Use `%O` for latexmk's options and `%S` for the source file. All default to latexmk's own commands (unset).

`pdflatex`, `lualatex`, `xelatex`, `latex`, `bibtex`, `biber`, `makeindex`, `makeglossaries`, `dvips`, `ps2pdf`, `dvipdf`, `gs`

and the quiet-mode switches `pdflatex_silent_switch`, `lualatex_silent_switch`, `xelatex_silent_switch`, `latex_silent_switch`, `bibtex_silent_switch`, `biber_silent_switch`.

```toml
[build]
pdflatex = "pdflatex -shell-escape %O %S"
```

### Execution

| key | default | description |
|---|---|---|
| `diagnostics` | `false` | Print latexmk's internal decisions (`$diagnostics`). `texi build --diagnostics` turns it on for one run. |
| `silent` | `false` | Quiet latexmk and the TeX engines (latexmk `$silent`). `texi build --silent` turns it on for one run. |
| `check_type3` | `true` | After `texi build`/`preview`, warn if the PDF contains Type 3 fonts. texi only; not written to `.latexmkrc`. |
| `write_latexmkrc` | `false` | Write the generated `.latexmkrc` to the project root. Off: texi pipes it to latexmk (`-norc -r /dev/stdin`) and nothing is written. Always written on Windows. |
| `halt_on_error` | `false` | Stop at the first error. `--strict` forces it on. |
| `force_mode` | `true` | Keep going past errors (`-f`). `--strict` forces it off. |
| `go_mode` | `"rebuild"` | `"rebuild"` redo only what changed; `"full-rebuild"` redo everything (`-g`); `"clean"` delete generated files first (`-gg`); `"force-main"` rerun only the main compile, then continue (`-gt`). `texi build --full` forces `"full-rebuild"`. |
| `bibtex_use` | `"bib_exists"` | `"never"` (0), `"bib_exists"` (1), `"even_without_bib"` (1.5), `"always"` (2: run bibtex/biber as needed and delete `.bbl` on clean). |
| `max_repeat` | `5` | Maximum latex reruns (1–20). |
| `preview_continuous_mode` | `false` | `-pvc`. Forced true by `texi preview`. |
| `preview_mode` | `false` | `-p`, one-shot preview. Always off for texi commands. |
| `sleep_time` | `2` | Seconds between file checks in continuous mode. |
| `recorder` | `true` | Pass `-recorder` so the `.fls` file list is written (needed by `texi build-tree`). |
| `warnings_as_errors` | `false` | Treat warnings as errors. |
| `optimize_pdf` | `false` | pdflatex only: inject `\pdfminorversion=5 \pdfcompresslevel=9 \pdfobjcompresslevel=3` to shrink the PDF. |

### Files

| key | default | description |
|---|---|---|
| `default_files` | `[]` | Main `.tex` files, relative to `src_dir`. If not set, it takes the value of `project.targets`. |
| `jobname` | none | Output name (`$jobname`). Falls back to `project.name`; if neither is set, no `$jobname` is written and latexmk uses the main file's name. |
| `clean_ext` | see below | Extra extensions removed by `latexmk -c`. |
| `clean_full_ext` | `[]` | Extra extensions removed by `latexmk -C`. |
| `generated_exts` | `[]` | Extensions latexmk treats as generated (affects rerun detection). |

`clean_ext` default: `synctex.gz`, `synctex.gz(busy)`, `run.xml`, `tex.bak`, `bbl`, `bcf`, `fdb_latexmk`, `fls`, `lof`, `lot`, `out`, `toc`, `nav`, `snm`, `vrb`. Setting it replaces the list.

### Viewers

All default to unset (latexmk's own defaults).

| key | description |
|---|---|
| `pdf_previewer`, `dvi_previewer`, `ps_previewer` | Viewer command for each output type. On macOS, if Skim is installed and `pdf_previewer` is unset, texi uses `open -a Skim %O %S` with `pdf_update_method = "auto"` (Skim reloads changed files itself). |
| `pdf_update_method`, `dvi_update_method`, `ps_update_method` | How the viewer shows a rebuilt file: `"auto"` (viewer watches the file), `"manual"` (you refresh it), `"signal"` (latexmk signals it), `"none"` (viewer locks the file, can't update), `"command"` (latexmk runs the `*_update_command`). |
| `pdf_update_command`, `dvi_update_command`, `ps_update_command` | Command to run when the method is `"command"`. |
| `pdf_update_signal`, `dvi_update_signal`, `ps_update_signal` | Signal number used with `"signal"` (1–64). |

## `[tex-fmt]`

See the [tex-fmt documentation](https://github.com/wgunderwood/tex-fmt). Settings for `texi fmt`, rendered as a tex-fmt config and piped to `tex-fmt` on every run. It overrides any `tex-fmt.toml` on disk.

| key | default | description |
|---|---|---|
| `wrap` | `true` | Wrap long lines. |
| `wraplen` | `80` | Line length for wrapping. |
| `wrapmin` | `wraplen` − 10 | Minimum line length before wrapping. |
| `tabsize` | `2` | Indent width. |
| `tabchar` | `"space"` | `"space"` or `"tab"`. |
| `lists` | `["itemize", "enumerate", "description", "enumerate*", "itemize*"]` | List environments (added to tex-fmt's built-ins). Setting it replaces this default. |
| `verbatims` | `[]` | Extra environments left untouched (added to the built-ins). |
| `no_indent_envs` | `[]` | Extra environments whose contents aren't indented. |
| `wrap_chars` | `[]` | Extra characters lines may be broken after. |
| `format_tables` | `false` | Format tables. |
| `verbosity` | `"warn"` | tex-fmt's log level: `"error"` (hides warnings), `"warn"`, `"info"` or `"trace"`. `texi fmt --error`, `--warn`, `--info` and `--trace` override it for one run. |

