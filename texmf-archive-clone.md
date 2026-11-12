# Archive clone mode: a standalone `.texenv/` built from the texi store

Status: design. The parts marked **(exists)** are in the code today. The parts marked **(new)** are what this document proposes.

## Goal

Every project's `.texenv/` is a complete, self-contained TeX Live tree. texi builds it by cloning packages from the shared store in `~/.cache/texi/archive/`. Nothing on the machine outside `.texenv/` takes part in a build:

- no Homebrew TeX Live, no `/usr/local/texlive`, no `tlmgr`;
- no `TEXMFHOME`, `TEXMFLOCAL`, `TEXMFVAR`, `TEXMFCONFIG`, `TEXMFSYS*` from the system or from the user's shell;
- texi installs TeX Live itself, from CTAN containers.

This is what `install_method = "archive"` (already the default, `config.py:229`) does when it builds the whole tree, not only extra packages. In that method `texlive_env` no longer chooses how the tree is made: the tree is always a standalone clone. Today `texlive_env = "install"` runs the official `install-tl` (`texenv.install_env`, `src/texi/texenv.py:196`) and only then uses the store for extra packages, and `copy` copies from the system TeX Live. Both would be replaced for the archive method, and `usermode` (a tlmgr usertree over the system TeX Live) would be rejected or ignored, because it reads the system tree. `install_method = "tlmgr"` keeps the current `texlive_env` behavior.

## Store and tree

The store holds one extracted container per package build **(exists)**:

```
~/.cache/texi/archive/<name>-<revision>-<checksum7>/<PACKAGE>/...
~/.cache/texi/archive/<name>-<revision>-<checksum7>/texi-archive.json
```

The store would need to take texlive versions into account i.e. install-tl is done once at e.g. ~/.cache/texi/2026. the archive folder should be shared across different .texenvs with the same texlive version.

A `.texenv/` is a set of copy-on-write clones of these files (APFS `clonefile`; plain copy where cloning fails). It never uses hard links or symlinks into the store **(exists, `archive.py`)**. Each tree therefore has its own bytes: editing a file in one project cannot change the store or another project.

The tree has the layout of a normal TeX Live installation:

```
.texenv/
  bin/<arch>/         binaries and script links (pdflatex, kpsewhich, fmtutil, updmap, mktexlsr, latexmk)
  texmf-dist/         runfiles of all packages; texmf-dist/web2c/texmf.cnf lives here
  texmf-var/          generated: formats, font maps, ls-R   (TEXMFVAR and TEXMFSYSVAR)
  texmf-config/       generated: updmap.cfg overrides       (TEXMFCONFIG and TEXMFSYSCONFIG)
  texmf-local/        empty; stands in for TEXMFLOCAL
  texmf-home/         empty; stands in for TEXMFHOME
  tlpkg/              texlive.tlpdb, tlpobj/<name>.tlpobj, texi-archive.json, TeXLive/*.pm
```

`<arch>` is the architecture of the machine, for example `universal-darwin` or `x86_64-linux`. Only that architecture's binary containers are installed **(exists)**.

## Why clone files instead of linking

kpathsea finds its configuration from the real path of the executable. `SELFAUTOLOC` is the directory of the binary, `SELFAUTOPARENT` is its parent, and so on. The default `texmf.cnf` derives `TEXMFROOT` from `$SELFAUTOPARENT`.

If `bin/<arch>/pdflatex` were a symlink into `~/.cache/texi/archive/...`, then `SELFAUTOPARENT` would resolve into the store and `TEXMFROOT` would point at the wrong place. A cloned file has its own inode in `.texenv/bin/<arch>/`, so the tree finds itself. This is the main reason the clone must be a real copy.

The `bin/<arch>/x -> ../../texmf-dist/scripts/...` links of script packages stay relative and stay inside the tree.

## Building the tree

Order of steps for `install_method = "archive"`:

