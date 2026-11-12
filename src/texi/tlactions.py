"""Post-install actions of TeX Live packages, done without ``tlmgr``.

A tlpdb entry can carry ``execute`` and ``postaction`` lines. After ``tlmgr``
has unpacked a batch of packages it turns them into configuration files and runs
the TeX Live tools that act on them. :func:`run` does the same once per batch for
the packages :mod:`texi.archive` cloned into a project tree. The behavior is
taken from the Perl sources of a TeX Live installation (``TLUtils.pm``,
``TLPOBJ.pm``, ``TLPDB.pm`` and ``tlmgr.pl``); see the comments for the
subroutine each part mirrors.

====================================  ==========================================
``execute addMap|addMixedMap|addKanjiMap FILE``
    ``updmap.cfg`` gets ``Map|MixedMap|KanjiMap FILE``, then ``updmap-user``
    (usertree) or ``updmap-sys`` (standalone tree) runs once.
``execute AddFormat name=... engine=...``
    ``fmtutil.cnf`` gets the format line, then ``fmtutil-user`` or
    ``fmtutil-sys`` builds the new formats, the formats of an updated engine and
    the formats whose ``fmttriggers`` name a package of the batch, once.
``execute AddHyphen name=... file=...``
    ``language.dat``, ``language.def`` and ``language.dat.lua`` are written again
    from every hyphenation entry of the tree, then the formats that read them are
    built again.
``postaction script file=...``
    Run for a standalone tree, as ``tlmgr`` does.
``postaction shortcut|fileassoc|filetype|progid``
    Ignored: ``tlmgr`` only acts on Windows for these (desktop integration).
====================================  ==========================================

Where the files go
------------------
A usertree (``.texenv/`` with ``texmf-dist`` contents at the top) writes
``web2c/updmap.cfg`` and ``web2c/fmtutil.cnf`` into the tree itself, listing only
the maps and formats of the tree's packages. ``updmap`` and ``fmtutil`` merge all
such files they find in the search path, so the system's entries stay. This is
what ``tlmgr --usermode`` does for ``updmap.cfg``. ``language.dat`` and friends
shadow the system's files instead of being merged, so they are written into
``texmf-var/tex/generic/config`` with the entries of the system tree *and* of the
usertree. A standalone tree (a full TeX Live layout with ``bin/<arch>``) writes
the files where ``tlmgr`` does: ``texmf-dist/web2c/updmap.cfg``,
``texmf-dist/web2c/fmtutil.cnf`` and the language files in ``TEXMFSYSVAR``,
generated from all packages of the tree.

The entries come from the tree's ``tlpkg/texlive.tlpdb``, so running again gives
the same files and removing a package removes its entries.

Never calls ``tlmgr``. The ``updmap`` and ``fmtutil`` programs are still used.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from texi import lsr, texenv

#: Programs that a standalone tree must supply itself.
TREE_TOOLS = frozenset({
    "updmap",
    "updmap-sys",
    "fmtutil",
    "fmtutil-sys",
    "kpsewhich",
    "texlua",
})

#: ``--no-error-if-no-engine`` value (``TLConfig::PartialEngineSupport``).
PARTIAL_ENGINES = "luametatex,luajithbtex,luajittex,mfluajit,xetex"

#: Kinds of ``postaction`` that ``tlmgr`` handles on Windows only, with the reason they are ignored.
IGNORED_POSTACTIONS = {
    "shortcut": "start menu and desktop shortcuts exist on Windows only",
    "fileassoc": "file associations exist on Windows only",
    "filetype": "file types exist on Windows only",
    "progid": "program ids exist on Windows only",
}

Notice = Callable[[str], None]


class ExecuteError(ValueError):
    """An ``execute`` line that cannot be parsed."""


# --- parsing the execute lines (TLUtils::parse_AddFormat_line etc.) -----------


@dataclass(frozen=True)
class FormatDef:
    """An ``execute AddFormat`` line."""

    name: str
    engine: str
    patterns: str = "-"
    options: str = ""
    #: False for ``mode=disabled``: the line is written as a comment.
    enabled: bool = True
    triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class HyphenDef:
    """An ``execute AddHyphen`` line."""

    name: str
    lefthyphenmin: str
    righthyphenmin: str
    file: str = ""
    synonyms: tuple[str, ...] = ()
    databases: tuple[str, ...] = ("dat", "def", "lua")
    #: None when the key is absent (an empty value is kept as "").
    patterns: str | None = None
    exceptions: str | None = None
    special: str | None = None
    comment: str | None = None


def _words(line: str, what: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError as e:
        msg = f"cannot parse {what} line {line!r}: {e}"
        raise ExecuteError(msg) from e


def parse_add_format(line: str) -> FormatDef:
    """Parse what follows ``execute AddFormat``.

    Raises
    ------
    ExecuteError
        Unknown key, or ``name``/``engine`` missing.
    """
    values: dict[str, str] = {}
    for word in _words(line, "AddFormat"):
        m = re.fullmatch(r"(name|engine|mode|patterns|options|fmttriggers)=(.*)", word)
        if not m:
            msg = f"Unknown format directive {word}"
            raise ExecuteError(msg)
        values[m[1]] = m[2]
    if not values.get("name"):
        msg = "AddFormat line needs name=something"
        raise ExecuteError(msg)
    if not values.get("engine"):
        msg = "AddFormat line needs engine=something"
        raise ExecuteError(msg)
    return FormatDef(
        values["name"],
        values["engine"],
        values.get("patterns") or "-",
        values.get("options") or "",
        values.get("mode") != "disabled",
        tuple(t for t in values.get("fmttriggers", "").split(",") if t),
    )


def parse_add_hyphen(line: str) -> HyphenDef:
    """Parse what follows ``execute AddHyphen``.

    Raises
    ------
    ExecuteError
        Unknown key, missing ``name`` or a ``lefthyphenmin``/``righthyphenmin``
        that is not one digit.
    """
    values: dict[str, str | None] = {}
    for word in _words(line, "AddHyphen"):
        key, _, rest = word.partition("=")
        if key not in {
            "name",
            "lefthyphenmin",
            "righthyphenmin",
            "file",
            "file_patterns",
            "file_exceptions",
            "luaspecial",
            "databases",
            "synonyms",
            "comment",
        }:
            msg = f"Unknown AddHyphen directive {key}: {line}"
            raise ExecuteError(msg)
        values[key] = rest if "=" in word else None
    for key in ("name", "file", "luaspecial", "databases", "synonyms"):
        if key in values and not values[key]:
            msg = f"AddHyphen line needs {key}=something: {line}"
            raise ExecuteError(msg)
    if not values.get("name"):
        msg = f"AddHyphen is missing name setting: {line}"
        raise ExecuteError(msg)
    for key in ("lefthyphenmin", "righthyphenmin"):
        if not re.fullmatch(r"[0-9]", values.get(key) or ""):
            msg = (
                f"AddHyphen has missing or bad {key} ({values.get(key) or ''}): {line}"
            )
            raise ExecuteError(msg)
    synonyms = values.get("synonyms") or ""
    databases = values.get("databases") or ""
    return HyphenDef(
        values["name"] or "",
        values["lefthyphenmin"] or "",
        values["righthyphenmin"] or "",
        values.get("file") or "",
        tuple(synonyms.split(",")) if synonyms else (),
        tuple(databases.split(",")) if databases else ("dat", "def", "lua"),
        values.get("file_patterns"),
        values.get("file_exceptions"),
        values.get("luaspecial"),
        values.get("comment"),
    )


_MAP_RE = re.compile(r"add(Mixed|Kanji)?Map\s+(.*?)\s*$")
_MAP_KEYWORD = {None: "Map", "Mixed": "MixedMap", "Kanji": "KanjiMap"}


def parse_map(execute: str) -> tuple[str, str] | None:
    """``(keyword, file)`` of an ``addMap`` line (keyword ``Map``, ``MixedMap`` or ``KanjiMap``), or None."""
    m = _MAP_RE.match(execute)
    return (_MAP_KEYWORD[m[1]], m[2]) if m else None


def _kind(execute: str) -> str:
    return execute.split(None, 1)[0] if execute.split() else ""


# --- config file contents (TLPOBJ::*_lines) ---------------------------------

#: ``package name -> execute lines`` (without the leading ``execute``).
Executes = Mapping[str, Sequence[str]]


def updmap_cfg_lines(executes: Executes) -> list[str]:
    """``Map`` lines of all packages, sorted by map file within a package."""
    lines: list[str] = []
    for pkg in sorted(executes):
        maps: dict[str, str] = {}
        for e in executes[pkg]:
            if parsed := parse_map(e):
                maps[parsed[1]] = parsed[0]
        lines += [f"{maps[f]} {f}\n" for f in sorted(maps)]
    return lines


def format_defs(executes: Executes) -> list[tuple[str, FormatDef]]:
    """``(package, format)`` of every ``AddFormat`` line, packages in name order."""
    out: list[tuple[str, FormatDef]] = []
    for pkg in sorted(executes):
        out.extend(
            (pkg, parse_add_format(e.split(None, 1)[1]))
            for e in executes[pkg]
            if _kind(e) == "AddFormat"
        )
    return out


def fmtutil_cnf_lines(executes: Executes) -> list[str]:
    """Format lines of all packages, each package's block led by ``# from PKG:``."""
    lines: list[str] = []
    first: set[str] = set()
    for pkg, f in format_defs(executes):
        if pkg not in first:
            first.add(pkg)
            lines.append(f"#\n# from {pkg}:\n")
        mode = "" if f.enabled else "#! "
        lines.append(f"{mode}{f.name} {f.engine} {f.patterns} {f.options}\n")
    return lines


