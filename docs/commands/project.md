# Project setup

## `texi init`

Create a `texproject.toml` for this project, filling in packages found in your `.tex` files. The file has `[project]` with `name` = directory name and `src_dir = "src"`; `dependencies` is seeded by scanning `.tex` files for `\usepackage`/`\RequirePackage`.

| option | description |
|---|---|
| `--force` | Replace an existing `texproject.toml`. |
| `--debug` | Show every external command texi runs, along with its output. |

## `texi add <pkg>...`

Add packages to `dependencies` and install them. A name that isn't a TeX Live package (for example `binhex`) is resolved to the package that ships it (`kastrup`), asking you to confirm or choose.

| option | description |
|---|---|
| `--sync` | Afterwards, install and update all dependencies, as `texi sync` does. |
| `--debug` | Show every external command texi runs. |

## `texi sync`

Fetch the latest package index (a conditional request; the cached copy is used when the repository is unreachable), install every package listed in `dependencies` into `.texenv/`, update those whose revision changed, and record the installed revisions in `texi.lock`.

| option | description |
|---|---|
| `--no-upgrade` | Install only: don't fetch a newer package index or update installed packages. |
| `--debug` | Show every external command texi runs. |

### Package store

Each package's runfiles container (`<repository>/archive/<name>.tar.xz`, verified against the sha512 in the tlpdb) is extracted once into a global store:

```text
~/.cache/texi/archive/<ID>/<PACKAGE>/          e.g. .../newtx-78101-1a2b3c4/newtx/tex/latex/newtx/newtx.sty
~/.cache/texi/archive/<ID>/texi-archive.json   name, revision, repository, checksum, files
```

`<ID>` is `<name>-<revision>-<first 7 characters of the container checksum>`, for example `kastrup-15878-d7cbd22`, so a new revision gets a new directory. `XDG_CACHE_HOME` is honored. Files are then cloned (copy-on-write on APFS, btrfs and XFS; a plain copy elsewhere, never a hard link) into `.texenv/` (`tex/latex/newtx/newtx.sty`; in `copy`/`install` trees under `texmf-dist/`), and recorded in `.texenv/tlpkg/texlive.tlpdb` and `tlpkg/tlpobj/` so `tlmgr`, `texi.lock` and `texi info` keep working. Dependencies are resolved as described under [Dependency resolution](#dependency-resolution). With updates enabled, a package whose index revision is newer is reinstalled from a new store entry.

texi never runs `tlmgr`. The user tree skeleton (`tlpkg/texlive.tlpdb`, `tlpkg/tlpobj/`, `web2c/`, `texmf-var/`, `texmf-config/`) is written by texi itself, packages the system tlpdb lists as installed are not copied, and `latexmk` is installed into `.texenv/` when no `latexmk` is on `PATH`. `.texenv/bin/<arch>` is put first on `PATH` for every process texi starts and in the generated latexmkrc. Deleting `~/.cache/texi/archive/` is safe; there is no cleanup command yet.

#### Install actions and binaries

After a batch is cloned, texi performs the `execute` and `postaction` lines of the installed packages itself, following `tlmgr`, once per batch and never through `tlmgr`:

- `addMap`, `addMixedMap`, `addKanjiMap`: texi writes `web2c/updmap.cfg` (standalone: `texmf-dist/web2c/updmap.cfg`) and runs `updmap-user` (standalone: `updmap-sys`) once.
- `AddFormat`: texi writes `web2c/fmtutil.cnf` and builds the formats of the batch with `fmtutil-user --byfmt` (standalone: `fmtutil-sys`) once. Formats whose `fmttriggers` name a changed package are rebuilt, a failing build is a yellow warning.
- `AddHyphen`: `language.dat`, `language.def` and `language.dat.lua` are derived from all installed packages plus the system's, written to `texmf-var/tex/generic/config/`, and fed to `fmtutil --byhyphen`. When no package adds hyphenation any more, the generated files are removed.
- Windows-only postactions (`shortcut`, `fileassoc`, `filetype`, `progid`) are ignored. `script` postactions run only in standalone trees.

Binary packages (`name.<arch>`) are cloned for the selected architecture only into `.texenv/bin/<arch>/`, with execute bits kept and symlinks relative. Uninstalling a package removes its map, format and hyphenation entries and binaries.

A package that cannot be installed is never handed to `tlmgr`. An unknown root goes to the missing list, a failed download or checksum mismatch is reported in yellow and the package is listed as failed, and a package that is not relocatable is skipped in a user tree with the reason, as `tlmgr` does.

#### Dependency resolution

texi resolves the `depend` closure itself, like TeX Live's `expand_dependencies`, against one repository's package index. `name.ARCH` dependencies become `name.<arch>` for the tree's architecture (a standalone `.texenv/`'s `bin/<arch>`, otherwise the architecture derived from the platform, for example `universal-darwin` or `x86_64-linux`; archive mode never runs `tlmgr print-platform`); binary packages for other architectures and infrastructure packages are skipped. Collections and schemes are followed but not installed; a collection or scheme you list yourself is expanded, its member packages are installed, and the collection is recorded in `.texenv/` without files so `tlmgr` sees it as installed. Packages are installed in dependency order. Settings stored in `depend` lines of `00texlive.*` entries are never dependencies. A dependency the index does not know is reported in yellow with the packages that need it and does not stop the sync. With `--debug`, texi also prints the install order and why each transitive package was pulled in.

