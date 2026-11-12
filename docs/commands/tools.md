# Format, export, build-tree

## `texi fmt [files]...`

Format `.tex` files with [tex-fmt](https://github.com/wgunderwood/tex-fmt), using the settings in `[tex-fmt]` of `texproject.toml`. Without arguments it formats every `.tex` file under `src_dir`. The config is rendered and piped to `tex-fmt` on every run, **overriding** any `tex-fmt.toml` on disk. See [`[tex-fmt]`](../configuration.md#tex-fmt).

| option | description |
|---|---|
| `--check`, `-c` | Only report files that need formatting; change nothing. |
| `--verbose`, `-v` | Show tex-fmt's info messages. |
| `--quiet`, `-q` | Print nothing, not even tex-fmt's output; the exit code (1 if tex-fmt failed) is the only signal. Not allowed with `--check`. |
| `--error` | Show only errors. |
| `--warn` | Show errors and warnings (the default). |
| `--info` | Show info messages too (same as `--verbose`). |
| `--trace` | Show tex-fmt's trace messages. |
| `--raw` | Print tex-fmt's output as is, without parsing it. `--debug` does the same. |
| `--debug` | Show every external command texi runs. |

## `texi export <format>`

Convert every file in `project.targets` (or `build.default_files`) into `build/export/<format>/`, one output per file, named after the file. Tools must be installed first e.g., `brew install pandoc latexml` or `texi add tex4ht`.

| format | tool |
|---|---|
| `markdown` | pandoc (with citeproc and MathJax) |
| `html` | pandoc (with citeproc and MathJax) |
| `tex4ht` | `make4ht` (needs `luaxml`) |
| `latexml` | `latexmlc` (LaTeXML) |
| `expand` | `latexpand`: one `.tex` file with every `\input` and `\include` inlined, written to `build/export/expand/<target>.tex`. Installed into an existing `.texenv/` on first use. |

## `texi build-tree`

Show which files the last build actually read, as a directory tree. It relies on the `.fls` file written by `-recorder` (`build.recorder`, on by default).

| option | description |
|---|---|
| `--project-dir <path>` | Project directory. |
| `--fls-filename <str>` | Name of the `.fls` file to read. |
