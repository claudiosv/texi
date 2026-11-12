"""Parser for makeindex / xindy / makeglossaries transcripts (`.ilg`, `.glg`)."""

import re
from dataclasses import dataclass, field

from texi.logparse_core import MAX_MATCH, Diagnostic, clean_path, split_lines, to_int

_MI_IN_ERR = re.compile(
    r"^!! (Input|Output) index error \(file = ([^,]*), line = (\d+)\):\s*$"
)
_MI_WARN = re.compile(
    r"^## Warning \(input = ([^,]*), line = (\d+); output = ([^,]*), line = (\d+)\):\s*$"
)
_MI_DETAIL = re.compile(r"^\s+-- (.*)$")
_MI_SCAN = re.compile(r"^Scanning input file (\S+?)\.{2,}(.*)$")
_MI_ACCEPT = re.compile(r"\((\d+) entries accepted, (\d+) rejected\)")
_MI_SORT = re.compile(r"^Sorting entries\.{2,}(.*)$")
_MI_GEN = re.compile(r"^Generating output file (\S+?)\.{2,}(.*)$")
_MI_WRITTEN = re.compile(r"\((\d+) lines written, (\d+) warnings?\)")
_MI_OUT = re.compile(r"^Output written in (\S+?)\.?\s*$")
_MI_NOTHING = re.compile(r"^Nothing written in (\S+?)\.?\s*$")
_MI_TRANSCRIPT = re.compile(r"^Transcript written in (\S+?)\.?\s*$")
_MI_NOTFOUND = re.compile(r"^Input index file (\S+) not found\.?\s*$")
_MI_CANT = re.compile(r"^(Can't (?:create|open|find)[^\n]*)$")
_MI_BANNER = re.compile(r"^This is makeindex, version (\S+)")
_ACCEPTED = re.compile(r"^Accepted (\d+) entries")
_XINDY_MSG = re.compile(r"^(WARNING|ERROR|FATAL|Warning|Error)[:!] ?(.*)$")
_XINDY_BANNER = re.compile(
    r"^(?:This is )?(?:xindy|texindy|Opening logfile)", re.IGNORECASE
)
_PHASE = re.compile(
    r"^(Reading|Sorting|Making pages|Generating output|Writing|Processing|Loading)\b"
)
_GLOSS = re.compile(r"^(makeglossaries(?:-lite)?|bib2gls)\b[: ]")


@dataclass
class IndexLogInfo:
    tool: str | None = None  # makeindex | xindy | makeglossaries
    version: str | None = None
    inputs: list[str] = field(default_factory=list)
    output: str | None = None
    accepted: int | None = None
    rejected: int | None = None
    lines_written: int | None = None
    warning_count: int | None = None
    sorted: bool = False
    nothing_written: bool = False
    phases: list[str] = field(default_factory=list)  # Sorting, Making pages, ...


@dataclass
class IndexResult:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    info: IndexLogInfo = field(default_factory=IndexLogInfo)


def parse_index(text: str, program: str | None = None) -> IndexResult:
    """Parse an `.ilg` / `.glg`. Tool is detected from the content; `program` is a fallback label."""
    lines = split_lines(text)
    info = IndexLogInfo()
    out: list[Diagnostic] = []
    tool = program
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i][:MAX_MATCH].rstrip()
        i += 1
        if not line:
            continue
        if m := _MI_BANNER.match(line):
            tool = "makeindex"
            info.version = m.group(1)
            continue
        if m := _MI_SCAN.match(line):
            tool = tool or "makeindex"
            info.inputs.append(m.group(1))
            if a := _MI_ACCEPT.search(m.group(2)):
                info.accepted, info.rejected = int(a.group(1)), int(a.group(2))
            continue
        if _MI_SORT.match(line):
            info.sorted = True
            info.phases.append("Sorting")
            continue
        if m := _MI_GEN.match(line):
            info.phases.append("Generating")
            if w := _MI_WRITTEN.search(m.group(2)):
                info.lines_written, info.warning_count = (
                    int(w.group(1)),
                    int(w.group(2)),
                )
            continue
        if a := _MI_ACCEPT.search(line):  # `done (4 entries accepted, 3 rejected).`
            info.accepted, info.rejected = int(a.group(1)), int(a.group(2))
            continue
        if w := _MI_WRITTEN.search(line):
            info.lines_written, info.warning_count = int(w.group(1)), int(w.group(2))
            continue
        if m := _MI_OUT.match(line):
            info.output = m.group(1)
            continue
        if m := _MI_NOTHING.match(line):
            info.nothing_written = True
            info.output = m.group(1)
            out.append(
                Diagnostic(
                    "warning",
                    f"Nothing written in {m.group(1)} (no index entries)",
                    source=tool or "makeindex",
                    code="index-empty",
                    raw=[line],
                )
            )
            continue
        if _MI_TRANSCRIPT.match(line):
            continue
        if m := _MI_IN_ERR.match(line):
            detail, i = _detail(lines, i)
            out.append(
                Diagnostic(
                    "error",
                    detail or f"{m.group(1)} index error",
                    clean_path(m.group(2).strip()),
                    to_int(m.group(3)),
                    source="makeindex",
                    code=f"makeindex-{m.group(1).lower()}-error",
                    raw=[line],
                )
            )
            continue
        if m := _MI_WARN.match(line):
            detail, i = _detail(lines, i)
            out.append(
                Diagnostic(
                    "warning",
                    detail or "makeindex warning",
                    clean_path(m.group(1).strip()),
                    to_int(m.group(2)),
                    source="makeindex",
                    code="makeindex-warning",
                    raw=[line],
                )
            )
            continue
        if m := _MI_NOTFOUND.match(line):
            out.append(
                Diagnostic(
                    "error",
                    f"Input index file {m.group(1)} not found",
                    source="makeindex",
                    code="index-input-missing",
                    raw=[line],
                )
            )
            continue
        if m := _MI_CANT.match(line):
            out.append(
                Diagnostic(
                    "error",
                    m.group(1),
                    source="makeindex",
                    code="makeindex-io",
                    raw=[line],
                )
            )
            continue
        if am := _ACCEPTED.match(line):
            info.accepted = int(am.group(1))
            continue
        if m := _GLOSS.match(line):
            tool = "makeglossaries"
            continue
        if m := _XINDY_MSG.match(line):
            tool = tool if tool and tool != "makeindex" else "xindy"
            level = m.group(1).upper()
            out.append(
                Diagnostic(
                    "warning" if level.startswith("WARN") else "error",
                    m.group(2).strip() or level.title(),
                    source=tool,
                    code=f"{tool}-{level.lower()}",
                    raw=[line],
                )
            )
            continue
        if _XINDY_BANNER.match(line):
            tool = tool or "xindy"
            continue
        if pm := _PHASE.match(line):
            info.phases.append(pm.group(1))
            if pm.group(1) == "Sorting":
                info.sorted = True
    info.tool = tool or program or "makeindex"
    return IndexResult(out, info)


def _detail(lines: list[str], i: int) -> tuple[str | None, int]:
    if i < len(lines) and (m := _MI_DETAIL.match(lines[i][:MAX_MATCH])):
        return m.group(1).strip(), i + 1
    return None, i
