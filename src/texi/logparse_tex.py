"""Parser for TeX engine logs (pdfTeX, XeTeX, LuaTeX/LuaHBTeX) and engine stdout.

Works best on logs written with a huge `max_print_line` (texi sets it) and
`-file-line-error`, but also copes with classic 79-column wrapped logs, CRLF
line endings, truncated files and garbage. Never raises on text input and all
regexes are linear, so a 1 MB single line is fine.
"""

import re

from texi.logparse_core import (
    MAX_MATCH,
    Diagnostic,
    Kind,
    ParseResult,
    TexLogInfo,
    clean_path,
    split_lines,
    to_int,
)

_FILE_EXTS = (
    ".tex",
    ".sty",
    ".cls",
    ".bbx",
    ".cbx",
    ".def",
    ".cfg",
    ".ltx",
    ".fd",
    ".clo",
    ".lbx",
    ".bib",
    ".aux",
    ".toc",
    ".lof",
    ".lot",
    ".out",
    ".bbl",
    ".ldf",
    ".lua",
    ".code",
)

# `./main.tex:12: message` (-file-line-error)
_FLE = re.compile(
    r"^((?:\./|\.\./|/|~|[A-Za-z]:[\\/]|\w)[^\s:]*?\.[A-Za-z][A-Za-z0-9]*):(\d+):(?: (.*))?$"
)
_BANG = re.compile(r"^! ?(.*)$")
_CONTEXT = re.compile(r"^l\.(\d+)(?:\s(.*))?$")
_STACKLINE = re.compile(r"^(?:<[^>]{0,40}>|\\\S* .*->|\s*->)")
_WARN = re.compile(
    r"^(LaTeX(?: Font)?|(?:Package|Class|Module) \S+) (Warning|Info|Error)(?: \(([^)]*)\))?: ?(.*)$"
)
_ENGINE_MSG = re.compile(
    r"^(pdfTeX|LuaTeX|LuaHBTeX|XeTeX|pdftex|luatex) (warning|error)(?: \(([^)]*)\))?: ?(.*)$",
    re.IGNORECASE,
)
_LUA_BACKEND = re.compile(r"^(warning|error)\s+\(([^)]*)\):\s*(.*)$")
_LUA_ERR = re.compile(r"^(\[[^\]]{1,80}\]|\S+\.lua):(\d+): (.*)$")
_LUA_LOWER = re.compile(
    r"^(?:lua|Lua|LuaTeX|LuaHBTeX) (?:callback )?(error|warning):? ?(.*)$"
)
_LUAOTFLOAD = re.compile(r"^luaotfload \| ([\w-]+)\s*: (.*)$")
_DVIPDFMX = re.compile(
    r"^(xdvipdfmx|dvipdfmx|dvipdfm|xdv2pdf|dvips):\s*(fatal|warning|error|info)?:?\s*(.*)$",
    re.IGNORECASE,
)
_STARSTAR = re.compile(r"^\*\* (WARNING|ERROR) \*\*:? ?(.*)$")
_DRIVER = re.compile(r"^Error (\d+) \(driver return code\) generating output;?")
_KPSE = re.compile(r"^kpathsea: (.*)$")
_BOX = re.compile(r"^((?:Overfull|Underfull) \\[hv]box .*)$")
_MISSING_CHAR = re.compile(r"^Missing character: (.*)$")
_RUNAWAY = re.compile(r"^Runaway (argument|definition|preamble|text)\?\s*$")
_CONT = re.compile(r"^\(([A-Za-z][\w/+-]*)\)\s{2,}(.*)$")
_CONT_EMPTY = re.compile(r"^\(([A-Za-z][\w/+-]*)\)\s*$")
_PKG_ERR = re.compile(r"^(Package|Class|Module) (\S+) Error:")
_ON_LINE = re.compile(r"\s*on input line (\d+)")
_ON_PAGE = re.compile(r"on page (\d+)")
_BOX_LINES = re.compile(r"at lines? (\d+)(?:--(\d+))?")
_OUTPUT = re.compile(
    r"^Output written on (.+?) \((\d+) pages?(?:, (\d+) bytes)?\)\.?\s*$"
)
_BANNER = re.compile(
    r"^This is ([A-Za-z0-9-]*TeX), Version (\S+)(?: \(([^)]*)\))?(.*)$"
)
_FORMAT = re.compile(r"format=(\S+)")
_OPENOUT = re.compile(r"^\\openout\d+\s*=\s*(.*)$")
_RUNSYSTEM = re.compile(r"^runsystem\((.*)\)\.\.\.(\w+)(?: \((\w+)\))?\.?\s*$")
_LATEX_DATE = re.compile(r"^LaTeX2e <([^>]+)>")
_DOCCLASS = re.compile(r"^Document Class: (\S+)")
_PAGE_MARK = re.compile(r"\[(\d+)(?=[\]{<\s]|$)")
_OPEN = re.compile(r'\((?:"[^"\n]*"|[^\s()]*)|\)')
_PLAIN_FILE = re.compile(r"^[\w.+@-]+(?:/[\w.+@-]+)*\.[A-Za-z][A-Za-z0-9]{0,9}$")
_BOUNDARY_START = re.compile(
    r"^(?:Overfull|Underfull) \\[hv]box|^Missing character:|^Runaway |^Output written|"
    r"^Here is how much|^No pages of output|^Transcript written|^\\openout|"
    r"^\[\d+[\]{ ]|^\(\.{0,2}/|^\)|^LaTeX2e <|^runsystem\(|^kpathsea:|^xdvipdfmx|^Error \d+ \("
)

