# Cache

## `texi cache`

Inspect the TeX Live package database (tlpdb) and manage the package cache. The database tells you which packages exist, what they contain and which package ships a given file. Without a subcommand it runs `texi cache stats`.

The database is `<repository>/tlpkg/texlive.tlpdb.xz` (about 2.8 MB, 21 MB unpacked). texi turns it into an SQLite index (about 60 MB, built in under a second) and queries that, so cached runs do not parse the text. Both files live in `$XDG_CACHE_HOME/texi/tlpdb/` (default `~/.cache/texi/tlpdb/`) as `<sha1 of the repository>.xz`, `.sqlite` and `.json` (ETag, fetch time and final URI). The cache is reused without network access for 24 hours; after that texi revalidates it with a conditional request, and `--refresh` does so at once. If the server cannot be reached, the cached copy is used with a warning. A missing, corrupt or outdated index (the schema version changed) is rebuilt from the cached `.xz` without a download; the index is replaced atomically, so parallel texi runs are safe. Delete the directory to start over. `--local` files are parsed into a temporary in-memory index on every run. The repository is, in order: `--repository`, `[project] repository` in `texproject.toml`, the `opt_location` setting of the system TeX Live's `tlpkg/texlive.tlpdb` (what `tlmgr option repository` would show), then `https://mirror.ctan.org/systems/texlive/tlnet`.

The same index resolves package names for `texi init`, `texi scan` and `texi add` (see [Resolves package name mismatches](../index.md)).

These options work on `stats`, `search` and `info`:

| option | description |
|---|---|
| `--repository <url>`, `-r` | TeX Live repository to read. |
| `--local <path>` | Read a local tlpdb file (plain or `.xz`) instead, for example `/usr/local/texlive/2026/tlpkg/texlive.tlpdb`. |
| `--refresh` | Ignore the cache age and check the server. |
| `--debug` | Show how the repository was chosen and where the cache lives. |

### `texi cache stats`

Prints the full URI the database was read from (after mirror redirects), whether it came from the cache, when it was fetched, its compressed and unpacked size, the index file with its schema version and size, the number of entries, entries per category, files per section, the number of packages with binaries, and the TeX Live release.

### `texi cache search <query>...`

Searches package names, CTAN keys and descriptions, best match first (exact name, name prefix, name substring, short description, long description, file). Several words must all match.

| option | description |
|---|---|
| `--file`, `-f` | Also search file names and paths, for example `texi cache search binhex.tex -f`. |
| `--regex`, `-e` | Treat the query as a regular expression. |
| `--category <str>`, `-c` | Only this category (`Package`, `Collection`, `Scheme`, `TLCore`, `ConTeXt`). |
| `--limit <n>`, `-n` | Maximum rows (default 25, 0 for all). |
| `--exact` | Only exact package names, CTAN keys or file names. |

### `texi cache info <name>`

Prints everything the database holds about a package: description, CTAN key, path, version, license and topics, dependencies, settings, container sizes and checksums, `execute` and `postaction` lines, and every file section (`runfiles`, `docfiles`, `srcfiles`, `binfiles:<arch>`) with `RELOC/` expanded to `texmf-dist/`. The name may also be a CTAN key. The tlpdb stores settings and release info as `depend` lines of the `00texlive.*` entries (`opt_location:__MASTER__`, `release/2026`). texi keeps these apart from real dependencies: `Depends` lists package names only, and a separate `Settings` section lists the pseudo-records (`settings` in `--json`, next to `depends`). If nothing matches, texi suggests close names.

| option | description |
|---|---|
| `--files` / `--no-files` | List the files (default) or only count them. |
| `--all-files` | Don't cut file lists after 200 entries. |
| `--json` | Print all fields as JSON. |

### `texi cache prune`

Remove entries from the shared package cache (`~/.cache/texi/archive/`) that this project's `.texenv/` does not use: old revisions of installed packages and packages that are not installed. The cache is shared by all projects, so a package another project needs is downloaded again on its next `texi sync`. Incomplete downloads (partial files and staging directories untouched for an hour) are removed too.

| option | description |
|---|---|
| `--dry-run`, `-n` | Only list what would be removed. |
