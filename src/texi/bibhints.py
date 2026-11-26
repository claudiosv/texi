"""Hints for BibTeX/Biber diagnostics: what is probably wrong and what to do.

The log parsers (:mod:`texi.logparse_bib`) only say what the tool printed.
:func:`explain` adds a one-line hint, using the parsed ``.bib`` files
(:mod:`texi.bibparse`) to name lines, near-miss keys and so on.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from texi.bibparse import BibEntry, BibFile
from texi.logparse_core import Diagnostic

__all__ = ["explain"]

type Bibs = Mapping[str, BibFile]

# Common biblatex fields, for "did you mean" on Datamodel warnings.
_FIELDS = (
    "abstract address annotation author booktitle chapter doi edition editor "
    "eprint eprinttype eventdate institution isbn issn issue issuetitle journal "
    "journaltitle keywords language location month note number organization "
    "pages publisher pubstate school series shortauthor shorttitle subtitle "
    "title translator type url urldate venue version volume year date"
).split()
_ALIAS = {
    "journaltitle": "journal",
    "journal": "journaltitle",
    "location": "address",
    "address": "location",
    "date": "year",
    "year": "date",
}

_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_CTX_KEY = re.compile(r"@\w+\s*[{(]\s*([^,\s]+)")


def _where(path: str, line: int | None) -> str:
    name = Path(path).name
    return f"{name}:{line}" if line else name


def _find_entries(bibs: Bibs, key: str | None) -> list[tuple[str, BibEntry]]:
    if not key:
        return []
    return [(p, e) for p, bf in bibs.items() for e in bf.entries if e.key == key]


def _bib_for(d: Diagnostic, bibs: Bibs) -> tuple[str, BibFile] | None:
    if not d.file:
        return None
    name = Path(d.file).name
    for p, bf in bibs.items():
        if Path(p).name == name:
            return p, bf
    return None


def _missing_entry(d: Diagnostic, bibs: Bibs) -> str | None:
    m = re.search(r"database entry for ['\"]([^'\"]+)['\"]", d.message)
    if not m:
        return None
    key = m.group(1)
    keys = [e.key for bf in bibs.values() for e in bf.entries]
    base = f"no entry '{key}' in any .bib read"
    if hits := _find_entries(bibs, key):
        where = ", ".join(_where(p, e.line) for p, e in hits)
        return (
            f"'{key}' is defined at {where}, but {d.source} did not find it: check that "
            "this .bib is listed in \\bibliography or \\addbibresource and fix any "
            "errors before the entry, then rebuild"
        )
    if not keys:
        return (
            f"{base}: check the \\cite key and that the .bib is listed in "
            "\\bibliography or \\addbibresource"
        )
    lower = {k.lower(): k for k in keys}
    if (k := lower.get(key.lower())) and k != key:
        return f"{base}; '{k}' differs only in case, and keys are case-sensitive: fix the \\cite"
    near = difflib.get_close_matches(key, keys, n=3, cutoff=0.6)
    if near:
        names = ", ".join(repr(k) for k in near)
        return f"{base}; did you mean {names}? Fix the \\cite key or add the entry"
    return f"{base}; add it or remove \\cite{{{key}}}"


def _duplicate(d: Diagnostic, bibs: Bibs) -> str | None:
    m = re.search(r"key '([^']+)'", d.message)
    key = m.group(1) if m else d.entry
    if not key:
        for c in d.context:
            if cm := _CTX_KEY.search(c):
                key = cm.group(1)
                break
    if not key:
        return None
    hits = _find_entries(bibs, key)
    if len(hits) < 2:
        return (
            f"key '{key}' is defined more than once: rename or delete the extra entry"
        )
    where = ", ".join(_where(p, e.line) for p, e in hits)
    identical = all(e.fields == hits[0][1].fields for _, e in hits)
    tail = (
        "identical entries: delete one"
        if identical
        else "the entries differ: keep one or rename the other"
    )
    return f"'{key}' is defined at {where}; {tail}"


def _undefined_string(d: Diagnostic, bibs: Bibs) -> str | None:
    m = re.search(r'string name "([^"]+)"', d.message)
    if not m:
        return None
    name = m.group(1)
    defined = [
        p for p, bf in bibs.items() if name.lower() in {s.lower() for s in bf.strings}
    ]
    if defined:
        return (
            f"'{name}' is defined in {Path(defined[0]).name}, but bibtex needs "
            "@string before its first use, in a database listed earlier"
        )
    return (
        f"'{name}' is a bare word in a field value. Wrap text in {{...}} or define "
        f'@string{{{name} = "..."}} first; only numbers, months and macros may be bare'
    )


def _empty_field(d: Diagnostic, bibs: Bibs) -> str | None:
    m = re.match(r"empty (\S+) in (\S+)", d.message)
    if not m:
        return None
    fld, key = m.groups()
    hits = _find_entries(bibs, key)
    loc = f" ({_where(hits[0][0], hits[0][1].line)})" if hits else ""
    typ = f"@{hits[0][1].type} " if hits else ""
    return (
        f"{typ}'{key}'{loc} has no '{fld}', which the style wants. Add "
        f"{fld}={{...}}, or use a type that does not need it (e.g. @misc)"
    )


def _datamodel(d: Diagnostic, bibs: Bibs) -> str | None:
    key = d.entry
    hits = _find_entries(bibs, key)
    loc = f" at {_where(hits[0][0], hits[0][1].line)}" if hits else ""
    if m := re.search(r"Invalid field '([^']+)' for entrytype '([^']+)'", d.message):
        fld, typ = m.groups()
        near = difflib.get_close_matches(fld.lower(), _FIELDS, n=1, cutoff=0.75)
        tip = f" Typo for '{near[0]}'?" if near else ""
        return (
            f"'{fld}' is not a field of @{typ}{loc}.{tip} Remove it, move it to "
            "note or keywords, or declare it with \\DeclareDatamodelFields"
        )
    if m := re.search(r"Missing mandatory field '([^']+)'", d.message):
        fld = m.group(1)
        alt = f" ('{_ALIAS[fld]}' works as an alias)" if fld in _ALIAS else ""
        return f"add {fld}={{...}} to '{key}'{loc}{alt}"
    return None


def _missing_source(d: Diagnostic, bibs: Bibs) -> str | None:
    if m := _QUOTED.search(d.message):
        name = m.group(1)
    elif m := re.search(r"database file (\S+)", d.message):
        name = m.group(1)
    elif d.file:
        name = d.file
    else:
        return None
    name = Path(name).name
    near = difflib.get_close_matches(
        name, [Path(p).name for p in bibs], n=1, cutoff=0.6
    )
    tip = f" A similar file exists: {near[0]}." if near else ""
    return (
        f"'{name}' was not found.{tip} Put it in the project, src_dir or a "
        "build.bib_dirs entry in texproject.toml, or fix the name in "
        "\\addbibresource / \\bibliography"
    )


def _bibtex_command(d: Diagnostic, bibs: Bibs) -> str | None:
    low = d.message.lower()
    if "style" in low:
        return "no \\bibstyle in the .aux: add \\bibliographystyle{plain}, then rerun LaTeX before bibtex"
    if "bibdata" in low:
        return "no \\bibdata in the .aux: add \\bibliography{refs} (no .bib extension), then rerun LaTeX"
    if "citation" in low:
        return "nothing is cited: add \\cite{...} or \\nocite{*}, then rerun LaTeX before bibtex"
    return None


def _bibtex_syntax(d: Diagnostic, bibs: Bibs) -> str | None:
    generic = (
        "a field is probably missing its trailing comma, or a brace/quote is unbalanced"
    )
    found = _bib_for(d, bibs)
    if not found or not d.line:
        return generic
    path, bf = found
    text = bf.lines[d.line - 1].strip() if 0 < d.line <= len(bf.lines) else ""
    start = [e for e in bf.entries if e.line <= d.line]
    ent = (
        f" in '{start[-1].key}' (entry starts at line {start[-1].line})"
        if start
        else ""
    )
    return (
        f"{_where(path, d.line)}{ent}: `{text[:60]}` is not valid here. "
        "Usually the previous field lacks a trailing comma, or a brace/quote is unbalanced"
    )


_STATIC = {
    "biber-missing-control-file": (
        "biber reads the .bcf that LaTeX writes: run LaTeX first (texi build does), "
        "and check biblatex is loaded with backend=biber"
    ),
    "bibtex-missing-style": (
        "\\bibliographystyle names a .bst that is not installed: check the spelling, "
        "or install the package that ships it with `texi add`"
    ),
    "bibtex-repeated-bibstyle": (
        "the .aux has two \\bibliographystyle commands: keep one, and check included files"
    ),
    "biber-fatal": "biber stopped: fix the errors above it first",
}

_HANDLERS: dict[str, Callable[[Diagnostic, Bibs], str | None]] = {
    "bibtex-missing-entry": _missing_entry,
    "biber-missing-entry": _missing_entry,
    "biber-duplicate-entry": _duplicate,
    "bibtex-repeated-entry": _duplicate,
    "bibtex-undefined-string": _undefined_string,
    "bibtex-empty-field": _empty_field,
    "biber-datamodel": _datamodel,
    "biber-source-not-found": _missing_source,
    "biber-cannot-open": _missing_source,
    "biber-file-not-found": _missing_source,
    "bibtex-missing-database": _missing_source,
    "bibtex-missing-command": _bibtex_command,
    "bibtex-syntax": _bibtex_syntax,
}


def explain(d: Diagnostic, bibs: Bibs) -> str | None:
    """Return a hint for a bibtex/biber diagnostic, or None.

    Parameters
    ----------
    d
        A diagnostic from :mod:`texi.logparse_bib`.
    bibs
        Parsed ``.bib`` files of the project, keyed by path. May be empty.
    """
    if d.source not in {"bibtex", "biber"} or not d.code:
        return None
    if handler := _HANDLERS.get(d.code):
        if hint := handler(d, bibs):
            return hint
    return _STATIC.get(d.code)