# (pattern on the message, slug)
_CODES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), c)
    for p, c in [
        (r"Undefined control sequence", "undefined-control-sequence"),
        (r"Reference `.*' on page \d+ undefined", "undefined-reference"),
        (r"Citation `.*' on page \d+ undefined", "undefined-citation"),
        (r"There were undefined references", "undefined-references"),
        (r"There were undefined citations", "undefined-citations"),
        (r"Label\(s\) may have changed|Rerun to get|\bRerun\b", "rerun"),
        (r"Label `.*' multiply defined", "duplicate-label"),
        (r"Font shape `.*' undefined", "font-shape-undefined"),
        (r"Font .* not loadable", "font-not-loadable"),
        (r"Missing character", "missing-character"),
        (r"^Overfull \\hbox", "overfull-hbox"),
        (r"^Underfull \\hbox", "underfull-hbox"),
        (r"^Overfull \\vbox", "overfull-vbox"),
        (r"^Underfull \\vbox", "underfull-vbox"),
        (r"Unicode character", "unicode-char"),
        (r"File ended while scanning", "file-ended"),
        (r"Paragraph ended before", "paragraph-ended"),
        (r"Emergency stop", "emergency-stop"),
        (r"==> Fatal error", "fatal-error"),
        (r"Missing \$ inserted", "missing-dollar"),
        (
            r"Missing [{}] inserted|Missing number|Missing \\(?:begin|end)|Missing .* inserted",
            "missing-token",
        ),
        (r"Double (?:super|sub)script", "double-script"),
        (r"Too many }'s|Extra }|Extra \\else|Extra \\fi", "unbalanced"),
        (
            r"Misplaced alignment tab|Extra alignment tab|Misplaced \\(?:noalign|omit)",
            "alignment",
        ),
        (r"Environment .* undefined", "undefined-environment"),
        (r"\\begin\{.*\} on input line .* ended by", "environment-mismatch"),
        (r"Option clash for package", "option-clash"),
        (r"Unknown option", "unknown-option"),
        (r"I can't find file|File `.*' not found|not found\.$", "file-not-found"),
        (r"TeX capacity exceeded", "capacity-exceeded"),
        (
            r"Use of .* doesn't match its definition|Illegal parameter number",
            "macro-mismatch",
        ),
        (r"Command .* already defined", "already-defined"),
        (r"destination with the same identifier", "duplicate-destination"),
        (r"cannot open font map file", "map-file"),
        (r"shell escape|write18|runsystem", "shell-escape"),
        (r"Token not allowed in a PDF string", "pdf-string"),
    ]
]


