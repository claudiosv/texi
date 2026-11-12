"""Parser for BibTeX and Biber `.blg` logs."""

import re
from dataclasses import dataclass, field

from texi.logparse_core import (
    MAX_MATCH,
    Diagnostic,
    Kind,
    clean_path,
    split_lines,
    to_int,
)

# biber:  `[37] Utils.pm:480> ERROR - message`  (the prefix is optional)
_BIBER = re.compile(r"^(?:\[\d+\] \S+> )?(INFO|WARN|ERROR|FATAL|DEBUG|TRACE) - (.*)$")
_BIBER_COUNT = re.compile(r"^(ERRORS|WARNINGS): (\d+)")
_BIBER_VERSION = re.compile(r"This is Biber (\S+)")
_BIBER_TMP = re.compile(r"_[A-Za-z0-9]{4,6}\.utf8$")
_BIBER_SYNTAX = re.compile(
    r"BibTeX subsystem: (\S+?), line (\d+), ((?:syntax )?error|warning): (.*)$"
)

_BT_WARN = re.compile(r"^Warning--(.*)$")
_BT_AT = re.compile(r"^(.*?)---line (\d+) of file (.+?)\s*$")
_BT_WHILE = re.compile(r"^(.*?)---while reading file (.+?)\s*$")
_BT_LOC_ONLY = re.compile(r"^---line (\d+) of file (.+?)\s*$")
_BT_ERR_LEAD = re.compile(
    r"^(I couldn't open|I found no|I was expecting|Illegal|Repeated entry|Sorry---|"
    r"You're missing|You're the|I'm confused|I don't know)"
)
_BT_COUNT = re.compile(r"^\(There (?:was|were) (\d+) (error|warning) messages?\)")
_BT_COUNT2 = re.compile(r"^\(There (?:was|were) (\d+) (error|warning)s?\)")
_BT_DB = re.compile(r"^Database file #\d+: (.+?)\s*$")
_BT_USED = re.compile(r"^You've used (\d+) entr(?:y|ies),?")
_BT_STYLE = re.compile(r"^The style file: (.+?)\s*$")
_BT_AUX = re.compile(r"^The top-level auxiliary file: (.+?)\s*$")
_BT_CTX = re.compile(r"^ : ")
_BT_ENTRY_IN = re.compile(r"\bin (\S+)$")
_BT_ENTRY_FOR = re.compile(r'(?:entry|for) "([^"]+)"')
_BT_EXPECT_LINE = re.compile(r"--line (\d+) of file (.+?)\s*$")

_ENTRY = re.compile(r"[Ee]ntry(?: key)? '([^']+)'")
_AT_LINE = re.compile(r"\bat line (\d+)\b")
_ANY_LINE = re.compile(r"\bline (\d+)\b")
_FILE_Q = re.compile(
    r"\b(?:file|Cannot find|source|open|in) '([^']+\.[A-Za-z0-9]{1,6})'"
)
_PAREN_FILE = re.compile(r"\(([^()\s]+\.(?:bib|bcf|bbl|bst|xml|dbx))\)")


@dataclass
class BibLogInfo:
    tool: str | None = None  # "bibtex" | "biber"
    version: str | None = None
    aux: str | None = None
    style: str | None = None
    databases: list[str] = field(default_factory=list)
    entries_used: int | None = None
    error_count: int | None = None  # from the tool's own summary
    warning_count: int | None = None


_HASH_NAME = re.compile(r"^[0-9a-f]{32}$")
_BCF_SOURCE = re.compile(r"<bcf:datasource\b[^>]*>([^<]+)</bcf:datasource>")
_RUNAWAY = re.compile(r"possible runaway string started at line (\d+)")
_FIELD_START = re.compile(r"\s*([A-Za-z][\w-]*)\s*=")


def biber_temp_sources(bcf_text: str) -> dict[str, str]:
    """``md5 -> data source name`` for the files biber copies to ``biber_tmp_*/<md5>_<pid>.utf8``.

    biber names the copy after the md5 of the data source as written in the
    ``.bcf``, so the name of the log's file can be mapped back.
    """
    import hashlib

    return {
        hashlib.md5(name.strip().encode()).hexdigest(): name.strip()
        for name in _BCF_SOURCE.findall(bcf_text)
    }


