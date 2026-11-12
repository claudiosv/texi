"""Parser for latexmk's own stdout/stderr and the run summary it implies.

latexmk prints its decisions (`Latexmk: ...`, `Rule '...'`, `Run number N of rule
'...'`) around the raw output of every engine / bibtex / makeindex run. This
module reads the latexmk lines into a `LatexmkInfo` (which rules ran, how many
passes, success or failure, missing files) and hands the embedded tool output to
the per-tool parsers.
"""

import re
from dataclasses import dataclass, field

from texi.logparse_bib import parse_bib
from texi.logparse_core import (
    MAX_MATCH,
    Diagnostic,
    LatexmkInfo,
    RuleRun,
    TexLogInfo,
    split_lines,
)
from texi.logparse_index import parse_index
from texi.logparse_tex import parse_tex

# latexmk documents these exit codes; 255 is what current versions return when
# the rc file fails to load.
EXIT_CODES: dict[int, str] = {
    0: "success",
    10: "bad command-line arguments",
    11: "a file specified on the command line does not exist",
    12: "failure in some part of making files",
    13: "error in initialization (rc) file",
    20: "probable bug or other unusual condition",
    255: "latexmk stopped before building (initialization problem)",
}

_RUN = re.compile(r"Run number (\d+) of rule '([^']+)'")
_RUNNING = re.compile(r"^Running '(.*)'\s*$")
_APPLY = re.compile(r"Latexmk: applying rule '([^']+)'")
_VERSION = re.compile(r"Latexmk: This is Latexmk, .*Version (\S+?)\.?\s*$")
_UPTODATE = re.compile(r"Latexmk: All targets \((.*)\) are up-to-date")
_NOTHING = re.compile(r"Latexmk: Nothing to do for '([^']+)'")
_MISSING = re.compile(r"Latexmk: Missing input file:? '([^']+)'")
_NOFILE = re.compile(r"Latexmk: Could not find file '([^']+)'")
_MAXRUNS = re.compile(r"Latexmk: Maximum runs of (\S+) reached")
_FAILURE_IN = re.compile(r"Latexmk: Failure in processing file '([^']+)'")
_DID_NOT = re.compile(r"Latexmk: Did not finish processing file '?([^'\n]*)'?")
_FAIL_MAKE = re.compile(r"^Failure to make '([^']+)'")
_RC_ERR = re.compile(r"Latexmk: Initialization file '([^']+)' gave an error")
_RC_STOP = re.compile(r"Latexmk: Stopping because of problem with rc file")
_RC_IN = re.compile(r"Latexmk: Error in rc file")
_BAD_OPT = re.compile(r"Latexmk: (?:Bad options specified|(\S+) unknown option)")
_INDEX_GEN = re.compile(r"Latexmk: Index file '([^']+)' was (?:generated|written)")
_CHANGED_HDR = re.compile(r"^Changed files or newly in use/created:")
_CMD_RC = re.compile(r"(?:Command for |^\s*)'?([\w.+-]+)'? gave return code (\d+)")
_SUMMARY_HDR = re.compile(r"Latexmk: Summary of warnings")
_REFS_HDR = re.compile(r"Latexmk: ====.*(?:refs|citations)", re.IGNORECASE)
_ERRSUM_HDR = re.compile(r"^Collected error summary")
_ERRORS_SO = re.compile(r"Latexmk: Errors, so I did not complete making targets")
_FORCE_HINT = re.compile(r"Latexmk: (?:Sometimes|If appropriate), the -f option")
_PREV_ERR = re.compile(r"^\s*([\w.+-]+): gave an error in previous invocation")
_TOOLS_TEX = {
    "pdflatex", "xelatex", "lualatex", "latex", "platex", "uplatex", "pdftex",
    "xetex", "luatex", "tex", "lualatex-dev", "pdflatex-dev", "etex", "context",
}  # fmt: skip
_TOOLS_BIB = {"bibtex", "biber", "bibtex8", "bibtexu", "pbibtex", "upbibtex"}
_TOOLS_IDX = {"makeindex", "xindy", "texindy", "makeglossaries", "makeglossaries-lite", "splitindex", "upmendex", "mendex"}  # fmt: skip


@dataclass
class LatexmkResult:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    info: LatexmkInfo = field(default_factory=LatexmkInfo)
    tex_runs: list[TexLogInfo] = field(default_factory=list)  # one per engine pass


def _tool_of(command: str | None, rule: str | None) -> str | None:
    """Program name of a `Running '...'` command, skipping `env -u X VAR=y` prefixes."""
    known = _TOOLS_TEX | _TOOLS_BIB | _TOOLS_IDX
    words = (command or "").split()
    skip = False
    for w in words[:12]:
        if skip:
            skip = False
            continue
        if w == "env" or "=" in w:
            continue
        if w in {"-u", "-C", "-S"}:
            skip = True
            continue
        if w.startswith("-"):
            continue
        name = w.strip("'\"").rsplit("/", 1)[-1].lower()
        if name in known:
            return name
        break
    word = (rule or "").strip().split(" ", 1)[0].lower()
    return word or None