def hyphen_defs(executes: Executes, database: str) -> list[tuple[str, HyphenDef]]:
    """``(package, hyphenation)`` of every ``AddHyphen`` line for ``database`` (dat, def, lua)."""
    out: list[tuple[str, HyphenDef]] = []
    for pkg in sorted(executes):
        for e in executes[pkg]:
            if _kind(e) == "AddHyphen":
                h = parse_add_hyphen(e.split(None, 1)[1])
                if database in h.databases:
                    out.append((pkg, h))
    return out


def _hyphen_lines(
    executes: Executes,
    database: str,
    comment_char: str,
    render: Callable[[HyphenDef], list[str]],
    disabled: Iterable[str],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    off = set(disabled)
    for pkg, h in hyphen_defs(executes, database):
        if pkg not in seen:
            seen.add(pkg)
            lines.append(f"{comment_char} from {pkg}:\n")
        if h.comment:
            lines.append(f"{comment_char} {h.comment}\n")
        if h.name not in off:
            lines += render(h)
    return lines


def language_dat_lines(executes: Executes, disabled: Iterable[str] = ()) -> list[str]:
    """``language.dat`` lines: ``name file`` and ``=synonym``."""

    def render(h: HyphenDef) -> list[str]:
        return [f"{h.name} {h.file}\n", *(f"={s}\n" for s in h.synonyms)]

    return _hyphen_lines(executes, "dat", "%", render, disabled)


def language_def_lines(executes: Executes, disabled: Iterable[str] = ()) -> list[str]:
    r"""``language.def`` lines: ``\\addlanguage`` for the name and each synonym."""

    def render(h: HyphenDef) -> list[str]:
        return [
            f"\\addlanguage{{{n}}}{{{h.file}}}{{}}{{{h.lefthyphenmin}}}{{{h.righthyphenmin}}}\n"
            for n in (h.name, *h.synonyms)
        ]

    return _hyphen_lines(executes, "def", "%", render, disabled)


def language_lua_lines(executes: Executes, disabled: Iterable[str] = ()) -> list[str]:
    """``language.dat.lua`` table entries."""

    def render(h: HyphenDef) -> list[str]:
        syn = ", ".join(f"'{s}'" for s in h.synonyms)
        ret = [
            f"['{h.name}'] = {{",
            f"\tloader = '{h.file}',",
            f"\tlefthyphenmin = {h.lefthyphenmin},",
            f"\trighthyphenmin = {h.righthyphenmin},",
            f"\tsynonyms = {{ {syn} }},",
        ]
        if h.patterns is not None:
            ret.append(f"\tpatterns = '{h.patterns}',")
        if h.exceptions is not None:
            ret.append(f"\thyphenation = '{h.exceptions}',")
        if h.special is not None:
            ret.append(f"\tspecial = '{h.special}',")
        ret.append("},")
        return [f"\t{line}\n" for line in ret]

    return _hyphen_lines(executes, "lua", "--", render, disabled)


def disabled_in(local: Path | None, comment_char: str) -> list[str]:
    """Names switched off in a ``*-local`` file by a ``%!NAME`` (or ``--!NAME``) line."""
    if local is None or not local.is_file():
        return []
    pat = re.compile(rf"^{re.escape(comment_char)}!(\S+)\s*$")
    return [
        m[1]
        for ln in local.read_text(errors="replace").splitlines()
        if (m := pat.match(ln))
    ]


def _stamp(comment_char: str) -> str:
    """The ``# Generated by ...`` line, like ``_create_config_files``."""
    return (
        f"{comment_char} Generated by texi on {time.strftime('%a %b %e %H:%M:%S %Y')}\n"
    )


def read_executes(tlpdb: Path) -> dict[str, list[str]]:
    """``package -> execute lines`` of a tlpdb file; empty when it cannot be read."""
    try:
        text = tlpdb.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, list[str]] = {}
    name = ""
    for line in text.splitlines():
        if line.startswith("name "):
            name = line[5:].strip()
        elif not line.strip():
            name = ""
        elif name and line.startswith("execute "):
            out.setdefault(name, []).append(line[8:].strip())
    return out