## `texi scan`

List the packages your source files use and their TeX Live packages, and flag any missing from `texproject.toml`.

| option | description |
|---|---|
| `--resolve` / `--no-resolve` | Resolve names to TeX Live packages (default: resolve). |
| `--debug` | Show every external command texi runs. |

## `texi info`

Show where texi keeps this project's local TeX tree and which tools it found.

## `texi outdated`

Fetch the latest package index and list the packages in `.texenv/` (and `tex-fmt`) that have a newer revision, as `name rOLD → rNEW`. Nothing is downloaded or installed. Run `texi sync` (or `texi update`) to apply the updates.

## `texi update`

The same as `texi sync`: fetch the latest index and update the packages of `.texenv/`. It never touches the system TeX Live.

## `texi tree`

Show the packages installed in `.texenv/` as a dependency tree. By default the roots are the packages that no other installed package depends on. A package that was already shown is marked `(*)` and its subtree is not repeated; `…` marks a subtree cut by `--depth`. The binary halves (`name.<arch>`) of packages are left out.

| option | description |
|---|---|
| `--depth`, `-d <n>` | Show at most `n` levels below each root (`0` shows the roots only). |
| `--explicit` | Only the dependencies in `texproject.toml` and what they depend on. |
| `--package`, `-p <name>` | Show only this package and its dependencies. |
| `--invert`, `-i` | With `--package`: show the packages that depend on it instead. |
| `--project-only` / `--no-project-only` | With `--package --invert` (default `--project-only`): list only packages that are in `texproject.toml`. A dependency that runs through an unlisted package is shown as a direct link between the two listed ones. |
| `--debug` | Show every external command texi runs. |

## `texi activate`

Print shell commands that put `.texenv/` first, so `pdflatex`, `kpsewhich`, `latexmk` and the rest in your shell come from the project's tree:

```sh
eval "$(texi activate)"      # bash, zsh, sh
texi activate --shell fish | source
eval "$(texi activate --off)"   # undo
```

It sets `PATH` (the tree's `bin/<arch>` first), the `TEXMF*` variables that point only at `.texenv/`, and `TEXI_ENV`, and unsets the variables that would add outside trees (`TEXINPUTS`, `BIBINPUTS`, ...). Only shell code goes to stdout; errors go to stderr with exit code 1, so a failed `eval` does nothing.

| option | description |
|---|---|
| `--shell <bash\|zsh\|sh\|fish>` | Shell syntax. Default: fish if `$SHELL` is fish, else POSIX. |
| `--off` | Take `.texenv/` out of `PATH` and unset the variables. Variables that `activate` unset are not restored. |

## `texi unused`

List the packages in `texproject.toml` that the project does not use, so you can remove them. It needs a finished `texi build`: without the build record every package would look unused.

A package counts as used when

- a file of it was read by the last build (the `.fls`/`.fdb_latexmk` inputs and the paths in the `.log`, which also lists embedded fonts), or
- a `\usepackage`, `\RequirePackage` or `\documentclass` in your sources resolves to it, or
- it is `latexmk`, or `biber`/`biblatex` when a `.bcf` file exists (biber leaves no input behind).

The result has three parts:

| part | meaning |
|---|---|
| unused | no evidence of use: likely safe to remove from `texproject.toml` |
| redundant | not used itself, but a used package depends on it, so it is installed anyway |
| used | everything else; a collection counts as used when it supplies a used package |

A package whose file is shadowed by a copy in your project (for example `acmart.cls` in the project directory) is reported unused, because the build reads your copy. Nothing is removed for you.
