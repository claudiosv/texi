# API Reference

texi is a CLI; these modules hold logic that does not depend on the CLI and are documented from their docstrings.

- [pdffonts](pdffonts.md): detect and convert Type 3 fonts.
- [latexmk_files](latexmk_files.md): parse latexmk `.fls` and `.fdb_latexmk` files.
- [ctan](ctan.md): typed client for the CTAN JSON API with an HTTP cache.
- [logparse](logparse.md): parse TeX, BibTeX and biber logs.
- [config](config.md): the `texproject.toml` models.
- [tlpdb](tlpdb.md): parse, fetch, cache and search the TeX Live package database.
- [resolver](resolver.md): dependency resolution modeled on TeX Live's `expand_dependencies`.
- [archive](archive.md): shared package store and copy-on-write installs into `.texenv/`.
- [tlactions](tlactions.md): `execute` and `postaction` handling (maps, formats, hyphenation) without `tlmgr`.
