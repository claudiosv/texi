# Format, export, tree

## `texi fmt [files]...`

Format `.tex` files with `tex-fmt`, using the settings in `[tex-fmt]` of `texproject.toml`. Without arguments it formats every `.tex` file under `src_dir`. The config is rendered and piped to `tex-fmt` on every run, **overriding** any `tex-fmt.toml` on disk. See [`[tex-fmt]`](../configuration.md#tex-fmt).

| option | description |
|---|---|
| `--check`, `-c` | Only report files that need formatting; change nothing. |
| `--verbose`, `-v` | Show tex-fmt's info messages. |
| `--quiet`, `-q` | Hide tex-fmt's warnings. |
| `--error` | Show only errors (same as `--quiet`). |
| `--warn` | Show errors and warnings (the default). |
| `--info` | Show info messages too (same as `--verbose`). |
| `--trace` | Show tex-fmt's trace messages. |
| `--debug` | Show every external command texi runs. |

## `texi export <format>`

Convert the document into `build/export/<format>/`. Tools must be installed first e.g., `brew install pandoc latexml` or `texi add tex4ht`.

| format | tool |
|---|---|
| `markdown` | pandoc (with citeproc and MathJax) |
| `html` | pandoc (with citeproc and MathJax) |
| `tex4ht` | `make4ht` (needs `luaxml`) |
| `latexml` | `latexmlc` (LaTeXML) |

## `texi build-tree`

Show which files the last build actually read, as a directory tree. It relies on the `.fls` file written by `-recorder` (`build.recorder`, on by default).

| option | description |
|---|---|
| `--project-dir <path>` | Project directory. |
| `--fls-filename <str>` | Name of the `.fls` file to read. |