def read_installation_option(tlpdb: Path, key: str) -> str | None:
    """Value of an ``opt_<key>`` setting of ``00texlive.installation``, or None."""
    try:
        text = tlpdb.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = f"depend opt_{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


# --- what changed ---------------------------------------------------------------


@dataclass
class Batch:
    """What a batch of installs and removals changed; input of :func:`run`."""

    maps: bool = False
    formats: bool = False
    hyphens: bool = False
    #: Formats of installed packages (``announce_execute_actions("enable")``).
    new_formats: dict[str, FormatDef] = field(default_factory=dict)
    #: Packages installed or updated (input of ``fmttriggers``).
    changed: set[str] = field(default_factory=set)
    #: ``(package, "install"|"remove", file)`` of ``postaction script`` lines.
    scripts: list[tuple[str, str, str]] = field(default_factory=list)
    #: Problems found in the lines; reported by :func:`run`.
    problems: list[str] = field(default_factory=list)

    def note(
        self, name: str, how: str, executes: Iterable[str], postactions: Iterable[str]
    ) -> None:
        """Record the lines of package ``name`` being installed (``how`` is ``install``) or removed."""
        enable = how == "install"
        if enable:
            self.changed.add(name)
        for e in executes:
            kind = _kind(e)
            if parse_map(e):
                self.maps = True
            elif kind == "AddFormat":
                self.formats = True
                try:
                    f = parse_add_format(e.split(None, 1)[1])
                except (ExecuteError, IndexError) as err:
                    self.problems.append(f"{name}: {err}")
                else:
                    if enable:
                        self.new_formats[f.name] = f
            elif kind == "AddHyphen":
                self.hyphens = True
            else:
                self.problems.append(f"{name}: unknown execute {e!r}")
        for pa in postactions:
            kind = _kind(pa)
            if kind == "script":
                m = re.search(r"\bfile=(\S+)", pa)
                if m:
                    self.scripts.append((name, how, m[1]))
                else:
                    self.problems.append(f"{name}: postaction without file: {pa!r}")
            elif kind not in IGNORED_POSTACTIONS:
                self.problems.append(f"{name}: don't know how to do postaction {pa!r}")

    @property
    def empty(self) -> bool:
        """True if there is nothing to do."""
        return not (
            self.maps or self.formats or self.hyphens or self.scripts or self.problems
        )


