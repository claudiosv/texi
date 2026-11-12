# Commands

Run `texi --help` for the list, and `texi COMMAND --help` for details on one command.

| group | commands | page |
|---|---|---|
| Project | `init`, `add`, `install`, `sync`, `outdated`, `scan`, `info`, `unused`, `list`, `tree`, `activate` | [Project setup](project.md) |
| Build | `build`, `preview`, `clean` | [Build, preview, clean](build.md) |
| Build | `fix-pdf` | [fix-pdf](fix-pdf.md) |
| Build | `fmt`, `export`, `build-tree` | [Format, export, build-tree](tools.md) |
| TeX Live maintenance | `doctor`, `formats`, `updmap`, `mktexlsr`, `luaotfload` | [TeX Live maintenance](maintenance.md) |
| Cache | `cache` (`stats`, `search`, `info`, `prune`) | [Cache](cache.md) |

Global option: `--debug` shows every external command texi runs, along with its output.
