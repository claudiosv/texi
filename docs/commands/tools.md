# Format, export, tree

## `texi fmt [files]...`

Format `.tex` files with `tex-fmt`, using the settings in `[tex-fmt]` of `texproject.toml`. Without arguments it formats every `.tex` file under `src_dir`. The config is rendered and piped to `tex-fmt --config /dev/stdin` on every run, overriding any `tex-fmt.toml` on disk. See [`[tex-fmt]`](../configuration.md#tex-fmt).

| option | description |
|---|---|
| `--check`, `-c` | Only report files that need formatting; change nothing. |
| `--debug` | Show every external command texi runs. |

## `texi export <format>`

Convert the document into `build/export/<format>/`.

| format | tool |
|---|---|
| `markdown` | pandoc (with citeproc and MathJax) |
| `html` | pandoc (with citeproc and MathJax) |
| `tex4ht` | `make4ht` (needs `luaxml`) |
| `latexml` | `latexmlc` (LaTeXML) |

## `texi tree`

Show which files the last build actually read, as a directory tree. It relies on the `.fls` file written by `-recorder` (`build.recorder`, on by default).

| option | description |
|---|---|
| `--project-dir <path>` | Project directory. |
| `--fls-filename <str>` | Name of the `.fls` file to read. |