def _code_for(message: str) -> str | None:
    head = message[:MAX_MATCH]
    for pat, code in _CODES:
        if pat.search(head):
            return code
    return None


def _package_of(who: str) -> str | None:
    parts = who.split(" ", 1)
    if parts[0] in {"Package", "Class", "Module"} and len(parts) == 2:
        return parts[1]
    if who in {"pdfTeX", "pdftex"}:
        return "pdftex"
    if who.lower().startswith("lua"):
        return "luatex"
    return None


def _engine_name(banner_word: str) -> str:
    return banner_word.lower().replace("-", "")


class _FileStack:
    """Best-effort tracking of which file TeX is reading, from `(path` / `)` in the log.

    Only file-looking `(path` opens are pushed. A `)` closes a file when it sits
    where TeX prints one (line start, after `)`, `]`, `>`, a space, right after a
    file name, or at the very end of a line with no text paren open); parens inside
    message text are ignored.
    """

    def __init__(self, seen: list[str] | None = None) -> None:
        self.stack: list[str] = []
        self.seen = seen if seen is not None else []

    @staticmethod
    def _is_file(tok: str) -> bool:
        if not tok:
            return False
        if tok.startswith(("/", "./", "../", "~/")):
            return True
        if len(tok) > 2 and tok[1] == ":" and tok[2] in "\\/" and tok[0].isalpha():
            return True
        return bool(_PLAIN_FILE.match(tok))

    def feed(self, line: str) -> None:
        opened_end = -1
        local = 0  # unbalanced `(` of ordinary text on this line
        n = len(line)
        for m in _OPEN.finditer(line):
            g = m.group(0)
            if g != ")":
                tok = g[1:]
                if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
                    tok = tok[1:-1]
                if self._is_file(tok):
                    self.stack.append(tok)
                    if len(self.seen) < 2000:
                        self.seen.append(tok)
                    opened_end = m.end()
                else:
                    local += 1
                continue
            if m.start() == opened_end:
                if self.stack:
                    self.stack.pop()
                continue
            if local > 0:
                local -= 1
                continue
            if not self.stack:
                continue
            before = line[m.start() - 1] if m.start() else "\n"
            at_end = not line[m.end() : n].strip()
            if before in "\n)]> " or at_end:
                self.stack.pop()

    @property
    def current(self) -> str | None:
        return self.stack[-1] if self.stack else None

    @property
    def current_tex(self) -> str | None:
        """Innermost .tex file (what the user wrote), else the innermost file."""
        return next(
            (f for f in reversed(self.stack) if f.endswith(".tex")), self.current
        )


def _is_boundary(line: str) -> bool:
    """Does this line start something that is not part of the previous message?"""
    if not line:
        return False
    head = line[:MAX_MATCH]
    c = head[0]
    if c in " \t":
        return False
    if c == "!":
        return True
    if _BOUNDARY_START.match(head):
        return True
    if _WARN.match(head) or _ENGINE_MSG.match(head):
        return True
    if _FLE.match(head):
        return True
    return bool(_LUA_ERR.match(head))


def _unwrap(lines: list[str], width: int) -> list[str]:
    """Rejoin lines that TeX hard-wrapped at exactly `width` characters."""
    out: list[str] = []
    cur: str | None = None
    for ln in lines:
        cur = ln if cur is None else cur + ln
        if len(ln) == width or (not ln.isascii() and len(ln.encode()) == width):
            continue
        out.append(cur)
        cur = None
    if cur is not None:
        out.append(cur)
    return out


def _detect_wrap(lines: list[str], max_print_line: int | None) -> int | None:
    if max_print_line:
        return max_print_line if any(len(x) == max_print_line for x in lines) else None
    over = 0  # lines longer than 79 (the banner and the stack statistics are never wrapped)
    hits = 0
    for x in lines[1:]:
        if "stack positions out of" in x:
            continue  # TeX's memory statistics are never wrapped, but are often 79 wide
        n = len(x) if x.isascii() else len(x.encode())
        if n > 79:
            over += 1
        elif n == 79:
            hits += 1
    return 79 if hits and over <= 2 else None