1. **Pick the architecture** from the platform, and check that the tlpdb has `binfiles arch=<arch>` sections for it.
2. **Resolve the roots** with `texi.resolver` **(exists)**. Roots: the base of `scheme-minimal`, `latex`, `latexmk`, and the packages the project needs. The resolver returns the full dependency order.
3. **Fetch** missing containers into the store, verify their checksums **(exists)**.
4. **Clone** the files into `.texenv/` **(exists)**. Binary containers go into `bin/<arch>/`.
5. **Write `tlpkg/`**: `texlive.tlpdb`, `tlpobj/<name>.tlpobj`, `texi-archive.json` **(exists)**. The `00texlive.installation` stanza must *not* contain `setting_usertree`, so the tree counts as standalone (`cli._standalone`, `texenv.is_usertree`).
6. **Install the infrastructure packages** the Perl tools need: `texlive.infra` (the `tlpkg/TeXLive/*.pm` modules that `fmtutil` and `updmap` load), `texlive-scripts` and `texlive-scripts-extra`. The system must supply only `perl`. `tlmgr` itself is not needed and is never called.
7. **Run the post-install actions** with `tlactions.run(standalone=True)` **(exists)**. They write `texmf-dist/web2c/updmap.cfg`, `texmf-dist/web2c/fmtutil.cnf`, the language files in `texmf-var`, then run `updmap-sys` and `fmtutil-sys`. Formats end up in `texmf-var/web2c/`.
8. **Run `mktexlsr`** over `texmf-dist`, `texmf-var` and `texmf-local` **(new, unless `tlactions` already does it)**. Every search path entry written with `!!` needs an `ls-R` file. Without it kpathsea does not search that tree.

Do not hand-write `texmf.cnf`. The one in `texmf-dist/web2c/` comes from a package container and works for any location of the tree.

## Search path and variables

### Search path

The stock `texmf.cnf` builds the search path as:

```
TEXMF = {$TEXMFCONFIG,$TEXMFVAR,$TEXMFHOME,!!$TEXMFLOCAL,!!$TEXMFSYSCONFIG,!!$TEXMFSYSVAR,!!$TEXMFDIST}
```

Do not just rewrite `TEXMF`. Point every variable it uses into the tree, and the whole path is inside `.texenv/`.

### Variables

Let `R` be the absolute, resolved path of `.texenv/`.

| Variable | Value | Why |
|---|---|---|
| `TEXMFHOME` | `R/texmf-home` | Replaces `~/Library/texmf`. Empty, but a place for project-local additions. |
| `TEXMFLOCAL` | `R/texmf-local` | Replaces the system's local tree. The default can point *outside* the tree. |
| `TEXMFVAR` | `R/texmf-var` | Replaces `~/Library/texlive/<year>/texmf-var`. |
| `TEXMFCONFIG` | `R/texmf-config` | Replaces `~/Library/texlive/<year>/texmf-config`. |
| `TEXMFSYSVAR` | `R/texmf-var` | Same dir as `TEXMFVAR`: one place for formats and maps. |
| `TEXMFSYSCONFIG` | `R/texmf-config` | Same dir as `TEXMFCONFIG`. |
| `TEXMFROOT` | `R` | Normally derived from the binary path. Setting it guards against a stray value. |
| `TEXMFDIST` | `R/texmf-dist` | Same reason. |
| `TEXMFCNF` | `R/texmf-dist/web2c` | Only this `texmf.cnf` is read. A `TEXMFCNF` from the user's shell would otherwise win. |
| `PATH` | `R/bin/<arch>` first | `latexmk`, `fmtutil`, `updmap`, `luaotfload-tool`, `mktexlsr` resolve to the tree's copies. |

Remove these from the environment if safe, because they can add trees or files from outside:

```
TEXMFAUXTREES  TEXMFDOTDIR  TEXINPUTS  BIBINPUTS  BSTINPUTS  TFMFONTS  TEXFONTS
OPENTYPEFONTS  TTFONTS  T1FONTS  VFFONTS  TEXFORMATS  TEXPOOL  TEXPSHEADERS
```

The project's own `TEXINPUTS` and `BIBINPUTS` (`config.build.tex_inputs`, `config.build.bib_dirs`) are added back by the generated latexmkrc after the scrub **(exists, `cli.py:392`)**. They are project paths, not system paths.

Setting the variables is a second line of defence. With the tree's `bin/<arch>` first on `PATH`, and with real copies there, kpathsea already locates the tree's `texmf.cnf` from the binary. The variables fix the per-user directories, which have no relation to the binary location, and they block overrides from the user's shell.

### In texi

Today a standalone tree gets only `PATH`, and a usertree gets `TEXMFHOME`/`TEXMFVAR`/`TEXMFCONFIG` (`tlactions.tool_env`, `src/texi/tlactions.py:468`). Archive mode needs one function that returns the full set, used everywhere instead of the three copies in `cli._texenv_vars`, `tlactions.tool_env` and the latexmkrc template:

