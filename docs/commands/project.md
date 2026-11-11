# Project setup

## `texi init`

Create a `texproject.toml` for this project, filling in packages found in your `.tex` files. The file has `[project]` with `name` = directory name and `src_dir = "src"`; `dependencies` is seeded by scanning `.tex` files for `\usepackage`/`\RequirePackage`.

| option | description |
|---|---|
| `--force` | Replace an existing `texproject.toml`. |
| `--texlive <usermode\|copy\|install>` | How to build `.texenv/` (default `usermode`). `usermode`: tlmgr usertree over the system TeX Live. `copy`: standalone tree copied from the system install. `install`: fresh `install-tl` installation. See `project.texlive_env` in the [configuration](../configuration.md). |
| `--debug` | Show every external command texi runs, along with its output. |

## `texi add <pkg>...`

Add packages to `dependencies` and install them. A name that isn't a TeX Live package (for example `binhex`) is resolved to the package that ships it (`kastrup`), asking you to confirm or choose.

| option | description |
|---|---|
| `--sync` | Afterwards, install and update all dependencies, as `texi sync` does. |
| `--debug` | Show every external command texi runs. |

## `texi sync`

Install every package listed in `dependencies` into `.texenv/`, then update them, and record the installed revisions in `texi.lock`.

| option | description |
|---|---|
| `--no-update` | Install only; skip updating packages that are already installed. |
| `--debug` | Show every external command texi runs. |

## `texi scan`

List the packages your source files use and their `tlmgr` dependencies, and flag any missing from `texproject.toml`.

| option | description |
|---|---|
| `--resolve` / `--no-resolve` | Resolve names to tlmgr bundles (default: resolve). |
| `--debug` | Show every external command texi runs. |

## `texi info`

Show where texi keeps this project's local TeX tree and which tools it found.