def is_biber_temp_name(name: str | None) -> bool:
    """True for the md5 file name biber reports for a copy of a ``.bib``."""
    return bool(name and _HASH_NAME.match(name))


def explain_syntax(d: Diagnostic, source_lines: list[str]) -> str | None:
    """Say what to read and what is probably wrong for a btparse warning.

    Parameters
    ----------
    d
        A ``biber-syntax`` diagnostic whose ``file`` is the real ``.bib``.
    source_lines
        The lines of that file.

    Returns
    -------
    str | None
        For example ``read main.bib from line 31; line 32 starts with
        'useprefix=' ...``; None if there is nothing useful to add.
    """
    if d.code != "biber-syntax" or not d.line or not d.file:
        return None
    m = _RUNAWAY.search(d.message)
    start = int(m.group(1)) if m else d.line
    name = d.file.rsplit("/", 1)[-1]
    text = source_lines[d.line - 1] if 0 < d.line <= len(source_lines) else ""
    head = f"read {name} from line {start} to {d.line}"
    if f := _FIELD_START.match(text):
        return (
            f"{head}: line {d.line} starts with '{f.group(1)}=', which looks like a "
            f"new field to the parser, so it thinks the value opened at line {start} "
            "was never closed. Harmless if intended; to silence it, move that text "
            "so the line does not start with key="
        )
    if text.lstrip().startswith("@"):
        return (
            f"{head}: line {d.line} starts with '@', which looks like a new entry, "
            f"so the value opened at line {start} may be missing its closing brace"
        )
    if m:
        return f"{head}: check that the braces or quotes opened at line {start} are closed"
    return None


@dataclass
class BibResult:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    info: BibLogInfo = field(default_factory=BibLogInfo)


def _biber_diag(level: str, msg: str, raw: str) -> Diagnostic:
    kind: Kind = "error" if level in {"ERROR", "FATAL"} else "warning"
    d = Diagnostic(kind, msg.strip(), source="biber", raw=[raw[:500]])
    head = msg[:MAX_MATCH]
    if m := _BIBER_SYNTAX.search(head):
        d.file = clean_path(_BIBER_TMP.sub("", m.group(1)).rsplit("/", 1)[-1])
        d.line = to_int(m.group(2))
        d.code = "biber-syntax"
        d.kind = "warning" if m.group(3) == "warning" else "error"
        d.message = f"{m.group(3)}: {m.group(4)}".strip()
    else:
        if (m := _AT_LINE.search(head)) or (m := _ANY_LINE.search(head)):
            d.line = to_int(m.group(1))
        if m := _PAREN_FILE.search(head) or _FILE_Q.search(head):
            d.file = clean_path(m.group(1))
    if m := _ENTRY.search(head):
        d.entry = m.group(1)
    low = head.lower()
    if d.code is None:
        if "cannot find control file" in low or ("bcf" in low and "cannot find" in low):
            d.code = "biber-missing-control-file"
        elif "data source" in low and "not found" in low:
            d.code = "biber-source-not-found"
        elif "cannot find" in low:
            d.code = "biber-file-not-found"
        elif "could not open" in low:
            d.code = "biber-cannot-open"
        elif "datamodel" in low:
            d.code = "biber-datamodel"
        elif "didn't find a database entry" in low:
            d.code = "biber-missing-entry"
        elif "duplicate entry" in low:
            d.code = "biber-duplicate-entry"
        elif level == "FATAL":
            d.code = "biber-fatal"
        else:
            d.code = "biber-error" if kind == "error" else "biber-warning"
    d.package = "biber"
    return d


def _bibtex_error(
    msg: str, line: int | None, file: str | None, raw: list[str]
) -> Diagnostic:
    d = Diagnostic("error", msg.strip(), file, line, source="bibtex", raw=raw)
    d.package = "bibtex"
    if re.search(r"couldn't open (?:database|style) file (\S+)", msg):
        d.code = "bibtex-missing-style" if "style" in msg else "bibtex-missing-database"
        d.entry = None
    elif "I found no" in msg:
        d.code = "bibtex-missing-command"
    elif "another \\bibstyle" in msg:
        d.code = "bibtex-repeated-bibstyle"
    elif "I was expecting" in msg:
        d.code = "bibtex-syntax"
    elif "Repeated entry" in msg:
        d.code = "bibtex-repeated-entry"
    else:
        d.code = "bibtex-error"
    return d


