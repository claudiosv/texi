# What is left

## Ship
- [ ] Review the uncommitted work as a whole and commit it. Nothing has been committed since `f312c2b`: SQLite tlpdb index and resolver, log parsers, `.fls`/`.fdb_latexmk` parsers, `archive` install method, dependency resolver, no-tlmgr archive mode, `max_print_line` change, Relocatable label, store folder rename.
- [ ] Push. Only `fdce0c5` is on origin; `9f1dee9`, `9b6d287`, `61c8802` and `f312c2b` are local.
- [ ] Untracked, left alone on purpose: `.claude/`, `.vscode/`, `example/`. Decide about `texmf-env.md` and this file (repo root, untracked).
- [ ] Set GitHub Pages source to "GitHub Actions" in the repo settings. The docs workflow has never run on GitHub, only `mkdocs build --strict` locally.

## Not wired up
- [ ] `texi tree` still uses its own `.fls` loop. `latexmk_files.tree_used_paths` is the replacement, but it resolves relative paths against the `.fls` `PWD` instead of the process directory. Decide, then swap.
- [ ] The new log parsers (`parse_build`, run sequence, `include_info`) are only partly used by the CLI: latexmk-level errors are shown in non-watch builds when the TeX and bib logs have none. Watch mode is unchanged.
- [ ] `texi init` and `texi scan` take the first match when several packages ship a file.
- [ ] `texi remove` does not exist. `archive.uninstall()` is only a function.
- [ ] No cleanup for the archive store (`~/.cache/texi/archive`). Deleting the directory is safe; add a `texi cache` command or GC.

## Known gaps
- [ ] `latexmk` is technically not relocatable, but that should be ok once the
- [ ] `name.ARCH` package is still installed when its parent was skipped (dangling `bin/universal-darwin/latexmk` symlink). Skip it too.
- [ ] The default `mirror.ctan.org` redirected to a host with an SSL chain failure during runs. Handle it by retrying with a different mirror.

## Not tested
- [ ] Standalone trees (`texlive_env = "copy"` and `"install"`) against a real tree: `updmap-sys`, `fmtutil-sys`, `TEXMFSYSVAR`, `script` postactions. Mocks only.
- [ ] Real `install-tl` run for `texlive_env = "install"` with archive mode (it now installs `latex` and `latexmk` through the archive).
- [ ] Real `fmtutil` format builds (only `--byhyphen` ran).
- [ ] Linux and Windows: reflink path (code-reviewed only), architecture detection beyond the mapping table.
- [ ] Concurrent `texi` runs: building the tlpdb index, filling the archive store, and the hishel CTAN cache. No lock file.
- [ ] SQLite without FTS5 or trigram (only simulated with `_FTS_ENABLED=False`).
- [ ] Live `304 Not Modified` for the tlpdb download (mock only), and a real multi-hit CTAN disambiguation.
- [ ] The `rebuild` method of `texi fix-pdf`, and the build and preview Type 3 warning hooks, against a real TeX build.
- [ ] A real lualatex fixture for the `.fls`/`.fdb_latexmk` parsers, a real biber success run, and Windows paths.

## Roadmap (`docs/roadmap.md`)
- [ ] `targets` list replacing `main = "main.tex"`.
- [ ] Own `updmap` replacement (texi writes `updmap.cfg` itself but still runs the `updmap` binary).
- [ ] Own `fmtutil` replacement (same).
- [ ] Match tlmgr's line length for `longdesc` in written `.tlpobj` files (texi re-wraps at 70).
- [ ] Resolve across multiple repositories.
