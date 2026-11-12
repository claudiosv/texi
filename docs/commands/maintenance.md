# TeX Live maintenance

Commands that act on the TeX Live install itself.

## `texi doctor`

Check your TeX install and warn about stale user-level formats.

| option | description |
|---|---|
| `--engine <str>` | Engine to check (default `pdflatex`). |

## `texi formats`

Rebuild LaTeX format files (`fmtutil`). Offers to fix TeX Live permissions if sudo would be needed.

| option | description |
|---|---|
| `--user` | Rebuild user formats (`fmtutil-user`). |
| `--missing` | Only build formats that don't exist yet. |
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |

## `texi updmap`

Rebuild font maps (`updmap`). Run this after installing fonts.

| option | description |
|---|---|
| `--user` | Regenerate user font maps (`updmap-user`). |
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |

## `texi mktexlsr`

Rebuild TeX's file-name databases (`ls-R`).

| option | description |
|---|---|
| `--local` | Only index `.texenv/` (no sudo). |
| `--yes`, `-y` | Don't ask; use sudo if the TeX Live tree isn't writable. |

## `texi luaotfload`

Rebuild the LuaTeX font database, for example when `lualatex` can't find a font.

| option | description |
|---|---|
| `--force` | Rebuild the whole database (`--update --force`). |

## `texi tlpdb`

Inspect the TeX Live package database (tlpdb): which packages exist, what they contain and which package ships a given file. Without a subcommand it runs `texi tlpdb stats`.

The database is `<repository>/tlpkg/texlive.tlpdb.xz` (about 2.8 MB, 21 MB unpacked). texi turns it into an SQLite index (about 60 MB, built in under a second) and queries that, so cached runs do not parse the text. Both files live in `$XDG_CACHE_HOME/texi/tlpdb/` (default `~/.cache/texi/tlpdb/`) as `<sha1 of the repository>.xz`, `.sqlite` and `.json` (ETag, fetch time and final URI). The cache is reused without network access for 24 hours; after that texi revalidates it with a conditional request, and `--refresh` does so at once. If the server cannot be reached, the cached copy is used with a warning. A missing, corrupt or outdated index (the schema version changed) is rebuilt from the cached `.xz` without a download; the index is replaced atomically, so parallel texi runs are safe. Delete the directory to start over. `--local` files are parsed into a temporary in-memory index on every run. The repository is, in order: `--repository`, `[project] repository` in `texproject.toml`, the `opt_location` setting of the system TeX Live's `tlpkg/texlive.tlpdb` (what `tlmgr option repository` shows; read from the file, tlmgr is not run, and `__MASTER__` is skipped), then `https://mirror.ctan.org/systems/texlive/tlnet`.

The same index resolves package names for `texi init`, `texi scan` and `texi add` (see [Resolves package name mismatches](../index.md)).

These options work on every subcommand:

| option | description |
|---|---|
| `--repository <url>`, `-r` | TeX Live repository to read. |
| `--local <path>` | Read a local tlpdb file (plain or `.xz`) instead, for example `/usr/local/texlive/2026/tlpkg/texlive.tlpdb`. |
| `--refresh` | Ignore the cache age and check the server. |
| `--debug` | Show how the repository was chosen and where the cache lives. |

### `texi tlpdb stats`

Prints the full URI the database was read from (after mirror redirects), whether it came from the cache, when it was fetched, its compressed and unpacked size, the index file with its schema version and size, the number of entries, entries per category, files per section, the number of packages with binaries, and the TeX Live release.

### `texi tlpdb search <query>...`

Searches names, CTAN keys and descriptions, best match first (exact name, name prefix, name substring, short description, long description, file). Several words must all match.

| option | description |
|---|---|
| `--file`, `-f` | Also search file names and paths, for example `texi tlpdb search binhex.tex -f`. |
| `--regex`, `-e` | Treat the query as a regular expression. |
| `--category <str>`, `-c` | Only this category (`Package`, `Collection`, `Scheme`, `TLCore`, `ConTeXt`). |
| `--limit <n>`, `-n` | Maximum rows (default 25, 0 for all). |
| `--exact` | Only exact package names, CTAN keys or file names. |

### `texi tlpdb info <name>`

Prints everything the database holds about a package: description, CTAN key, path, version, license and topics, dependencies, settings, container sizes and checksums, `execute` and `postaction` lines, and every file section (`runfiles`, `docfiles`, `srcfiles`, `binfiles:<arch>`) with `RELOC/` expanded to `texmf-dist/`. The name may also be a CTAN key. The tlpdb stores settings and release info as `depend` lines of the `00texlive.*` entries (`opt_location:__MASTER__`, `release/2026`). texi keeps these apart from real dependencies: `Depends` lists package names only, and a separate `Settings` section lists the pseudo-records (`settings` in `--json`, next to `depends`). If nothing matches, texi suggests close names.

| option | description |
|---|---|
| `--files` / `--no-files` | List the files (default) or only count them. |
| `--all-files` | Don't cut file lists after 200 entries. |
| `--json` | Print all fields as JSON. |