def _indented(lines: list[str], i: int) -> tuple[list[str], int]:
    """Consume the indented lines that follow a latexmk section header."""
    block: list[str] = []
    while (
        i < len(lines)
        and lines[i].strip()
        and lines[i][0] in " \t"
        and len(block) < 200
    ):
        block.append(lines[i].strip()[:MAX_MATCH])
        i += 1
    return block, i


def _lm(
    kind: str, msg: str, code: str, raw: str | None = None, file: str | None = None
) -> Diagnostic:
    return Diagnostic(
        "error" if kind == "error" else "warning",
        msg,
        file,
        source="latexmk",
        code=code,
        raw=[raw[:500]] if raw else [],
    )


def parse_latexmk(
    text: str, *, exit_code: int | None = None, engine_output: bool = True
) -> LatexmkResult:
    """Parse latexmk's stdout/stderr.

    `exit_code` (latexmk's process exit status) sharpens the verdict when known.
    With `engine_output` the TeX / bibtex / makeindex output printed between
    latexmk's own lines is parsed too and its diagnostics are included.
    """
    lines = split_lines(text)
    n = len(lines)
    info = LatexmkInfo(exit_code=exit_code)
    diags: list[Diagnostic] = []
    res = LatexmkResult(diags, info)
    failed = False
    uptodate = False
    seg_tool: str | None = None
    seg_start: int | None = None

    def close_segment(end: int) -> None:
        nonlocal seg_start, seg_tool
        if seg_start is None or not engine_output:
            seg_start = None
            return
        chunk = "\n".join(lines[seg_start:end])
        tool = seg_tool
        seg_start = None
        if tool in _TOOLS_TEX:
            r = parse_tex(chunk)
            diags.extend(r.diagnostics)
            if r.info:
                res.tex_runs.append(r.info)
        elif tool in _TOOLS_BIB:
            diags.extend(parse_bib(chunk, tool).diagnostics)
        elif tool in _TOOLS_IDX:
            diags.extend(parse_index(chunk, tool).diagnostics)

    i = 0
    while i < n:
        line = lines[i][:MAX_MATCH]
        i += 1
        if seg_start is not None and (
            line.startswith(("Latexmk:", "Rule '")) or _RUN.search(line)
        ):
            close_segment(i - 1)
        if not line.strip():
            continue

        if m := _RUN.search(line):
            info.runs.append(RuleRun(m.group(2), int(m.group(1))))
            continue
        if m := _RUNNING.match(line):
            if info.runs:
                info.runs[-1].command = m.group(1)
            seg_tool = _tool_of(m.group(1), info.runs[-1].rule if info.runs else None)
            # the tool's own output starts after the closing `------------`
            j = i
            if j < n and lines[j].startswith("-----"):
                j += 1
            seg_start = j
            i = j
            continue
        if m := _VERSION.search(line):
            info.version = m.group(1)
            continue
        if m := _APPLY.search(line):
            info.applied_rules.append(m.group(1))
            continue
        if m := _UPTODATE.search(line):
            info.targets = [t.strip() for t in m.group(1).split(",") if t.strip()]
            uptodate = True
            continue
        if _NOTHING.search(line):
            uptodate = True
            continue
        if m := _MISSING.search(line):
            if m.group(1) not in info.missing_files:
                info.missing_files.append(m.group(1))
            continue
        if m := _NOFILE.search(line):
            failed = True
            if m.group(1) not in info.missing_files:
                info.missing_files.append(m.group(1))
            diags.append(
                _lm(
                    "error",
                    f"Source file not found: {m.group(1)}",
                    "latexmk-no-source",
                    line,
                    m.group(1),
                )
            )
            continue
        if m := _MAXRUNS.search(line):
            info.max_runs_reached = True
            failed = True
            info.failed_rules.append(m.group(1))
            diags.append(
                _lm(
                    "error",
                    f"{m.group(1)} reached the maximum number of runs without stable files "
                    "(a label or reference keeps changing)",
                    "latexmk-max-runs",
                    line,
                )
            )
            continue
        if m := _FAILURE_IN.search(line):
            failed = True
            block, i = _indented(lines, i)
            while i < n and lines[i].startswith("-") and not block:
                i += 1
            if i < n and lines[i].strip().startswith("'") and not block:
                block.append(lines[i].strip())
                i += 1
            detail = " ".join(block)
            text_msg = f"Failure in processing {m.group(1)}" + (
                f": {detail}" if detail else ""
            )
            if info.max_runs_reached and not detail:
                text_msg += " (too many passes)"
            diags.append(_lm("error", text_msg, "latexmk-failure", line, m.group(1)))
            continue
        if m := _DID_NOT.search(line):
            failed = True
            diags.append(
                _lm(
                    "error",
                    f"Did not finish processing {m.group(1).strip() or 'the file'}",
                    "latexmk-failure",
                    line,
                )
            )
            continue
        if m := _FAIL_MAKE.match(line):
            failed = True
            diags.append(
                _lm("error", f"Failed to make {m.group(1)}", "latexmk-failure", line)
            )
            continue
        if _RC_ERR.search(line) or _RC_STOP.search(line) or _RC_IN.search(line):
            failed = True
            info.rc_error = True
            msg = line.split("Latexmk:", 1)[-1].strip()
            block, i = _indented(lines, i)
            if block:
                msg += " " + " ".join(block)
            if not any(d.code == "latexmk-rc-error" for d in diags):
                diags.append(_lm("error", f"latexmk: {msg}", "latexmk-rc-error", line))
            continue
        if m := _BAD_OPT.search(line):
            failed = True
            info.bad_options = True
            if m.group(1):
                diags.append(
                    _lm(
                        "error",
                        f"latexmk: unknown option {m.group(1)}",
                        "latexmk-bad-option",
                        line,
                    )
                )
            continue
        if m := _INDEX_GEN.search(line):
            info.generated.append(m.group(1))
            continue
        if _CHANGED_HDR.match(line):
            block, i = _indented(lines, i)
            info.missing_files = [f for f in info.missing_files if f not in block]
            continue
        if _ERRORS_SO.search(line):
            failed = True
            continue
        if _FORCE_HINT.search(line):
            info.forced_hint = True
            continue
        if _SUMMARY_HDR.search(line):
            block, i = _indented(lines, i)
            info.summary.extend(block)
            continue
        if _REFS_HDR.search(line):
            block, i = _indented(lines, i)
            info.undefined.extend(block)
            continue
        if _ERRSUM_HDR.match(line):
            failed = True
            block, i = _indented(lines, i)
            for b in block:
                if pm := _PREV_ERR.match(b):
                    info.previous_error = True
                    info.failed_rules.append(pm.group(1))
                    diags.append(
                        _lm(
                            "error",
                            f"{pm.group(1)} failed in a previous run and was not retried (rerun with --full)",
                            "latexmk-previous-error",
                            b,
                        )
                    )
                elif cm := _CMD_RC.search(b):
                    rule, rc = cm.group(1), int(cm.group(2))
                    if rule not in info.failed_rules:
                        info.failed_rules.append(rule)
                    for r in reversed(info.runs):
                        if r.rule == rule or _tool_of(r.command, r.rule) == rule:
                            r.exit_code = rc
                            break
                    diags.append(
                        _lm(
                            "error",
                            f"{rule} failed (return code {rc}); see its log",
                            "latexmk-rule-failed",
                            b,
                        )
                    )
                elif b.endswith("failed to create output file"):
                    rule = b.split(":", 1)[0]
                    if rule not in info.failed_rules:
                        info.failed_rules.append(rule)
                    diags.append(
                        _lm(
                            "error",
                            f"{rule} failed to create the output file",
                            "latexmk-no-output",
                            b,
                        )
                    )
            continue
        if _PREV_ERR.match(line):
            info.previous_error = True
            failed = True
            continue
    close_segment(n)

    # the missing files only matter as errors when the build did fail
    if failed or (exit_code not in {None, 0}):
        for f in info.missing_files:
            if not any(d.file == f for d in diags):
                diags.append(
                    _lm(
                        "error",
                        f"Missing input file: {f}",
                        "latexmk-missing-file",
                        None,
                        f,
                    )
                )
    if exit_code is not None:
        info.exit_meaning = EXIT_CODES.get(exit_code, f"exit code {exit_code}")
        if exit_code != 0:
            failed = True
    if failed:
        info.status = "failed"
    elif uptodate and not info.runs:
        info.status = "up-to-date"
    elif info.runs or exit_code == 0:
        info.status = "success"
    if info.status == "failed" and not any(
        d.kind == "error" and d.source == "latexmk" for d in diags
    ):
        code = f" (exit code {exit_code}: {info.exit_meaning})" if exit_code else ""
        diags.append(
            _lm("error", f"latexmk reported a failure{code}", "latexmk-failure")
        )
    return res


def run_sequence(info: LatexmkInfo) -> str:
    """Compact human summary such as `pdflatex x2, bibtex x1, pdflatex x1`."""
    out: list[str] = []
    prev: str | None = None
    count = 0
    for r in info.runs:
        if r.rule == prev:
            count += 1
            continue
        if prev is not None:
            out.append(f"{prev} x{count}")
        prev, count = r.rule, 1
    if prev is not None:
        out.append(f"{prev} x{count}")
    return ", ".join(out)


__all__ = ["EXIT_CODES", "LatexmkResult", "parse_latexmk", "run_sequence"]