def parse_bib(text: str, program: str | None = None) -> BibResult:
    """Parse a `.blg`. The format is detected; `program` is only a fallback label."""
    lines = split_lines(text)
    info = BibLogInfo()
    out: list[Diagnostic] = []
    biber_seen = False
    bibtex_seen = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i][:MAX_MATCH]
        i += 1
        if not line.strip():
            continue
        if m := _BIBER.match(line):
            biber_seen = True
            level, msg = m.group(1), m.group(2)
            if level == "INFO":
                if v := _BIBER_VERSION.search(msg):
                    info.version = v.group(1)
                if c := _BIBER_COUNT.match(msg):
                    if c.group(1) == "ERRORS":
                        info.error_count = int(c.group(2))
                    else:
                        info.warning_count = int(c.group(2))
                continue
            if level in {"DEBUG", "TRACE"}:
                continue
            out.append(_biber_diag(level, msg, line))
            continue
        if line.startswith("This is BibTeX"):
            bibtex_seen = True
            m2 = re.search(r"Version (\S+)", line)
            info.version = m2.group(1) if m2 else None
            continue
        if m := _BT_AUX.match(line):
            bibtex_seen = True
            info.aux = m.group(1)
            continue
        if m := _BT_STYLE.match(line):
            info.style = m.group(1)
            continue
        if m := _BT_DB.match(line):
            info.databases.append(m.group(1))
            continue
        if m := _BT_USED.match(line):
            info.entries_used = int(m.group(1))
            continue
        if (m := _BT_COUNT.match(line)) or (m := _BT_COUNT2.match(line)):
            if m.group(2) == "error":
                info.error_count = int(m.group(1))
            else:
                info.warning_count = int(m.group(1))
            continue
        if m := _BT_WARN.match(line):
            bibtex_seen = True
            msg = m.group(1).strip()
            raw = [line]
            file = None
            ln = None
            if i < n and (lm := _BT_EXPECT_LINE.match(lines[i][:MAX_MATCH])):
                ln, file = int(lm.group(1)), clean_path(lm.group(2))
                raw.append(lines[i][:500])
                i += 1
            d = Diagnostic("warning", msg, file, ln, source="bibtex", raw=raw)
            d.package = "bibtex"
            if "didn't find a database entry" in msg:
                d.code = "bibtex-missing-entry"
            elif msg.startswith("empty "):
                d.code = "bibtex-empty-field"
            elif "undefined" in msg:
                d.code = "bibtex-undefined-string"
            else:
                d.code = "bibtex-warning"
            if (e := _BT_ENTRY_FOR.search(msg)) or (e := _BT_ENTRY_IN.search(msg)):
                d.entry = e.group(1)
            out.append(d)
            continue
        if line.startswith((" : ", "I'm skipping whatever")):
            continue
        # errors: `message---line N of file F`, `message---while reading file F`,
        # or the message on one line and `---line N of file F` on the next
        err: Diagnostic | None = None
        if m := _BT_AT.match(line):
            err = _bibtex_error(
                m.group(1), int(m.group(2)), clean_path(m.group(3)), [line]
            )
        elif m := _BT_WHILE.match(line):
            err = _bibtex_error(m.group(1), None, clean_path(m.group(2)), [line])
        elif _BT_ERR_LEAD.match(line):
            ln, file = None, None
            raw = [line]
            if i < n and (lm := _BT_LOC_ONLY.match(lines[i][:MAX_MATCH])):
                ln, file = int(lm.group(1)), clean_path(lm.group(2))
                raw.append(lines[i][:500])
                i += 1
            err = _bibtex_error(line, ln, file, raw)
        if err is not None:
            bibtex_seen = True
            while i < n and _BT_CTX.match(lines[i]) and len(err.context) < 4:
                err.context.append(lines[i][3:].rstrip())
                err.raw.append(lines[i][:500])
                i += 1
            out.append(err)
    source = (
        "biber" if biber_seen else "bibtex" if bibtex_seen else (program or "biber")
    )
    info.tool = source
    return BibResult(out, info)


def parse_blg(text: str, program: str = "biber") -> list[Diagnostic]:
    """Warnings/errors from a biber or bibtex .blg log."""
    return parse_bib(text, program).diagnostics