@dataclass
class Outcome:
    """What :func:`run` did."""

    #: Commands that were run.
    ran: list[list[str]] = field(default_factory=list)
    #: Messages for the user (also given to the ``notice`` callback).
    warnings: list[str] = field(default_factory=list)
    #: ``name/engine`` of formats that did not build.
    failed_formats: list[str] = field(default_factory=list)
    #: True if ``updmap`` failed.
    maps_failed: bool = False
    #: Files that were written.
    written: list[Path] = field(default_factory=list)


# --- environment and files ----------------------------------------------------


def bin_dir(root: Path, arch: str | None) -> Path | None:
    """``root/bin/<arch>`` of a standalone tree (the only arch present when ``arch`` is unknown)."""
    if arch and (root / "bin" / arch).is_dir():
        return root / "bin" / arch
    return next(iter(sorted(p for p in (root / "bin").glob("*") if p.is_dir())), None)


def tool_env(
    env_dir: Path, *, standalone: bool, arch: str | None = None
) -> dict[str, str]:
    """Environment for the TeX Live tools: the tree's ``TEXMF*`` variables, or its ``bin`` first on ``PATH``."""
    env = dict(os.environ)
    root = env_dir.resolve()
    if standalone:
        if b := bin_dir(root, arch):
            env = texenv.clone_env(root, b.name, env)
    else:
        env["TEXMFHOME"] = str(root)
        env["TEXMFVAR"] = str(root / "texmf-var")
        env["TEXMFCONFIG"] = str(root / "texmf-config")
    return env


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _Run:
    """One call of :func:`run`: environment, paths, output."""

    def __init__(
        self,
        env_dir: Path,
        standalone: bool,
        arch: str | None,
        notice: Notice | None,
        log: Notice | None,
    ) -> None:
        self.standalone = standalone
        self.root = env_dir.resolve()
        self.env = tool_env(env_dir, standalone=standalone, arch=arch)
        self.notice = notice or (lambda _m: None)
        self.log = log or (lambda _m: None)
        self.out = Outcome()
        self.suffix = "sys" if standalone else "user"
        self.tlpdb = self.root / "tlpkg" / "texlive.tlpdb"

    def warn(self, msg: str) -> None:
        self.out.warnings.append(msg)
        self.notice(msg)

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Run ``cmd`` in the tree's environment; None if the program does not exist.

        In a standalone tree the TeX Live tools must come from the tree. A missing
        or dangling one is reported and never replaced by a system tool, which would
        read the tree through ``TEXMFROOT`` and fail in confusing ways.
        """
        if self.standalone and cmd[0] in TREE_TOOLS:
            exe = shutil.which(cmd[0], path=self.env.get("PATH"))
            if exe is None or self.root not in Path(exe).parents:
                self.warn(
                    f"{cmd[0]} is missing from the tree (a package did not install); "
                    "not using the system's"
                )
                return None
        self.out.ran.append(cmd)
        self.log("running " + " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                env=self.env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return None

    def kpse_var(self, var: str) -> str | None:
        """``kpsewhich -var-value=VAR`` (single-path values only)."""
        r = self.run(["kpsewhich", f"-var-value={var}"])
        value = r.stdout.strip() if r and r.returncode == 0 else ""
        return value if value and not re.search(r"[{};:]", value) else None

    def kpse_file(self, name: str) -> str | None:
        """Where ``kpsewhich NAME`` finds a file."""
        r = self.run(["kpsewhich", name])
        return (
            r.stdout.strip() if r and r.returncode == 0 and r.stdout.strip() else None
        )

    # config files

    def write_config(
        self,
        dest: Path,
        header: Path | None,
        body: list[str],
        comment_char: str,
        *,
        stamp: bool = True,
        local: Path | None = None,
        post: Sequence[str] = (),
    ) -> bool:
        """Write ``header + body + local + post`` to ``dest`` (``_create_config_files``).

        An unchanged file (apart from the date in the first line) is left alone.
        """
        lines = _read_lines(header) if header and header.is_file() else []
        lines += body
        if local is not None and local.is_file():
            lines += _read_lines(local)
        lines += post
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        text = "".join(lines)
        try:
            old = dest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            old = None
        if old is not None:
            current = old
            if stamp and current.startswith(f"{comment_char} Generated by"):
                current = current.split("\n", 1)[1] if "\n" in current else ""
            elif stamp:  # written by hand: keep a copy, like tlmgr does in usermode
                _write_atomic(dest.with_name(dest.name + ".backup"), old)
            if current == text:
                return False
        _write_atomic(dest, (_stamp(comment_char) if stamp else "") + text)
        self.out.written.append(dest)
        return True

    def system_root(self) -> Path | None:
        """The system TeX Live (``TEXMFROOT``), for a usertree."""
        value = self.kpse_var("TEXMFROOT")
        return Path(value) if value and Path(value).is_dir() else None

    def texmflocal_config(self) -> Path | None:
        value = self.kpse_var("TEXMFLOCAL")
        return Path(value) / "tex" / "generic" / "config" if value else None


# --- the actions ----------------------------------------------------------------


def _update_maps(ctx: _Run, executes: Executes) -> None:
    """``create_updmap`` and ``updmap-user|sys`` (tlmgr.pl ``handle_execute_actions``)."""
    if ctx.standalone:
        dest = ctx.root / "texmf-dist" / "web2c" / "updmap.cfg"
        header = ctx.root / "texmf-dist" / "web2c" / "updmap-hdr.cfg"
    else:
        dest, header = ctx.root / "web2c" / "updmap.cfg", None
    ctx.write_config(dest, header, updmap_cfg_lines(executes), "#")


#: ``(file, static header, comment char, line generator, local file extension)``.
_LANGUAGE_FILES = (
    ("language.dat", "language.us", "%", language_dat_lines, "dat"),
    ("language.def", "language.us.def", "%", language_def_lines, "def"),
    ("language.dat.lua", "language.us.lua", "--", language_lua_lines, "dat.lua"),
)
_DEF_TAIL = [
    "%%% No changes may be made beyond this point.\n",
    "\n",
    "\\uselanguage {USenglish}             %%% This MUST be the last line of the file.\n",
]


def _language_files(ctx: _Run, executes: Executes) -> list[Path]:
    """Write ``language.dat``, ``language.def`` and ``language.dat.lua``.

    Mirrors ``create_language_dat|def|lua``: the static head file of the tree
    (``texmf-dist/tex/generic/config/language.us*``), the entries of all
    packages, the ``language-local.*`` file of ``TEXMFLOCAL`` and a fixed tail.
    A usertree takes the head and the entries of the system tree, with the
    usertree's packages replacing system packages of the same name, because
    these files shadow the system's. Without hyphenation packages of its own a
    usertree writes nothing and removes the files it wrote before.

    Returns
    -------
    list[Path]
        The files that apply now, to pass to ``fmtutil --byhyphen``.
    """
    names = [spec[0] for spec in _LANGUAGE_FILES]
    if ctx.standalone:
        head_root: Path | None = ctx.root
        entries = dict(executes)
        gen_dir = Path(ctx.kpse_var("TEXMFSYSVAR") or ctx.root / "texmf-var")
    else:
        gen_dir = ctx.root / "texmf-var"
        if not any(
            e.startswith("AddHyphen") for lines in executes.values() for e in lines
        ):
            for name in names:
                f = gen_dir / "tex" / "generic" / "config" / name
                if not f.is_file():
                    continue
                text = f.read_text(errors="replace")
                # language.def carries no stamp line; its fixed tail marks it
                if "Generated by texi" in text[:200] or (
                    name == "language.def" and _DEF_TAIL[0] in text
                ):
                    f.unlink()
            return [Path(p) for n in names if (p := ctx.kpse_file(n))]
        head_root = ctx.system_root()
        if head_root is None:
            ctx.warn(
                "no system TeX Live found: language.dat, language.def and language.dat.lua not written"
            )
            return []
        entries = {**read_executes(head_root / "tlpkg" / "texlive.tlpdb"), **executes}
    gen_dir = gen_dir / "tex" / "generic" / "config"
    head_dir = head_root / "texmf-dist" / "tex" / "generic" / "config"
    local_dir = ctx.texmflocal_config()
    written: list[Path] = []
    for name, head, cc, lines_of, ext in _LANGUAGE_FILES:
        if not (head_dir / head).is_file():
            ctx.warn(f"{head_dir / head} not found: {name} not written")
            continue
        local = local_dir / f"language-local.{ext}" if local_dir else None
        post = (
            _DEF_TAIL
            if name == "language.def"
            else ["}\n"]
            if name.endswith(".lua")
            else []
        )
        ctx.write_config(
            gen_dir / name,
            head_dir / head,
            lines_of(entries, disabled_in(local, cc)),
            cc,
            stamp=name != "language.def",
            local=local,
            post=post,
        )
        written.append(gen_dir / name)
    return written


def _update_formats_cnf(ctx: _Run, executes: Executes) -> None:
    """``create_fmtutil``."""
    if ctx.standalone:
        dest = ctx.root / "texmf-dist" / "web2c" / "fmtutil.cnf"
        header = ctx.root / "texmf-dist" / "web2c" / "fmtutil-hdr.cnf"
    else:
        dest, header = ctx.root / "web2c" / "fmtutil.cnf", None
    try:
        body = fmtutil_cnf_lines(executes)
    except ExecuteError as e:
        ctx.warn(f"fmtutil.cnf not updated: {e}")
        return
    ctx.write_config(dest, header, body, "#")


def _tail(text: str, n: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _select_formats(
    ctx: _Run, batch: Batch, executes: Executes, language: list[Path]
) -> list[str]:
    """The ``fmtutil`` selection of ``handle_execute_actions``.

    Formats of installed packages, formats built by an updated engine (a format
    named like its engine) and formats whose ``fmttriggers`` name a changed
    package (``check_announce_format_triggers``) are selected once each;
    after a hyphenation change also the formats that read the language files.
    """
    enabled = dict(batch.new_formats)
    try:
        for _pkg, f in format_defs(executes):
            if f.enabled and any(t in batch.changed for t in f.triggers):
                enabled.setdefault(f.name, f)
    except ExecuteError as e:
        ctx.warn(f"formats not rebuilt: {e}")
        return []
    engines = {n for n, f in enabled.items() if f.name == f.engine}
    select: list[str] = []
    for e in sorted(engines):
        select += ["--byengine", e]
    for n in sorted(enabled):
        if enabled[n].engine not in engines and enabled[n].enabled:
            select += ["--byfmt", n]
    if batch.hyphens:
        for p in language:
            select += ["--byhyphen", str(p)]
    return select


def _build_formats(ctx: _Run, select: list[str]) -> None:
    """One ``fmtutil-user|sys`` run for everything selected; failures become warnings."""
    tool = f"fmtutil-{ctx.suffix}"
    fd, name = tempfile.mkstemp(prefix="texi-fmtutil-", suffix=".status")
    os.close(fd)
    status = Path(name)
    try:
        cmd = [
            tool,
            *select,
            f"--no-error-if-no-engine={PARTIAL_ENGINES}",
            f"--status-file={status}",
        ]
        if read_installation_option(ctx.tlpdb, "create_formats") == "0":
            cmd.append("--refresh")  # only rebuild formats that exist
        r = ctx.run(cmd)
        if r is None:
            ctx.warn(f"{tool} not found: formats not built")
            return
        failed = []
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "FAILURE":
                failed.append(f"{parts[1]}/{parts[2]}")
        if r.returncode != 0 and not failed:
            failed = ["(see output)"]
        ctx.out.failed_formats += failed
        if failed:
            ctx.warn(
                f"{tool} could not build {', '.join(failed)}:\n{_tail(r.stdout + chr(10) + r.stderr)}"
            )
    finally:
        status.unlink(missing_ok=True)


def _run_updmap(ctx: _Run) -> None:
    tool = f"updmap-{ctx.suffix}"
    r = ctx.run([tool])
    if r is None:
        ctx.out.maps_failed = True
        ctx.warn(f"{tool} not found: font maps not updated")
    elif r.returncode != 0:
        ctx.out.maps_failed = True
        ctx.warn(
            f"{tool} failed (exit {r.returncode}), font maps may be stale:\n{_tail(r.stdout + chr(10) + r.stderr)}"
        )


def _run_scripts(ctx: _Run, batch: Batch) -> None:
    """``TLUtils::_do_postaction_script``; trees with a ``bin`` only (a usertree has no ``tlpkg/tlpostcode``)."""
    for pkg, how, file in batch.scripts:
        path = ctx.root / file
        if not ctx.standalone or not path.is_file():
            continue
        if file.lower().endswith(".pl"):
            cmd = ["perl", str(path)]
        elif file.lower().endswith(".texlua"):
            cmd = ["texlua", str(path)]
        else:
            cmd = [str(path)]
        r = ctx.run([*cmd, how, str(ctx.root)])
        if r is None or r.returncode != 0:
            ctx.warn(f"postaction script {file} of {pkg} did not succeed")


def _lsr_targets(root: Path, standalone: bool) -> list[Path]:
    """Trees that need an ``ls-R``: all ``!!`` entries of a standalone tree's search path."""
    if not standalone:
        return [root]
    return [root / d for d in ("texmf-dist", "texmf-var", "texmf-local")]