```python
_SCRUBBED = (
    "TEXMFAUXTREES",
    "TEXMFDOTDIR",
    "TEXINPUTS",
    "BIBINPUTS",
    "BSTINPUTS",
    "TFMFONTS",
    "TEXFONTS",
    "OPENTYPEFONTS",
    "TTFONTS",
    "T1FONTS",
    "VFFONTS",
    "TEXFORMATS",
    "TEXPOOL",
    "TEXPSHEADERS",
)


def clone_env(env_dir: Path, arch: str, base: Mapping[str, str]) -> dict[str, str]:
    """Environment in which only `env_dir` is on TeX's search path."""
    root = env_dir.resolve()
    env = {k: v for k, v in base.items() if k not in _SCRUBBED}
    env |= {
        "TEXMFROOT": str(root),
        "TEXMFDIST": str(root / "texmf-dist"),
        "TEXMFLOCAL": str(root / "texmf-local"),
        "TEXMFHOME": str(root / "texmf-home"),
        "TEXMFVAR": str(root / "texmf-var"),
        "TEXMFSYSVAR": str(root / "texmf-var"),
        "TEXMFCONFIG": str(root / "texmf-config"),
        "TEXMFSYSCONFIG": str(root / "texmf-config"),
        "TEXMFCNF": str(root / "texmf-dist" / "web2c"),
        "PATH": f"{root / 'bin' / arch}{os.pathsep}{base.get('PATH', '')}",
    }
    return env
```

The latexmkrc must do the same from Perl. The `$ENV{...}` block at `cli.py:380` should set the same ten variables and `delete $ENV{...}` the scrubbed ones, based on `$root/.texenv`. Keep the `glob("$root/.texenv/bin/*")` line for `PATH`.

`tlactions` runs `updmap-sys` and `fmtutil-sys` in a standalone tree. Pass them `clone_env(...)` too. Otherwise `TEXMFSYSVAR` falls back to the tree default and a user-level `TEXMFVAR` can still redirect output.

## Check that it works

Run these with the environment from `clone_env` and expect every path to start with `R`:

```sh
kpsewhich -var-value=TEXMFROOT
kpsewhich -var-value=TEXMFLOCAL
kpsewhich -var-value=TEXMFHOME
kpsewhich -var-value=TEXMFVAR
kpsewhich -expand-path='$TEXMF'
kpsewhich article.cls
kpsewhich pdflatex.fmt
kpsewhich -all texmf.cnf
```

`kpsewhich -all texmf.cnf` must print exactly one file. A second hit is a leak. `kpsewhich -expand-path='$TEXMF'` must not contain `/usr/local`, `/opt/homebrew` or `~/Library`.

To confirm that nothing reads the system, temporarily make `/usr/local/texlive` and the Homebrew tree unreadable, or run on a machine without TeX Live, and build a document. A build that needs no `ls-R` outside `.texenv/` has no leak.

## Caveats

- **Binaries need to be installed once per architecture and version.** A store entry for `universal-darwin` is not usable on `x86_64-linux`. Keep one store, with several arch entries if needed.
- **Fonts.** `OSFONTDIR` still lets `xetex` and `luaotfload` see the system fonts. That is usually wanted. If it is not, set `OSFONTDIR` to an empty directory in the tree, and rebuild the luaotfload cache in `texmf-var`.
- **`fmtutil` and `updmap` need `perl`.** macOS ships one. Nothing else is taken from the system.
- **Moving the tree.** `texmf-var` holds formats and `ls-R` files with absolute paths in some caches (the luaotfload database, `texmf-var/web2c/*.log`). After moving or renaming a project, re-run the post-install actions and generate the ls-R files with our own implementation of `mktexlsr`.
- **Upgrades.** A new TeX Live release changes `<revision>` and checksums, so new store entries appear next to the old ones. Pin a frozen `tlnet` repository (`repository` in the config) to keep a project reproducible. `texi-archive.json` in the tree lists the exact store IDs it was cloned from.
- **Disk use.** Copy-on-write clones share blocks with the store on APFS and cost almost nothing until edited. On volumes without cloning (another disk, ext4) every tree is a full copy.
- **Unverified.** The `texlive.infra`/`texlive-scripts` list in step 6, and whether `tlactions.run` already calls `mktexlsr`, are not confirmed against the code. Check both before implementing.
- `latexmk` is technically not relocatable, but that should be ok once we figure out TEXMFROOT and TEXMFDIST and ~/.cache/texi and .texenv structure. Ideally latexmk is a clone in .texenv.
