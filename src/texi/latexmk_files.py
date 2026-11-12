"""Parse the files latexmk leaves behind: ``.fls`` and ``.fdb_latexmk``.

``latexmk -recorder`` makes TeX write a *file list* (``<jobname>.fls``) with
one ``INPUT``/``OUTPUT`` line per file it opened. latexmk itself keeps a
*database* (``<jobname>.fdb_latexmk``) with one block per rule (pdflatex,
bibtex, biber, ...) listing every dependency with its mtime, size and MD5.

All parsers here take text and never raise on malformed or truncated input;
they keep whatever they could read. Only the ``load_*`` helpers touch the
disk and can raise :class:`OSError`.

Examples
--------
>>> fls = parse_fls("PWD /p\\nINPUT main.tex\\nINPUT ./main.tex\\nOUTPUT main.pdf\\n")
>>> fls.inputs
['main.tex']
>>> fls.outputs
['main.pdf']
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Dep",
    "Fdb",
    "Fls",
    "ProjectFiles",
    "Rule",
    "classify_path",
    "is_texmf_path",
    "load_fdb",
    "load_fls",
    "parse_fdb",
    "parse_fls",
    "tree_used_paths",
]


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _normalize(path: str, pwd: str | None) -> str:
    """Absolute-if-possible, ``.``/``..``-free form of ``path``, without touching the disk."""
    if not path:
        return path
    if not os.path.isabs(path) and pwd:
        path = os.path.join(pwd, path)
    return os.path.normpath(path)


_KIND_BY_SUFFIX = {
    ".tex": "tex",
    ".ltx": "tex",
    ".dtx": "tex",
    ".cls": "class",
    ".clo": "class",
    ".sty": "package",
    ".def": "package",
    ".cfg": "config",
    ".cnf": "config",
    ".fmt": "format",
    ".bib": "bib",
    ".bst": "bib",
    ".bbl": "bib",
    ".bcf": "bib",
    ".blg": "bib",
    ".aux": "aux",
    ".toc": "aux",
    ".lof": "aux",
    ".lot": "aux",
    ".out": "aux",
    ".nav": "aux",
    ".snm": "aux",
    ".idx": "aux",
    ".ind": "aux",
    ".ilg": "aux",
    ".glo": "aux",
    ".gls": "aux",
    ".glg": "aux",
    ".acn": "aux",
    ".acr": "aux",
    ".alg": "aux",
    ".xdv": "output",
    ".dvi": "output",
    ".ps": "output",
    ".pdf": "output",
    ".log": "log",
    ".fls": "log",
    ".fdb_latexmk": "log",
    ".tfm": "font",
    ".pfb": "font",
    ".pfa": "font",
    ".otf": "font",
    ".ttf": "font",
    ".ttc": "font",
    ".vf": "font",
    ".map": "font",
    ".enc": "font",
    ".fd": "font",
    ".png": "graphic",
    ".jpg": "graphic",
    ".jpeg": "graphic",
    ".eps": "graphic",
    ".svg": "graphic",
    ".mps": "graphic",
    ".lua": "lua",
}


def classify_path(path: str) -> str:
    """Guess what kind of file ``path`` is from its extension.

    Parameters
    ----------
    path : str
        File path, absolute or relative.

    Returns
    -------
    str
        One of ``"tex"``, ``"class"``, ``"package"``, ``"config"``,
        ``"format"``, ``"bib"``, ``"aux"``, ``"output"``, ``"log"``,
        ``"font"``, ``"graphic"``, ``"lua"`` or ``"other"``.
    """
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".fdb_latexmk"):
        return "log"
    dot = name.rfind(".")
    if dot < 0:
        return "other"
    return _KIND_BY_SUFFIX.get(name[dot:], "other")


def is_texmf_path(path: str) -> bool:
    """Tell whether ``path`` lives in a TeX distribution tree.

    A path counts as texmf when one of its directories starts with ``texmf``
    (``texmf-dist``, ``texmf-var``, ``texmf-local``) or is named ``texlive``.

    Parameters
    ----------
    path : str
        File path.

    Returns
    -------
    bool
        ``True`` for files of the TeX distribution.
    """
    parts = path.replace("\\", "/").split("/")[:-1]
    return any(p.startswith("texmf") or p == "texlive" for p in parts)


def _is_within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root if root == "/" else root + "/")


@dataclass
class Fls:
    """Parsed ``.fls`` recorder file.

    Parameters
    ----------
    pwd : str or None
        Directory TeX ran in (the ``PWD`` line); relative paths are relative
        to it. ``None`` when the line is missing.
    inputs : list of str
        Paths of ``INPUT`` lines as written, deduplicated (``./a`` and ``a``
        count as the same file) in first-seen order.
    outputs : list of str
        Same for ``OUTPUT`` lines.
    """

    pwd: str | None = None
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    def resolve(self, path: str) -> str:
        """Return ``path`` made absolute against :attr:`pwd` and normalized (no disk access)."""
        return _normalize(path, self.pwd)

    def resolved_inputs(self) -> list[str]:
        """Return :attr:`inputs` made absolute and normalized."""
        return [self.resolve(p) for p in self.inputs]

    def resolved_outputs(self) -> list[str]:
        """Return :attr:`outputs` made absolute and normalized."""
        return [self.resolve(p) for p in self.outputs]

    def inputs_in(self, root: str | Path) -> list[str]:
        """Return resolved inputs located under ``root``.

        Parameters
        ----------
        root : str or Path
            Directory to look in. Relative values are taken against :attr:`pwd`.
        """
        r = self.resolve(os.fspath(root))
        return [p for p in self.resolved_inputs() if _is_within(p, r)]

    def project_inputs(self, root: str | Path | None = None) -> list[str]:
        """Return resolved inputs that are project sources.

        These are inputs under ``root`` that are neither texmf files nor
        files the same run wrote (see :meth:`read_then_written`).

        Parameters
        ----------
        root : str or Path, optional
            Project directory; defaults to :attr:`pwd`.
        """
        base = os.fspath(root) if root is not None else self.pwd
        if not base:
            return []
        written = set(self.resolved_outputs())
        return [
            p for p in self.inputs_in(base) if p not in written and not is_texmf_path(p)
        ]

    def external_inputs(self, root: str | Path | None = None) -> list[str]:
        """Return resolved inputs outside ``root`` (packages, classes, fonts, config)."""
        base = os.fspath(root) if root is not None else self.pwd
        inside = set(self.inputs_in(base)) if base else set()
        return [p for p in self.resolved_inputs() if p not in inside]

    def read_then_written(self) -> list[str]:
        """Return resolved paths that appear as both ``INPUT`` and ``OUTPUT`` (``.aux``, ``.toc``, ...)."""
        written = set(self.resolved_outputs())
        return [p for p in self.resolved_inputs() if p in written]

    def inputs_of_kind(self, kind: str) -> list[str]:
        """Return inputs (as written) whose :func:`classify_path` kind is ``kind``."""
        return [p for p in self.inputs if classify_path(p) == kind]


def parse_fls(text: str) -> Fls:
    """Parse the text of a ``.fls`` file.

    Unknown or malformed lines are skipped. Paths keep inner and trailing
    spaces; only the line ending is removed. The first ``PWD`` line wins.

    Parameters
    ----------
    text : str
        Content of the file.

    Returns
    -------
    Fls
        Parsed result; empty for empty or garbage input.
    """
    fls = Fls()
    seen_in: set[str] = set()
    seen_out: set[str] = set()
    for line in text.splitlines():
        keyword, _, rest = line.partition(" ")
        if not rest:
            continue
        if keyword == "PWD":
            if fls.pwd is None:
                fls.pwd = rest
        elif keyword == "INPUT":
            key = _normalize(rest, fls.pwd)
            if key not in seen_in:
                seen_in.add(key)
                fls.inputs.append(rest)
        elif keyword == "OUTPUT":
            key = _normalize(rest, fls.pwd)
            if key not in seen_out:
                seen_out.add(key)
                fls.outputs.append(rest)
    return fls


def load_fls(path: str | Path) -> Fls:
    """Read and parse a ``.fls`` file (decoded as UTF-8, bad bytes replaced).

    Raises
    ------
    OSError
        If the file cannot be read.
    """
    return parse_fls(_decode(Path(path).read_bytes()))


def tree_used_paths(
    fls_path: str | Path, base: str | Path | None = None
) -> tuple[set[Path], set[Path]]:
    """Return the files and folders a build read, as resolved paths.

    Replacement for the inline ``.fls`` loop in ``texi tree``. Differences:
    relative paths are resolved against the ``PWD`` line (or ``base`` when
    given) instead of the process's current directory, and paths with
    trailing spaces survive.

    Parameters
    ----------
    fls_path : str or Path
        The ``.fls`` file.
    base : str or Path, optional
        Directory for relative paths; overrides ``PWD``.

    Returns
    -------
    tuple of set of Path
        ``(files, folders)``, where folders are the parents of the files.

    Raises
    ------
    OSError
        If the file cannot be read.
    """
    fls = load_fls(fls_path)
    if base is not None:
        fls.pwd = os.fspath(base)
    files: set[Path] = set()
    folders: set[Path] = set()
    for raw in fls.inputs:
        p = Path(fls.resolve(raw)).resolve()
        files.add(p)
        folders.add(p.parent)
    return files, folders


# --------------------------------------------------------------------------
# fdb
# --------------------------------------------------------------------------

_TOKEN = re.compile(r'"([^"]*)"?|(\S+)')
_RULE_HEAD = re.compile(r'^\["([^"]*)"?\]?(.*)$')
_VERSION = re.compile(r"^#\s*Fdb version\s+(\d+)", re.IGNORECASE)


def _tokens(line: str) -> list[str]:
    """Split on whitespace; ``"..."`` groups may hold spaces (an unclosed quote runs to the end)."""
    return [
        m.group(1) if m.group(1) is not None else m.group(2)
        for m in _TOKEN.finditer(line)
    ]


def _num(tok: str | None) -> float | None:
    if tok is None:
        return None
    try:
        return float(tok)
    except ValueError:
        return None


def _int(tok: str | None) -> int | None:
    if tok is None:
        return None
    try:
        return int(tok)
    except ValueError:
        f = _num(tok)
        return int(f) if f is not None and f == f and abs(f) != float("inf") else None


@dataclass
class Dep:
    """One file line inside a rule block of a ``.fdb_latexmk``.

    Parameters
    ----------
    path : str
        Path as latexmk recorded it (relative to the latexmk working directory
        or absolute).
    mtime : float or None
        Modification time in seconds since the epoch; ``0`` for files that did
        not exist; ``None`` for generated entries and truncated lines.
    size : int or None
        Size in bytes; ``-1`` when the file did not exist; ``None`` if absent.
    md5 : str or None
        Hex digest; ``None`` when latexmk wrote ``0`` or nothing.
    from_rule : str
        Name of the rule that produced the file (``""`` for hand-written sources).
    generated : bool
        ``True`` when the line is below the ``(generated)`` marker, that is the
        rule writes this file.
    """

    path: str
    mtime: float | None = None
    size: int | None = None
    md5: str | None = None
    from_rule: str = ""
    generated: bool = False

    @property
    def exists(self) -> bool:
        """``False`` when latexmk recorded the file as missing (size ``-1``)."""
        return self.size is None or self.size >= 0

    def same_content_as(self, other: Dep) -> bool:
        """Compare mtime, size and md5 (fields missing on both sides count as equal)."""
        return (
            self.mtime == other.mtime
            and self.size == other.size
            and self.md5 == other.md5
        )


@dataclass
class Rule:
    """One rule block of a ``.fdb_latexmk``.

    Parameters
    ----------
    name : str
        Rule name, for example ``"pdflatex"`` or ``"bibtex main"``.
    run_time : float or None
        First number of the header: time of the last run of the rule.
    source : str
        Source file of the rule.
    dest : str
        Destination file of the rule.
    base : str
        Base name of the rule's files.
    check_time : float or None
        Time latexmk last checked the rule.
    status : int or None
        Last field of the header (latexmk's run status code; ``0`` is success).
    extra : list of str
        Header fields beyond the known ones, kept for forward compatibility.
    deps : list of Dep
        Dependencies, then the files the rule generated (``Dep.generated``).
    rewritten : list of str
        Paths listed under ``(rewritten before read)``.
    """

    name: str
    run_time: float | None = None
    source: str = ""
    dest: str = ""
    base: str = ""
    check_time: float | None = None
    status: int | None = None
    extra: list[str] = field(default_factory=list)
    deps: list[Dep] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)

    @property
    def inputs(self) -> list[Dep]:
        """Dependencies that are read, not generated."""
        return [d for d in self.deps if not d.generated]

    @property
    def generated(self) -> list[Dep]:
        """Files the rule writes."""
        return [d for d in self.deps if d.generated]


@dataclass
class Fdb:
    """Parsed ``.fdb_latexmk`` database.

    Parameters
    ----------
    version : int or None
        Number from the ``# Fdb version N`` header; ``None`` when absent.
        Versions other than 4 are parsed with the same best-effort rules.
    rules : list of Rule
        Rule blocks in file order.
    """

    version: int | None = None
    rules: list[Rule] = field(default_factory=list)

    def rule(self, name: str) -> Rule | None:
        """Return the first rule called ``name``, or ``None``."""
        return next((r for r in self.rules if r.name == name), None)

    def all_deps(self, include_generated: bool = False) -> list[Dep]:
        """Return deduplicated deps over all rules, first-seen order.

        Parameters
        ----------
        include_generated : bool, default False
            Also return files that rules write.
        """
        seen: set[str] = set()
        out: list[Dep] = []
        for r in self.rules:
            for d in r.deps:
                if d.generated and not include_generated:
                    continue
                if d.path not in seen:
                    seen.add(d.path)
                    out.append(d)
        return out

    def generated_files(self) -> list[str]:
        """Return paths any rule generates, deduplicated, first-seen order."""
        seen: dict[str, None] = {}
        for r in self.rules:
            for d in r.generated:
                seen.setdefault(d.path)
        return list(seen)

    def changed_since(self, other: Fdb) -> list[Dep]:
        """Return deps of this database that differ from the older ``other``.

        A dep counts as changed when ``other`` lacks it, or its mtime, size or
        md5 differ. Generated entries are ignored.

        Parameters
        ----------
        other : Fdb
            Earlier database to compare against.
        """
        old = {d.path: d for d in other.all_deps()}
        return [
            d
            for d in self.all_deps()
            if d.path not in old or not d.same_content_as(old[d.path])
        ]

    def removed_since(self, other: Fdb) -> list[Dep]:
        """Return deps of ``other`` that this database no longer lists."""
        mine = {d.path for d in self.all_deps()}
        return [d for d in other.all_deps() if d.path not in mine]


def _parse_rule_head(m: re.Match[str]) -> Rule:
    toks = _tokens(m.group(2))
    rule = Rule(name=m.group(1))
    rule.run_time = _num(toks[0]) if len(toks) > 0 else None
    rule.source = toks[1] if len(toks) > 1 else ""
    rule.dest = toks[2] if len(toks) > 2 else ""
    rule.base = toks[3] if len(toks) > 3 else ""
    rule.check_time = _num(toks[4]) if len(toks) > 4 else None
    rule.status = _int(toks[5]) if len(toks) > 5 else None
    rule.extra = toks[6:]
    return rule


def _parse_dep(toks: list[str], generated: bool) -> Dep:
    md5 = toks[3] if len(toks) > 3 else None
    if md5 in (None, "", "0"):
        md5 = None
    return Dep(
        path=toks[0],
        mtime=_num(toks[1]) if len(toks) > 1 else None,
        size=_int(toks[2]) if len(toks) > 2 else None,
        md5=md5,
        from_rule=toks[4] if len(toks) > 4 else "",
        generated=generated,
    )


def parse_fdb(text: str) -> Fdb:
    """Parse the text of a ``.fdb_latexmk`` file.

    Tolerates CRLF, blank lines, truncation, other format versions, extra
    header fields and unknown ``(...)`` marker lines (those switch to an
    unknown section whose files are ignored). Lines before the first rule are
    ignored.

    Parameters
    ----------
    text : str
        Content of the file.

    Returns
    -------
    Fdb
        Parsed result; empty for empty or garbage input.
    """
    fdb = Fdb()
    current: Rule | None = None
    section = "deps"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _VERSION.match(line)
            if m and fdb.version is None:
                fdb.version = int(m.group(1))
            continue
        head = _RULE_HEAD.match(line) if line.startswith("[") else None
        if head:
            current = _parse_rule_head(head)
            fdb.rules.append(current)
            section = "deps"
            continue
        if current is None:
            continue
        if line.startswith("("):
            marker = line.strip("() ").lower()
            if marker == "generated":
                section = "generated"
            elif marker == "rewritten before read":
                section = "rewritten"
            else:
                section = "unknown"
            continue
        toks = _tokens(line)
        if not toks:
            continue
        if section == "deps":
            current.deps.append(_parse_dep(toks, False))
        elif section == "generated":
            current.deps.append(_parse_dep(toks, True))
        elif section == "rewritten":
            current.rewritten.append(toks[0])
    return fdb


def load_fdb(path: str | Path) -> Fdb:
    """Read and parse a ``.fdb_latexmk`` file (UTF-8, bad bytes replaced).

    Raises
    ------
    OSError
        If the file cannot be read.
    """
    return parse_fdb(_decode(Path(path).read_bytes()))


# --------------------------------------------------------------------------
# combined view
# --------------------------------------------------------------------------


@dataclass
class ProjectFiles:
    """The ``.fls`` and ``.fdb_latexmk`` of one build, side by side.

    Parameters
    ----------
    aux_dir : Path
        Directory holding the two files.
    jobname : str
        Job name (the files are ``<jobname>.fls`` and ``<jobname>.fdb_latexmk``).
    fls : Fls or None
        Parsed file list, ``None`` if the file is missing or unreadable.
    fdb : Fdb or None
        Parsed database, ``None`` if the file is missing or unreadable.
    """

    aux_dir: Path
    jobname: str
    fls: Fls | None = None
    fdb: Fdb | None = None

    @classmethod
    def from_build(cls, aux_dir: str | Path, jobname: str) -> ProjectFiles:
        """Load ``<jobname>.fls`` and ``<jobname>.fdb_latexmk`` from ``aux_dir``.

        Missing or unreadable files leave the matching field ``None``.
        """
        aux = Path(aux_dir)
        pf = cls(aux_dir=aux, jobname=jobname)
        try:
            pf.fls = load_fls(aux / f"{jobname}.fls")
        except OSError:
            pass
        try:
            pf.fdb = load_fdb(aux / f"{jobname}.fdb_latexmk")
        except OSError:
            pass
        return pf

    @property
    def pwd(self) -> str | None:
        """Directory latexmk ran in, from the ``.fls`` (``None`` without one)."""
        return self.fls.pwd if self.fls else None

    def _root(self, root: str | Path | None) -> str:
        if root is not None:
            return _normalize(os.fspath(root), self.pwd)
        return self.pwd or os.fspath(self.aux_dir.resolve())

    def _all_inputs(self) -> list[str]:
        """Resolved inputs from both files, first-seen order, fls first."""
        out: dict[str, None] = {}
        pwd = self.pwd
        if self.fls:
            for p in self.fls.resolved_inputs():
                out.setdefault(p)
        if self.fdb:
            for r in self.fdb.rules:
                for d in r.inputs:
                    if d.exists:
                        out.setdefault(_normalize(d.path, pwd))
        return list(out)

    def _generated(self) -> set[str]:
        gen: set[str] = set()
        pwd = self.pwd
        if self.fls:
            gen.update(self.fls.resolved_outputs())
        if self.fdb:
            for r in self.fdb.rules:
                gen.update(_normalize(d.path, pwd) for d in r.generated)
                gen.update(_normalize(p, pwd) for p in r.rewritten)
        return gen

    def source_files(self, root: str | Path | None = None) -> list[Path]:
        """Return project-local files the document used.

        Files under ``root`` that are not texmf files and not generated by
        the build (so ``main.aux`` and ``main.bbl`` are left out).

        Parameters
        ----------
        root : str or Path, optional
            Project directory; defaults to the ``.fls`` ``PWD`` or ``aux_dir``.
        """
        r = self._root(root)
        gen = self._generated()
        return [
            Path(p)
            for p in self._all_inputs()
            if _is_within(p, r) and p not in gen and not is_texmf_path(p)
        ]

    def external_files(self, root: str | Path | None = None) -> list[Path]:
        """Return used files outside the project: packages, classes, fonts, config.

        Parameters
        ----------
        root : str or Path, optional
            Project directory; same default as :meth:`source_files`.
        """
        r = self._root(root)
        gen = self._generated()
        return [
            Path(p)
            for p in self._all_inputs()
            if p not in gen and (not _is_within(p, r) or is_texmf_path(p))
        ]

    def generated_files(self) -> list[Path]:
        """Return files the build wrote, resolved, from both sources."""
        out: dict[str, None] = {}
        pwd = self.pwd
        if self.fls:
            for p in self.fls.resolved_outputs():
                out.setdefault(p)
        if self.fdb:
            for p in self.fdb.generated_files():
                out.setdefault(_normalize(p, pwd))
        return [Path(p) for p in out]