class _Parser:
    def __init__(self, text: str, include_info: bool, max_print_line: int | None):
        lines = split_lines(text)
        self.info = TexLogInfo()
        width = _detect_wrap(lines, max_print_line)
        if width:
            lines = _unwrap(lines, width)
            self.info.wrapped_at = width
        self.lines = lines
        self.n = len(lines)
        self.include_info = include_info
        self.out: list[Diagnostic] = []
        self.files = _FileStack(self.info.files)
        self.runaway: str | None = None
        self.i = 0

    # -- helpers ---------------------------------------------------------
    def _cur(self) -> str | None:
        f = self.files.current
        return clean_path(f) if f else None

    def _cur_tex(self) -> str | None:
        f = self.files.current_tex
        return clean_path(f) if f else None

    def _continuations(self, msg: str, raw: list[str]) -> str:
        """Append `(pkg)  text` and indented lines that continue a message."""
        count = 0
        while self.i < self.n and count < 40:
            ln = self.lines[self.i]
            if not ln.strip() or _is_boundary(ln):
                break
            if m := _CONT.match(ln[:MAX_MATCH]):
                msg += " " + m.group(2).strip()
            elif _CONT_EMPTY.match(ln):
                pass
            elif ln[0] in " \t":
                msg += " " + ln.strip()
            else:
                break
            raw.append(ln[:500])
            self.i += 1
            count += 1
        return msg.strip()

    def _read_context(self, diag: Diagnostic) -> None:
        """Consume stack, help text and the `l.NN ...` context after a message."""
        lines, n = self.lines, self.n
        i = self.i
        limit = min(n, i + 60)
        found: int | None = None
        k = i
        while k < limit:
            ln = lines[k]
            if _CONTEXT.match(ln[:MAX_MATCH]):
                found = k
                break
            if _is_boundary(ln):
                break
            k += 1
        if self.runaway:
            diag.stack.append(self.runaway)
            self.runaway = None
        if found is None:
            # no l.NN: take one blank-separated group (stack and help text)
            j = i
            seen_text = False
            blank_run = 0
            while j < n and j < i + 30:
                ln = lines[j]
                if _is_boundary(ln):
                    break
                if not ln.strip():
                    if seen_text:
                        break
                    blank_run += 1
                    if blank_run > 1:
                        break
                    j += 1
                    continue
                seen_text = True
                self._classify(diag, ln)
                j += 1
            self.i = j
            return
        for j in range(i, found):
            ln = lines[j]
            if ln.strip():
                self._classify(diag, ln)
        m = _CONTEXT.match(lines[found][:MAX_MATCH])
        assert m is not None
        diag.line = diag.line or to_int(m.group(1))
        diag.context.append(lines[found].replace("^^M", "").strip())
        diag.raw.append(lines[found][:500])
        j = found + 1
        if j < n and not _is_boundary(lines[j]):
            pad = lines[j]
            padded = pad.replace("^^M", "").strip()
            if padded and not padded.startswith("?"):
                diag.context.append(padded)
                diag.raw.append(pad[:500])
            j += 1
        count = 0
        while j < n and count < 12:
            ln = lines[j]
            if not ln.strip() or _is_boundary(ln):
                break
            if ln.strip() != "?":
                diag.help.append(ln.strip())
                diag.raw.append(ln[:500])
            j += 1
            count += 1
        self.i = j

    def _classify(self, diag: Diagnostic, ln: str) -> None:
        s = ln.rstrip()
        diag.raw.append(s[:500])
        if len(diag.raw) > 60:
            return
        if _STACKLINE.match(s[:MAX_MATCH]) or s.startswith(("\t", "stack traceback")):
            diag.stack.append(s.strip())
        elif s.strip() and s.strip() != "?":
            diag.help.append(s.strip())

    def _finish(self, diag: Diagnostic) -> None:
        if diag.code is None:
            diag.code = _code_for(diag.message)
        self.out.append(diag)

    # -- main loop -------------------------------------------------------
    def run(self) -> ParseResult:
        while self.i < self.n:
            line = self.lines[self.i]
            self.i += 1
            if line:
                self._line(line)
        self._propagate_lines()
        return ParseResult(self.out, self.info)

    def _line(self, line: str) -> None:
        head = line[:MAX_MATCH]
        info = self.info
        c = head[:1]

        if m := _BANNER.match(head):
            info.banner = head
            info.engine = _engine_name(m.group(1))
            info.version = m.group(2)
            if f := _FORMAT.search(m.group(3) or "") or _FORMAT.search(
                m.group(4) or ""
            ):
                info.format = f.group(1)
            return
        if m := _FLE.match(head):
            self._error(m.group(3) or "", clean_path(m.group(1)), int(m.group(2)), line)
            return
        if c == "!" and (m := _BANG.match(head)):
            self._error(m.group(1).strip(), self._cur(), None, line)
            return
        if m := _LUA_ERR.match(head):
            self._lua_error(m, line)
            return
        if m := _RUNAWAY.match(head):
            text = self.lines[self.i] if self.i < self.n else ""
            if text and not _is_boundary(text):
                self.i += 1
            self.runaway = f"Runaway {m.group(1)}: {text.strip()}"
            return
        if m := _WARN.match(head):
            self._tex_warning(m, line)
            return
        if m := _ENGINE_MSG.match(head):
            self._engine_message(m, line)
            return
        if m := _BOX.match(head):
            self._box(m, line)
            return
        if m := _MISSING_CHAR.match(head):
            self._finish(
                Diagnostic(
                    "warning",
                    f"Missing character: {m.group(1)}",
                    self._cur(),
                    code="missing-character",
                    raw=[line[:500]],
                )
            )
            return
        if self._misc(head, line):
            return
        if _CONT.match(head) or _CONT_EMPTY.match(head):
            return  # `(pkg)   more text` is never a file open
        for _pm in _PAGE_MARK.finditer(head):
            info.page_markers += 1
        self.files.feed(line)

    def _misc(self, head: str, line: str) -> bool:
        info = self.info
        if head.startswith("**") and info.input_name is None and not info.files:
            info.input_name = head[2:].strip()
            return True
        if head.startswith("entering extended mode"):
            info.extended_mode = True
            return True
        s = head.strip()
        if s.endswith("\\write18 enabled."):
            info.shell_escape = "restricted" if "restricted" in s else "enabled"
            return True
        if s.endswith("system commands enabled."):
            info.shell_escape = "restricted" if "restricted" in s else "enabled"
            return True
        if s.startswith("file:line:error style messages enabled"):
            info.file_line_error = True
            return True
        if s.startswith("%&-line parsing enabled"):
            info.parse_first_line = True
            return True
        if m := _LATEX_DATE.match(head):
            info.latex_date = m.group(1)
            return False  # still fed (it is followed by nothing interesting)
        if m := _DOCCLASS.match(head):
            info.document_class = m.group(1)
            return True
        if m := _OPENOUT.match(head):
            name = m.group(1).strip().strip("`").rstrip(".").rstrip("'")
            info.openout.append(name)
            return True
        if m := _RUNSYSTEM.match(head):
            info.shell_commands.append(head)
            if m.group(2) == "disabled":
                self._finish(
                    Diagnostic(
                        "warning",
                        f"Shell escape disabled, not run: {m.group(1)}",
                        self._cur_tex(),
                        code="shell-escape-disabled",
                        raw=[line[:500]],
                    )
                )
            return True
        if m := _OUTPUT.match(head):
            info.output = m.group(1)
            info.pages = int(m.group(2))
            info.bytes = to_int(m.group(3))
            return True
        if head.startswith("No pages of output"):
            info.no_output = True
            return True
        if m := _LUA_BACKEND.match(head):
            self._lua_backend(m, line)
            return True
        if m := _LUAOTFLOAD.match(head):
            msg = m.group(2)
            if re.search(
                r"\b(?:error|warning|fail\w*|cannot|not found)\b", msg, re.IGNORECASE
            ):
                self._finish(
                    Diagnostic(
                        "warning",
                        f"luaotfload ({m.group(1)}): {msg}"[:MAX_MATCH],
                        self._cur_tex(),
                        code="luaotfload",
                        package="luaotfload",
                        raw=[line[:500]],
                    )
                )
            # luaotfload prints a file's closing paren on the same line
            self.files.feed(line)
            return True
        if m := _LUA_LOWER.match(head):
            kind: Kind = "error" if m.group(1) == "error" else "warning"
            self._finish(
                Diagnostic(
                    kind,
                    f"Lua {m.group(1)}: {m.group(2)}".strip(),
                    self._cur_tex(),
                    code="lua-error" if kind == "error" else "lua",
                    raw=[line[:500]],
                )
            )
            return True
        if m := _DVIPDFMX.match(head):
            sev = (m.group(2) or "info").lower()
            if sev in {"fatal", "error", "warning"}:
                self._finish(
                    Diagnostic(
                        "warning" if sev == "warning" else "error",
                        m.group(3).strip() or f"{m.group(1)} {sev}",
                        None,
                        source=m.group(1).lower(),
                        code=f"{m.group(1).lower()}-{sev}",
                        raw=[line[:500]],
                    )
                )
                if sev == "fatal":
                    info.fatal = True
                    info.no_output = True
            return True
        if m := _STARSTAR.match(head):
            self._finish(
                Diagnostic(
                    "error" if m.group(1) == "ERROR" else "warning",
                    m.group(2).strip(),
                    None,
                    source="dvipdfmx",
                    code="dvipdfmx",
                    raw=[line[:500]],
                )
            )
            return True
        if m := _DRIVER.match(head):
            self._finish(
                Diagnostic(
                    "error",
                    f"Output driver failed (return code {m.group(1)}); the PDF may be invalid",
                    None,
                    code="driver-error",
                    raw=[line[:500]],
                )
            )
            return True
        if m := _KPSE.match(head):
            msg = m.group(1)
            if msg.startswith("Running"):
                if self.include_info:
                    self._finish(
                        Diagnostic(
                            "info",
                            f"kpathsea: {msg}",
                            source="kpathsea",
                            code="kpathsea-run",
                            raw=[line[:500]],
                        )
                    )
            else:
                self._finish(
                    Diagnostic(
                        "warning",
                        f"kpathsea: {msg}",
                        source="kpathsea",
                        code="kpathsea",
                        raw=[line[:500]],
                    )
                )
            return True
        return bool(head.startswith(("Latexmk:", "Rule '")))

    # -- message kinds ---------------------------------------------------
    def _error(self, msg: str, file: str | None, line: int | None, first: str) -> None:
        raw = [first[:500]]
        msg = self._continuations(msg.rstrip(), raw)
        diag = Diagnostic("error", msg, file, line, raw=raw)
        if m := _PKG_ERR.match(msg[:200]):
            diag.package = m.group(2)
        if "Emergency stop" in msg[:40]:
            self.info.fatal = True
        if "==> Fatal error" in msg:
            self.info.fatal = True
            if "no output" in msg:
                self.info.no_output = True
        self._read_context(diag)
        if diag.code is None:
            diag.code = _code_for(msg)
        if diag.code is None and m:
            diag.code = f"{m.group(1).lower()}-error"
        diag.help = diag.help[:12]
        self._finish(diag)

    def _lua_error(self, m: re.Match[str], first: str) -> None:
        diag = Diagnostic(
            "error",
            f"Lua error: {m.group(3).strip()}",
            self._cur_tex(),
            code="lua-error",
            raw=[first[:500]],
        )
        diag.stack.append(f"{m.group(1)}:{m.group(2)}")
        self._read_context(diag)
        self._finish(diag)

    def _lua_backend(self, m: re.Match[str], line: str) -> None:
        text = m.group(3).strip()
        kind: Kind = "error" if m.group(1) == "error" else "warning"
        if "no pages of output" in text.lower():
            self.info.no_output = True
        self._finish(
            Diagnostic(
                kind,
                f"LuaTeX {m.group(1)} ({m.group(2)}): {text}",
                self._cur_tex(),
                package="luatex",
                raw=[line[:500]],
            )
        )

    def _tex_warning(self, m: re.Match[str], line: str) -> None:
        who, sev, _extra, msg = m.group(1), m.group(2), m.group(3), m.group(4)
        raw = [line[:500]]
        if sev == "Error":
            self._error(f"{who} Error: {msg}", self._cur(), None, line)
            return
        msg = self._continuations(msg, raw)
        if sev == "Info":
            if self.include_info:
                self._finish(
                    Diagnostic(
                        "info",
                        f"{who} Info: {msg}",
                        self._cur(),
                        to_int(_first(_ON_LINE, msg)),
                        package=_package_of(who),
                        raw=raw,
                    )
                )
            return
        self._warning_common(who, msg, "warning", raw, f"{who} Warning")

    def _warning_common(
        self, who: str, msg: str, kind: Kind, raw: list[str], label: str
    ) -> None:
        ln = _ON_LINE.search(msg)
        page = _ON_PAGE.search(msg)
        text = _ON_LINE.sub("", msg).rstrip(" .")
        text = text or msg
        # a line number refers to the innermost file; line-less warnings to the document
        where = self._cur() if ln else self._cur_tex()
        full = f"{label}: {text}"
        diag = Diagnostic(
            kind,
            full,
            where,
            int(ln.group(1)) if ln else None,
            package=_package_of(who),
            page=int(page.group(1)) if page else None,
            raw=raw,
        )
        self._finish(diag)
        low = msg.lower()
        if "there were undefined" in low:
            self.info.undefined_references = True
        if "rerun" in low:
            self.info.rerun = True

    def _engine_message(self, m: re.Match[str], line: str) -> None:
        who, sev, extra, msg = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        raw = [line[:500]]
        msg = self._continuations(msg, raw)
        label = f"{who} {sev}" + (f" ({extra})" if extra else "")
        kind: Kind = "error" if sev == "error" else "warning"
        diag = Diagnostic(
            kind,
            f"{label}: {msg}",
            self._cur(),
            package=_package_of(who),
            raw=raw,
        )
        nxt = self.lines[self.i] if self.i < self.n else ""
        if nxt.startswith("<") or _CONTEXT.match(nxt[:MAX_MATCH]):
            self._read_context(diag)
        self._finish(diag)

    def _box(self, m: re.Match[str], line: str) -> None:
        ln = _BOX_LINES.search(line[:MAX_MATCH])
        diag = Diagnostic(
            "box",
            m.group(1),
            self._cur(),
            int(ln.group(1)) if ln else None,
            end_line=int(ln.group(2)) if ln and ln.group(2) else None,
            raw=[line[:500]],
        )
        # the offending text follows, up to a blank line
        count = 0
        while self.i < self.n and count < 6:
            nxt = self.lines[self.i]
            if not nxt.strip() or _is_boundary(nxt):
                break
            diag.raw.append(nxt[:500])
            self.i += 1
            count += 1
        self._finish(diag)

    # -- post-processing -------------------------------------------------
    def _propagate_lines(self) -> None:
        """A line-less error followed by `Emergency stop` took its line from that stop."""
        for prev, cur in zip(self.out, self.out[1:], strict=False):
            if (
                cur.code == "emergency-stop"
                and cur.line
                and prev.kind == "error"
                and prev.line is None
                and prev.file == cur.file
            ):
                prev.line = cur.line


def _first(pat: re.Pattern[str], s: str) -> str | None:
    m = pat.search(s)
    return m.group(1) if m else None


def parse_tex(
    text: str, *, include_info: bool = False, max_print_line: int | None = None
) -> ParseResult:
    """Diagnostics plus run facts (engine, output, pages, files) from a TeX log.

    `include_info` also returns LaTeX/Package `Info` messages as kind="info".
    `max_print_line` forces unwrapping at that column; by default 79-column
    wrapping is detected automatically.
    """
    try:
        return _Parser(text, include_info, max_print_line).run()
    except RecursionError:  # pragma: no cover - defensive, the parser has no recursion
        return ParseResult([], TexLogInfo())


def parse_tex_log(text: str) -> list[Diagnostic]:
    """Errors, warnings and box warnings from a TeX engine log."""
    return parse_tex(text).diagnostics