def _index(ctx: _Run, trees: list[Path]) -> None:
    """Write the ``ls-R`` files (texi's own ``mktexlsr``); failures become warnings."""
    ctx.log("writing ls-R in " + ", ".join(map(str, trees)))
    for tree in trees:
        try:
            if tree.is_dir():
                lsr.write(tree)
        except lsr.LsrError as e:
            ctx.warn(str(e))


def run(
    env_dir: Path,
    *,
    standalone: bool,
    batch: Batch,
    arch: str | None = None,
    notice: Notice | None = None,
    log: Notice | None = None,
) -> Outcome:
    """Do the actions of a batch of installs and removals once (``handle_execute_actions``).

    Writes the configuration files from ``env_dir/tlpkg/texlive.tlpdb`` (so call
    it after the tlpdb was written), writes the ``ls-R`` files itself, runs ``updmap-user|sys`` and
    one ``fmtutil-user|sys``. Missing programs and failed builds are reported as
    warnings; nothing is raised for them.

    Parameters
    ----------
    env_dir
        The ``.texenv/`` directory.
    standalone
        True for a full tree with ``bin/<arch>``, False for a usertree.
    batch
        What changed.
    arch
        Architecture of a standalone tree (to find its ``bin``).
    notice
        Called with each warning.
    log
        Called with each command run (for ``--debug``).

    Returns
    -------
    Outcome
        Commands run, warnings and formats that failed.
    """
    ctx = _Run(env_dir, standalone, arch, notice, log)
    for p in batch.problems:
        ctx.warn(p)
    if batch.empty and not batch.changed:
        return ctx.out
    if (
        standalone
        and not (
            ctx.root / "texmf-var" / "tex" / "generic" / "config" / "language.def"
        ).is_file()
    ):
        # hyphen-base ships a language.def that lists every language of TeX Live;
        # until ours shadows it, formats fail on the patterns that are not installed
        batch.hyphens = True
    executes = read_executes(ctx.tlpdb)
    language: list[Path] = []
    if batch.maps:
        _update_maps(ctx, executes)
    if batch.hyphens:
        language = _language_files(ctx, executes)
    if batch.formats:
        _update_formats_cnf(ctx, executes)
    select = _select_formats(ctx, batch, executes, language)
    if batch.maps or batch.formats or batch.hyphens or select:
        _index(ctx, _lsr_targets(ctx.root, standalone))
    if batch.maps:
        _run_updmap(ctx)
    if select:
        _build_formats(ctx, select)
    _run_scripts(ctx, batch)
    return ctx.out
