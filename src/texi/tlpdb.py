"""Parse, fetch, cache and query the TeX Live package database (tlpdb).

The tlpdb is a text file of blank-line-separated entries made of ``key value``
lines. File lists are sections (``runfiles size=N``) followed by indented path
lines. A full copy lives at ``<repository>/tlpkg/texlive.tlpdb.xz``.

Queries run against an SQLite index built from that text (see
:data:`SCHEMA_VERSION`). Remote databases are cached as the downloaded ``.xz``
plus the index, so cached runs never parse the text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import lzma
import os
import re
import sqlite3
import subprocess
import urllib.error
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from texi.resolver import is_pseudo_record

DEFAULT_REPOSITORY = "https://mirror.ctan.org/systems/texlive/tlnet"
USER_AGENT = "texi (+https://github.com/claudiosv/texi)"
CACHE_MAX_AGE = 24 * 3600
TIMEOUT = 20.0

_XZ_MAGIC = b"\xfd7zXZ\x00"
_ATTR_RE = re.compile(r'\s+(\w+)=("[^"]*"|\S+)')
_SECTION_KINDS = ("runfiles", "docfiles", "srcfiles", "binfiles")


class TlpdbError(Exception):
    """The database could not be loaded."""


@dataclass
class TlFile:
    """One file listed in a package.

    Parameters
    ----------
    path
        Path as written in the tlpdb (may start with ``RELOC/``).
    attrs
        Trailing ``key=value`` attributes (``details``, ``language``).
    """

    path: str
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def full_path(self) -> str:
        """Path with ``RELOC/`` expanded to ``texmf-dist/``."""
        return expand_reloc(self.path)

    @property
    def basename(self) -> str:
        """File name without directories."""
        return self.path.rsplit("/", 1)[-1]


def expand_reloc(path: str) -> str:
    """Expand a leading ``RELOC/`` to ``texmf-dist/``."""
    if path.startswith("RELOC/"):
        return "texmf-dist/" + path[len("RELOC/") :]
    return path


@dataclass
class TlPackage:
    """A tlpdb entry.

    Parameters
    ----------
    name
        TeX Live package name.
    category
        Package, Collection, Scheme, TLCore, ...
    revision
        TeX Live revision number.
    shortdesc
        One-line description.
    longdesc
        Long description with the repeated lines joined by spaces.
    catalogue_key
        Value of the ``catalogue`` key; empty when absent.
    depends
        Real package dependencies (``depend`` values) in order; ``name.ARCH``
        placeholders are kept as written.
    settings
        Pseudo-records TeX Live also stores in ``depend`` lines, verbatim and
        in order (``opt_location:__MASTER__``, ``release/2026``). Only
        ``00texlive.*`` entries have them in practice.
    fields
        Every other key verbatim (values of repeated keys in order).
    files
        Sections ``runfiles``, ``docfiles``, ``srcfiles`` and
        ``binfiles:<arch>`` mapped to their files.
    sizes
        Declared ``size=`` per section (summed if a section repeats).
    """

    name: str
    category: str = ""
    revision: str = ""
    shortdesc: str = ""
    longdesc: str = ""
    catalogue_key: str = ""
    depends: list[str] = field(default_factory=list)
    settings: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, list[TlFile]] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    @property
    def catalogue(self) -> str:
        """CTAN catalogue key; the package name when none is given."""
        return self.catalogue_key or self.name

    def first(self, key: str) -> str | None:
        """First value of an extra key, or None."""
        vals = self.fields.get(key)
        return vals[0] if vals else None

    def all_files(self) -> list[TlFile]:
        """All files of all sections."""
        return [f for fs in self.files.values() for f in fs]

    def to_dict(self) -> dict[str, object]:
        """Machine-readable form with every field."""
        return {
            "name": self.name,
            "category": self.category,
            "revision": self.revision,
            "shortdesc": self.shortdesc,
            "longdesc": self.longdesc,
            "catalogue": self.catalogue,
            "catalogue_explicit": bool(self.catalogue_key),
            "depends": list(self.depends),
            "settings": list(self.settings),
            "fields": {k: list(v) for k, v in self.fields.items()},
            "sizes": dict(self.sizes),
            "files": {
                sec: [
                    {"path": f.full_path, **({"attrs": f.attrs} if f.attrs else {})}
                    for f in fs
                ]
                for sec, fs in self.files.items()
            },
        }


@dataclass
class SearchHit:
    """A search result.

    Parameters
    ----------
    package
        The matching package (a summary without files, fields or dependencies).
    score
        Rank score; higher is better.
    matched_on
        Best matching field: name, catalogue, shortdesc, longdesc or file.
    files
        Matching file paths (only when files were searched).
    """

    package: TlPackage
    score: int
    matched_on: str
    files: list[str] = field(default_factory=list)


@dataclass
class TlpdbStats:
    """Summary figures of a database."""

    entries: int
    categories: dict[str, int]
    files: dict[str, int]
    with_binfiles: int
    architectures: list[str]
    release: str | None
    minrelease: str | None
    frozen: str | None
    revision: str | None


#: Version of the SQLite layout; a mismatch rebuilds the index from the cached ``.xz``.
SCHEMA_VERSION = 2
#: Set to False to build indexes without the FTS5 table (searches then use LIKE).
_FTS_ENABLED = True

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE pkg(
    name TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    revision TEXT NOT NULL,
    catalogue TEXT NOT NULL,
    shortdesc TEXT NOT NULL,
    longdesc TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    layout_json TEXT NOT NULL
);
CREATE TABLE depend(pkg TEXT NOT NULL, dep TEXT NOT NULL);
CREATE TABLE setting(pkg TEXT NOT NULL, line TEXT NOT NULL);
CREATE TABLE file(
    pkg TEXT NOT NULL,
    section TEXT NOT NULL,
    path TEXT NOT NULL,
    base TEXT NOT NULL,
    attrs_json TEXT NOT NULL
);
"""
_INDEXES = """
CREATE INDEX file_base ON file(base);
CREATE INDEX file_pkg ON file(pkg);
CREATE INDEX pkg_catalogue ON pkg(catalogue);
CREATE INDEX depend_pkg ON depend(pkg);
CREATE INDEX setting_pkg ON setting(pkg);
"""
_FTS = (
    "CREATE VIRTUAL TABLE pkg_fts USING fts5("
    "name, catalogue, shortdesc, longdesc, content='pkg', content_rowid='rowid', "
    "tokenize='trigram')"
)
#: Full path of a file row: ``RELOC/`` expanded to ``texmf-dist/``.
_FULL_PATH = (
    "CASE WHEN path LIKE 'RELOC/%' THEN 'texmf-dist/' || substr(path, 7) ELSE path END"
)
_PKG_COLS = "name, category, revision, catalogue, shortdesc, longdesc"
_CHUNK = 500


def _like(term: str) -> str:
    """LIKE pattern matching ``term`` anywhere (escape char is a backslash)."""
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


@lru_cache(maxsize=32)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and _compiled(pattern).search(value) is not None


def _summary(row: tuple[str, ...]) -> TlPackage:
    """A package without files, fields or dependencies from a ``pkg`` row."""
    name, category, revision, catalogue, shortdesc, longdesc = row[:6]
    return TlPackage(
        name=name,
        category=category,
        revision=revision,
        shortdesc=shortdesc,
        longdesc=longdesc,
        catalogue_key="" if catalogue == name else catalogue,
    )


class _PackageMap(Mapping[str, TlPackage]):
    """Read-only ``name -> TlPackage`` view that loads entries on demand."""

    def __init__(self, db: Tlpdb) -> None:
        self._db = db

    def __getitem__(self, name: str) -> TlPackage:
        pkg = self._db.get(name)
        if pkg is None:
            raise KeyError(name)
        return pkg

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._db.has(name)

    def __iter__(self) -> Iterator[str]:
        return iter(self._db.names())

    def __len__(self) -> int:
        return self._db.count()


class Tlpdb:
    """A package database backed by an SQLite connection.

    Rows are turned into :class:`TlPackage` objects only when asked for; the
    fast lookups (:meth:`packages_for_files`, :meth:`by_catalogue`,
    :meth:`shortdesc`, ...) never build them.

    Parameters
    ----------
    conn
        Connection to an index created by this module.
    index_path
        File the index lives in; None for an in-memory index.

    Attributes
    ----------
    source
        URI or path the data was read from (final URI after redirects).
    fetched
        When the data was fetched (or the file's mtime).
    size_bytes
        Unpacked size of the text database.
    compressed_bytes
        Size of the ``.xz`` file, or None for a plain file.
    repository
        Repository the user asked for (before redirects), if remote.
    cache_path
        Path of the cached ``.xz`` when loaded from a remote repository.
    from_cache
        True when served from the cache without a fresh download.
    offline
        True when the cache was used because the network failed.
    previous_revisions
        After a fresh download: ``name -> revision`` of the index it replaced, or None.
    stale_mirror
        True when every download was older than the cached index, so the cached
        one was kept.
    changed
        After a fresh download: False if it is byte-identical to the copy it
        replaced, True if not, None if there was no earlier copy (or no download).
    """

    def revisions(self) -> dict[str, int]:
        """``name -> revision`` of every package that has a numeric revision."""
        return {
            n: int(r)
            for n, r in self._conn.execute("SELECT name, revision FROM pkg")
            if str(r).isdigit()
        }

    def __init__(self, conn: sqlite3.Connection, index_path: Path | None = None):
        self._conn = conn
        self.index_path = index_path
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        self.source: str = meta.get("source", "")
        self.fetched: datetime | None = None
        if meta.get("fetched"):
            self.fetched = datetime.fromtimestamp(float(meta["fetched"]), tz=UTC)
        self.size_bytes: int = int(meta.get("unpacked_bytes") or 0)
        comp = meta.get("compressed_bytes")
        self.compressed_bytes: int | None = int(comp) if comp else None
        self.repository: str | None = meta.get("repository") or None
        self.cache_path: Path | None = None
        self.from_cache = False
        self.offline = False
        self.changed: bool | None = None
        self.stale_mirror = False
        self.previous_revisions: dict[str, int] | None = None
        self._cache: dict[str, TlPackage] = {}
        self._has_fts = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'pkg_fts'"
            ).fetchone()
            is not None
        )
        conn.create_function("regexp", 2, _regexp, deterministic=True)
        #: ``packages[name]`` loads a package on demand.
        self.packages: Mapping[str, TlPackage] = _PackageMap(self)

    # -- index facts ----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """``PRAGMA user_version`` of the index."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    @property
    def index_size(self) -> int | None:
        """Size of the index file in bytes; None for an in-memory index."""
        try:
            return self.index_path.stat().st_size if self.index_path else None
        except OSError:
            return None

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    # -- lookups --------------------------------------------------------------

    def count(self) -> int:
        """Number of entries."""
        return int(self._conn.execute("SELECT count(*) FROM pkg").fetchone()[0])

    def names(self) -> list[str]:
        """All entry names in database order."""
        return [r[0] for r in self._conn.execute("SELECT name FROM pkg ORDER BY rowid")]

    def has(self, name: str) -> bool:
        """True if an entry called ``name`` exists."""
        return (
            self._conn.execute("SELECT 1 FROM pkg WHERE name = ?", (name,)).fetchone()
            is not None
        )

    def get(self, name: str) -> TlPackage | None:
        """Package by exact name, with fields, dependencies and files."""
        if name in self._cache:
            return self._cache[name]
        row = self._conn.execute(
            f"SELECT {_PKG_COLS}, fields_json, layout_json FROM pkg WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        pkg = self._materialize(row)
        self._cache[name] = pkg
        return pkg

    def _materialize(self, row: tuple[str, ...]) -> TlPackage:
        pkg = _summary(row)
        pkg.fields = json.loads(row[6])
        layout = json.loads(row[7])
        pkg.sizes = layout["sizes"]
        pkg.catalogue_key = row[3] if layout.get("explicit") else ""
        pkg.files = {sec: [] for sec in layout["sections"]}
        pkg.depends = [
            r[0]
            for r in self._conn.execute(
                "SELECT dep FROM depend WHERE pkg = ? ORDER BY rowid", (pkg.name,)
            )
        ]
        pkg.settings = [
            r[0]
            for r in self._conn.execute(
                "SELECT line FROM setting WHERE pkg = ? ORDER BY rowid", (pkg.name,)
            )
        ]
        for sec, path, attrs in self._conn.execute(
            "SELECT section, path, attrs_json FROM file WHERE pkg = ? ORDER BY rowid",
            (pkg.name,),
        ):
            pkg.files.setdefault(sec, []).append(
                TlFile(path, json.loads(attrs) if attrs else {})
            )
        return pkg

    def by_catalogue(self, key: str) -> list[TlPackage]:
        """Packages whose CTAN catalogue key is ``key`` (case-sensitive)."""
        rows = self._conn.execute(
            f"SELECT {_PKG_COLS}, fields_json, layout_json FROM pkg "
            "WHERE catalogue = ? ORDER BY rowid",
            (key,),
        ).fetchall()
        return [self._cache.get(r[0]) or self._materialize(r) for r in rows]

    def names_by_catalogue(self, key: str) -> list[str]:
        """Names of the packages whose catalogue key is ``key`` (no materializing)."""
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM pkg WHERE catalogue = ? ORDER BY rowid", (key,)
            )
        ]

    def catalogue_keys(self) -> list[tuple[str, str]]:
        """``(catalogue key, package name)`` of every package with its own key."""
        return [
            (r[0], r[1])
            for r in self._conn.execute(
                "SELECT catalogue, name FROM pkg WHERE catalogue != name ORDER BY rowid"
            )
        ]

    def names_by_catalogue_nocase(self, key: str) -> list[str]:
        """Like :meth:`names_by_catalogue` but ASCII case-insensitive."""
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM pkg WHERE catalogue = ? COLLATE NOCASE ORDER BY rowid",
                (key,),
            )
        ]

    def packages_for_file(self, basename: str, section: str | None = None) -> list[str]:
        """Names of the packages that list a file called ``basename``."""
        return self.packages_for_files([basename], section).get(basename, [])

    def packages_for_files(
        self, basenames: Iterable[str], section: str | None = None
    ) -> dict[str, list[str]]:
        """Map each of ``basenames`` to the packages listing it (absent if none).

        Parameters
        ----------
        basenames
            File names without directories (case-sensitive).
        section
            Only files of this section (for example ``runfiles``).
        """
        wanted = list(dict.fromkeys(basenames))
        out: dict[str, list[str]] = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i : i + _CHUNK]
            sql = (
                "SELECT base, pkg FROM file WHERE base IN "
                f"({','.join('?' * len(chunk))})"
            )
            params: list[str] = list(chunk)
            if section:
                sql += " AND section = ?"
                params.append(section)
            for base, pkg in self._conn.execute(sql + " ORDER BY rowid", params):
                names = out.setdefault(base, [])
                if pkg not in names:
                    names.append(pkg)
        return out

    def shortdesc(self, names: Iterable[str]) -> dict[str, str]:
        """Short descriptions of the known ``names`` (unknown ones are left out)."""
        return {n: d for n, d, _ in self._summaries(names)}

    def categories(self, names: Iterable[str]) -> dict[str, str]:
        """Categories of the known ``names`` (unknown ones are left out)."""
        return {n: c for n, _, c in self._summaries(names)}

    def _summaries(self, names: Iterable[str]) -> list[tuple[str, str, str]]:
        wanted = list(dict.fromkeys(names))
        rows: list[tuple[str, str, str]] = []
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i : i + _CHUNK]
            rows.extend(
                self._conn.execute(
                    "SELECT name, shortdesc, category FROM pkg WHERE name IN "
                    f"({','.join('?' * len(chunk))})",
                    chunk,
                )
            )
        return rows

    # -- search ---------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        regex: bool = False,
        in_name: bool = True,
        in_desc: bool = True,
        in_files: bool = False,
        category: str | None = None,
    ) -> list[SearchHit]:
        """Search packages, best match first.

        Ranking: exact name, name prefix, name substring, shortdesc, longdesc,
        file. Without ``regex`` the query is split on whitespace and every
        term must match somewhere (case-insensitive); with ``regex`` the whole
        query is one case-insensitive pattern.

        Parameters
        ----------
        query
            Text or regular expression.
        regex
            Treat the query as a regular expression.
        in_name
            Match names (and catalogue keys).
        in_desc
            Match short and long descriptions.
        in_files
            Match file paths.
        category
            Only packages of this category (case-insensitive).

        Returns
        -------
        list[SearchHit]
            Hits sorted by descending score, then name. Each hit's package is a
            summary without files, fields or dependencies; use :meth:`get` for
            the full entry.

        Raises
        ------
        TlpdbError
            If ``regex`` is set and the pattern is invalid.
        """
        if regex:
            try:
                pats = [re.compile(query, re.IGNORECASE)]
            except re.error as e:
                msg = f"invalid regular expression: {e}"
                raise TlpdbError(msg) from e
            terms: list[str] = []
        else:
            terms = query.split()
            pats = [re.compile(re.escape(t), re.IGNORECASE) for t in terms]
        if not pats:
            return []

        where: list[str] = []
        params: list[str] = []
        if category:
            where.append("lower(category) = ?")
            params.append(category.lower())
        if not in_files:
            # A package must contain every term in a searched text field, so the
            # index can narrow the rows; Python scoring below decides for real.
            for t in terms:
                if self._has_fts and len(t) >= 3:
                    where.append(
                        "rowid IN (SELECT rowid FROM pkg_fts WHERE pkg_fts MATCH ?)"
                    )
                    params.append('"' + t.replace('"', '""') + '"')
                else:
                    cols = ("name", "catalogue", "shortdesc", "longdesc")
                    where.append(
                        "(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")"
                    )
                    params.extend([_like(t)] * len(cols))
        sql = f"SELECT {_PKG_COLS} FROM pkg"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._conn.execute(sql + " ORDER BY rowid", params).fetchall()

        file_hits: list[dict[str, list[str]]] = []
        if in_files:
            file_hits = [
                self._file_matches(query if regex else terms[i], regex)
                for i in range(len(pats))
            ]

        hits: list[SearchHit] = []
        for row in rows:
            pkg = _summary(row)
            total = 0
            best = ("", 0)
            files: list[str] = []
            for i, pat in enumerate(pats):
                matched = file_hits[i].get(pkg.name, []) if in_files else []
                score, fld = self._score(pkg, pat, in_name, in_desc, matched)
                if score == 0:
                    total = 0
                    break
                total += score
                files.extend(m for m in matched if m not in files)
                if score > best[1]:
                    best = (fld, score)
            if total:
                hits.append(SearchHit(pkg, total, best[0], files))
        hits.sort(key=lambda h: (-h.score, h.package.name))
        return hits

    def _file_matches(self, term: str, regex: bool) -> dict[str, list[str]]:
        """Full paths matching one term (or the regex), grouped by package."""
        if regex:
            sql = f"SELECT pkg, {_FULL_PATH} FROM file WHERE {_FULL_PATH} REGEXP ?"
            arg = term
        else:
            sql = (
                f"SELECT pkg, {_FULL_PATH} FROM file "
                f"WHERE {_FULL_PATH} LIKE ? ESCAPE '\\'"
            )
            arg = _like(term)
        out: dict[str, list[str]] = {}
        for pkg, full in self._conn.execute(sql + " ORDER BY rowid", (arg,)):
            out.setdefault(pkg, []).append(full)
        return out

    @staticmethod
    def _score(
        p: TlPackage,
        pat: re.Pattern[str],
        in_name: bool,
        in_desc: bool,
        matched: list[str],
    ) -> tuple[int, str]:
        """Best (score, field) of one pattern against a package."""
        score, fld = 0, ""
        if in_name:
            for value, label in ((p.name, "name"), (p.catalogue_key, "catalogue")):
                if not value:
                    continue
                m = pat.search(value)
                if not m:
                    continue
                if pat.fullmatch(value):
                    s = 100 if label == "name" else 90
                elif m.start() == 0:
                    s = 80 if label == "name" else 70
                else:
                    s = 60 if label == "name" else 50
                if s > score:
                    score, fld = s, label
        if in_desc and score < 40 and pat.search(p.shortdesc):
            score, fld = 40, "shortdesc"
        if in_desc and score < 20 and pat.search(p.longdesc):
            score, fld = 20, "longdesc"
        if matched:
            exact = any(pat.fullmatch(m.rsplit("/", 1)[-1]) for m in matched)
            s = 15 if exact else 10
            if s > score:
                score, fld = s, "file"
        return score, fld

    # -- settings -------------------------------------------------------------

    def settings(self, name: str = "00texlive.config") -> dict[str, str]:
        """Pseudo-records of an entry as ``key -> value``.

        ``opt_location:__MASTER__`` gives ``opt_location``; ``release/2026``
        gives ``release``. Only the first ``:`` or ``/`` splits.
        """
        out: dict[str, str] = {}
        for (line,) in self._conn.execute(
            "SELECT line FROM setting WHERE pkg = ? ORDER BY rowid", (name,)
        ):
            key, value = split_setting(line)
            out[key] = value
        return out

    def available_architectures(self) -> list[str]:
        """Architectures the repository has binaries for.

        From ``setting_available_architectures`` in ``00texlive.config``; if that
        is missing, from the ``binfiles`` sections.
        """
        listed = self.settings().get("setting_available_architectures", "").split()
        if listed:
            return sorted(listed)
        return sorted({
            sec.partition(":")[2]
            for (sec,) in self._conn.execute(
                "SELECT DISTINCT section FROM file WHERE section LIKE 'binfiles:%'"
            )
        })

    # -- stats ----------------------------------------------------------------

    def stats(self) -> TlpdbStats:
        """Entry, category and file counts plus release info from ``00texlive.config``."""
        q = self._conn.execute
        cats = dict(q("SELECT category, count(*) FROM pkg GROUP BY category"))
        files: Counter[str] = Counter()
        archs: set[str] = set()
        for sec, n in q("SELECT section, count(*) FROM file GROUP BY section"):
            kind, _, arch = sec.partition(":")
            files[kind] += n
            if kind == "binfiles":
                archs.add(arch)
        with_bin = q(
            "SELECT count(DISTINCT pkg) FROM file WHERE section LIKE 'binfiles:%'"
        ).fetchone()[0]
        info = self.settings()
        return TlpdbStats(
            entries=self.count(),
            categories=dict(sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))),
            files={k: files.get(k, 0) for k in _SECTION_KINDS},
            with_binfiles=with_bin,
            architectures=sorted(archs),
            release=info.get("release"),
            minrelease=info.get("minrelease"),
            frozen=info.get("frozen"),
            revision=info.get("revision"),
        )


_SETTING_RE = re.compile(r"([^:/]*)[:/](.*)", re.DOTALL)


def split_setting(line: str) -> tuple[str, str]:
    """``(key, value)`` of a pseudo-record; the key is the text before the first ``:`` or ``/``."""
    m = _SETTING_RE.fullmatch(line)
    return (m[1], m[2]) if m else (line, "")


def _parse_file_line(line: str) -> TlFile:
    """Parse `` path key=value ...``."""
    body = line.strip()
    m = _ATTR_RE.search(body)
    if not m:
        return TlFile(body)
    # attributes run to the end of the line; path is everything before the first one
    tail = body[m.start() :]
    attrs = {k: v.strip('"') for k, v in _ATTR_RE.findall(tail)}
    return TlFile(body[: m.start()], attrs)


def _parse_entry(lines: list[str]) -> TlPackage | None:
    """Parse the lines of one blank-line-separated entry."""
    pkg = TlPackage(name="")
    longdesc: list[str] = []
    section: str | None = None
    for line in lines:
        if line[0] in " \t":
            if section is not None and line.strip():
                pkg.files[section].append(_parse_file_line(line))
            continue
        key, _, value = line.partition(" ")
        section = None
        if key in _SECTION_KINDS:
            attrs = dict(_ATTR_RE.findall(" " + value))
            sec = key
            if key == "binfiles":
                sec = f"binfiles:{attrs.get('arch', '').strip()}"
            pkg.files.setdefault(sec, [])
            size = attrs.get("size")
            if size and size.isdigit():
                pkg.sizes[sec] = pkg.sizes.get(sec, 0) + int(size)
            section = sec
        elif key == "name":
            pkg.name = value
        elif key == "category":
            pkg.category = value
        elif key == "revision":
            pkg.revision = value
        elif key == "shortdesc":
            pkg.shortdesc = value
        elif key == "longdesc":
            longdesc.append(value)
        elif key == "catalogue":
            pkg.catalogue_key = value
        elif key == "depend":
            pkg.depends.append(value)
        else:
            pkg.fields.setdefault(key, []).append(value)
    pkg.longdesc = " ".join(longdesc)
    real = [d for d in pkg.depends if not is_pseudo_record(pkg.name, d)]
    if len(real) != len(pkg.depends):
        pkg.settings = [d for d in pkg.depends if is_pseudo_record(pkg.name, d)]
        pkg.depends = real
    return pkg if pkg.name else None


def _parse_packages(text: str) -> dict[str, TlPackage]:
    """Parse tlpdb text into entries by name (a repeated name keeps the last)."""
    packages: dict[str, TlPackage] = {}
    block: list[str] = []

    def flush() -> None:
        if block:
            pkg = _parse_entry(block)
            if pkg:
                packages[pkg.name] = pkg
            block.clear()

    for line in text.splitlines():
        if line.strip():
            block.append(line.rstrip())
        else:
            flush()
    flush()
    return packages


def _populate(
    conn: sqlite3.Connection, packages: dict[str, TlPackage], meta: dict[str, str]
) -> None:
    """Create the schema in ``conn`` and fill it."""
    conn.executescript(_SCHEMA)
    with conn:
        conn.executemany(
            "INSERT INTO pkg VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    p.name,
                    p.category,
                    p.revision,
                    p.catalogue,
                    p.shortdesc,
                    p.longdesc,
                    json.dumps(p.fields, ensure_ascii=False),
                    json.dumps({
                        "sections": list(p.files),
                        "sizes": p.sizes,
                        "explicit": bool(p.catalogue_key),
                    }),
                )
                for p in packages.values()
            ),
        )
        conn.executemany(
            "INSERT INTO depend VALUES (?, ?)",
            ((p.name, d) for p in packages.values() for d in p.depends),
        )
        conn.executemany(
            "INSERT INTO setting VALUES (?, ?)",
            ((p.name, s) for p in packages.values() for s in p.settings),
        )
        conn.executemany(
            "INSERT INTO file VALUES (?, ?, ?, ?, ?)",
            (
                (
                    p.name,
                    sec,
                    f.path,
                    f.basename,
                    json.dumps(f.attrs, ensure_ascii=False) if f.attrs else "",
                )
                for p in packages.values()
                for sec, fs in p.files.items()
                for f in fs
            ),
        )
    conn.executescript(_INDEXES)
    fts = "0"
    if _FTS_ENABLED:
        try:
            conn.execute(_FTS)
            conn.execute("INSERT INTO pkg_fts(pkg_fts) VALUES ('rebuild')")
            fts = "1"
        except sqlite3.OperationalError:  # no FTS5 or trigram tokenizer
            conn.execute("DROP TABLE IF EXISTS pkg_fts")
    with conn:
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [*meta.items(), ("schema", str(SCHEMA_VERSION)), ("fts", fts)],
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def parse_tlpdb(text: str) -> Tlpdb:
    """Parse tlpdb text into an in-memory index.

    Parameters
    ----------
    text
        Contents of ``texlive.tlpdb``.

    Returns
    -------
    Tlpdb
        Database with ``size_bytes`` set; ``source`` is empty.
    """
    conn = sqlite3.connect(":memory:")
    _populate(
        conn,
        _parse_packages(text),
        {"unpacked_bytes": str(len(text.encode("utf-8")))},
    )
    return Tlpdb(conn)


def _decode(data: bytes) -> tuple[str, int | None]:
    """Decompress if xz; returns text and the compressed size (None if plain)."""
    if data.startswith(_XZ_MAGIC):
        try:
            raw = lzma.decompress(data)
        except lzma.LZMAError as e:
            msg = f"corrupt xz data: {e}"
            raise TlpdbError(msg) from e
        return raw.decode("utf-8", errors="replace"), len(data)
    return data.decode("utf-8", errors="replace"), None


def load_local(path: Path | str) -> Tlpdb:
    """Load a local tlpdb file (plain or ``.xz``) or a repository directory.

    The text is parsed into an in-memory index on every call.

    Raises
    ------
    TlpdbError
        If the file is missing or unreadable.
    """
    p = Path(path).expanduser()
    if p.is_dir():
        for cand in ("tlpkg/texlive.tlpdb", "tlpkg/texlive.tlpdb.xz", "texlive.tlpdb"):
            if (p / cand).is_file():
                p /= cand
                break
    if not p.is_file():
        msg = f"tlpdb file not found: {p}"
        raise TlpdbError(msg)
    try:
        data = p.read_bytes()
    except OSError as e:
        msg = f"cannot read {p}: {e}"
        raise TlpdbError(msg) from e
    text, comp = _decode(data)
    db = parse_tlpdb(text)
    db.source = str(p.resolve())
    db.fetched = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
    db.compressed_bytes = comp
    return db


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/texi/tlpdb`` (default ``~/.cache/texi/tlpdb``)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "texi" / "tlpdb"


def normalize_repository(repo: str) -> str:
    """Map the ``ctan``/``tlnet`` aliases to the mirror URL; strip a trailing slash."""
    repo = repo.strip()
    if repo.lower() in {"ctan", "tlnet"}:
        return DEFAULT_REPOSITORY
    return repo.rstrip("/")


def _http_repository(value: str) -> str | None:
    """A usable repository URL from a tlmgr/tlpdb option value, else None."""
    if value.lower() in {"ctan", "tlnet"}:
        return DEFAULT_REPOSITORY
    if value.startswith(("http://", "https://")):
        return value.rstrip("/")
    return None


# --- the system TeX Live (read from its tlpdb; never through tlmgr) --------------


@dataclass(frozen=True)
class SystemTexLive:
    """What the system's ``tlpkg/texlive.tlpdb`` says: installed names and settings."""

    root: Path
    path: Path
    installed: frozenset[str]
    settings: dict[str, str]
    revisions: dict[str, int]

    @property
    def release(self) -> str | None:
        """The release year (``release/2026`` in ``00texlive.config``)."""
        return self.settings.get("release")

    @property
    def location(self) -> str | None:
        """``opt_location`` as an http(s) URL; None for ``__MASTER__`` or a path."""
        value = self.settings.get("opt_location", "")
        if value.startswith("http://mirror.ctan.org"):  # the installer's default
            value = "https" + value[4:]
        return _http_repository(value)

    @property
    def architectures(self) -> list[str]:
        """Installed architectures (``setting_available_architectures``)."""
        return self.settings.get("setting_available_architectures", "").split()


_UNSET: Any = object()
_root_memo: Any = _UNSET
_system_memo: dict[tuple[str, int, int], SystemTexLive] = {}


def reset_system_cache() -> None:
    """Forget the TEXMFROOT and parsed system tlpdb (for tests)."""
    global _root_memo
    _root_memo = _UNSET
    _system_memo.clear()


def texlive_root() -> Path | None:
    """The system TeX Live root, from ``kpsewhich -var-value TEXMFROOT``."""
    global _root_memo
    if _root_memo is _UNSET:
        root: Path | None = None
        try:
            out = subprocess.run(
                ["kpsewhich", "-var-value=TEXMFROOT"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            root = Path(out) if out else None
        except OSError, subprocess.SubprocessError:
            pass
        _root_memo = root
    return _root_memo


def read_system_tlpdb(path: Path) -> SystemTexLive:
    """Scan an installed tlpdb for package names and the ``00texlive.*`` settings."""
    installed: set[str] = set()
    settings: dict[str, str] = {}
    revisions: dict[str, int] = {}
    current = ""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("name "):
                current = line[5:].strip()
                installed.add(current)
            elif line.startswith("revision ") and line[9:].strip().isdigit():
                revisions[current] = int(line[9:])
            elif current.startswith("00texlive.") and line.startswith("depend "):
                key, value = split_setting(line[7:].strip())
                settings[key] = value
    return SystemTexLive(
        path.parent.parent, path, frozenset(installed), settings, revisions
    )


def load_local_installed(root: Path | None = None) -> SystemTexLive | None:
    """The system TeX Live's installed-package view, cached by path and mtime.

    Returns None when there is no ``<root>/tlpkg/texlive.tlpdb``.
    """
    root = root or texlive_root()
    if root is None:
        return None
    path = root / "tlpkg" / "texlive.tlpdb"
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _system_memo:
        try:
            _system_memo[key] = read_system_tlpdb(path)
        except OSError:
            return None
    return _system_memo[key]


def system_installed(names: Iterable[str]) -> set[str]:
    """Which of ``names`` the system tlpdb lists as installed (empty if unreadable)."""
    info = load_local_installed()
    return set(names) & info.installed if info else set()


# TeX Live architecture names by (sys.platform prefix, machine)
_ARCH_BY_MACHINE: dict[str, dict[str, str]] = {
    "linux": {
        "x86_64": "x86_64-linux",
        "amd64": "x86_64-linux",
        "aarch64": "aarch64-linux",
        "arm64": "aarch64-linux",
        "armv7l": "armhf-linux",
        "armv6l": "armhf-linux",
        "i386": "i386-linux",
        "i686": "i386-linux",
    },
    "freebsd": {
        "amd64": "amd64-freebsd",
        "x86_64": "amd64-freebsd",
        "i386": "i386-freebsd",
    },
    "netbsd": {
        "amd64": "amd64-netbsd",
        "x86_64": "amd64-netbsd",
        "i386": "i386-netbsd",
    },
    "cygwin": {"x86_64": "x86_64-cygwin", "amd64": "x86_64-cygwin"},
}


def _is_musl() -> bool:
    return any(Path("/lib").glob("ld-musl-*"))


def platform_arch(
    platform: str | None = None, machine: str | None = None, musl: bool | None = None
) -> str | None:
    """TeX Live architecture name for this machine, from the platform alone.

    macOS and Windows have one architecture each; unknown combinations give None.
    """
    import platform as _platform
    import sys

    platform = sys.platform if platform is None else platform
    machine = (_platform.machine() if machine is None else machine).lower()
    if platform == "darwin":
        return "universal-darwin"
    if platform in {"win32", "windows"}:
        return "windows"
    for prefix, table in _ARCH_BY_MACHINE.items():
        if platform.startswith(prefix):
            arch = table.get(machine)
            if arch == "x86_64-linux" and (_is_musl() if musl is None else musl):
                return "x86_64-linuxmusl"
            return arch
    return None


def system_arch() -> str | None:
    """The platform's architecture, checked against the system tlpdb's list.

    If the system tree has no binaries for it but exactly one architecture
    installed, that one wins.
    """
    arch = platform_arch()
    info = load_local_installed()
    listed = info.architectures if info else []
    if listed and arch not in listed and len(listed) == 1:
        return listed[0]
    return arch


def _project_config(project_dir: Path | None) -> Any:
    """``[project]`` of the texproject.toml in ``project_dir`` (default: the cwd), or None."""
    cfg = (project_dir or Path.cwd()) / "texproject.toml"
    if not cfg.is_file():
        return None
    import typer

    from texi.cli import load_project_config

    try:
        return load_project_config(cfg).project
    except typer.Exit:
        return None


def resolve_repository(
    repository: str | None, project_dir: Path | None = None
) -> tuple[str, str]:
    """Pick the repository; returns it and where the choice came from.

    Order: the ``repository`` argument, ``[project] repository`` of the
    texproject.toml in ``project_dir`` (default: the cwd), the system tlpdb's
    ``opt_location``, then :data:`DEFAULT_REPOSITORY`.
    """
    if repository:
        return normalize_repository(repository), "--repository"
    project = _project_config(project_dir)
    if project is not None and project.repository:
        return normalize_repository(project.repository), "texproject.toml"
    info = load_local_installed()
    if info is not None and info.location:
        return info.location, "system tlpdb"
    return DEFAULT_REPOSITORY, "default"


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(meta), encoding="utf-8")


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _write_index(
    dest: Path,
    text: str,
    comp: int | None,
    repo: str,
    meta: dict[str, Any],
    fetched: float,
) -> None:
    """Build the SQLite index of ``text`` and move it to ``dest`` atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    values = {
        "source": str(meta.get("url") or f"{repo}/tlpkg/texlive.tlpdb.xz"),
        "repository": repo,
        "fetched": str(fetched),
        "etag": str(meta.get("etag") or ""),
        "last_modified": str(meta.get("last_modified") or ""),
        "compressed_bytes": "" if comp is None else str(comp),
        "unpacked_bytes": str(len(text.encode("utf-8"))),
    }
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        _populate(conn, _parse_packages(text), values)
        conn.close()
        Path(tmp).replace(dest)
    except (sqlite3.Error, OSError) as e:
        conn.close()
        _unlink(tmp)
        msg = f"cannot build the index {dest}: {e}"
        raise TlpdbError(msg) from e


def _open_index(path: Path) -> Tlpdb:
    """Open an index read-only; raises sqlite3.DatabaseError if unusable."""
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            msg = f"schema version {version}"
            raise sqlite3.DatabaseError(msg)
        return Tlpdb(conn, path)
    except sqlite3.Error:
        conn.close()
        raise


#: Extra downloads when a mirror serves an index older than the cached one.
STALE_RETRIES = 3
_REVISION_LINE = re.compile(r"^revision (\d+)$", re.MULTILINE)


def _newest_revision(text: str) -> int:
    """The highest package revision in tlpdb text: dates the index (SVN revisions)."""
    return max((int(m) for m in _REVISION_LINE.findall(text)), default=0)


def has_cache(repository: str) -> bool:
    """True if an index or a downloaded copy of ``repository`` is cached."""
    key = hashlib.sha1(normalize_repository(repository).encode()).hexdigest()
    return any((cache_dir() / f"{key}.{ext}").is_file() for ext in ("sqlite", "xz"))


def _read_body(resp: Any, progress: Callable[[int, int], None] | None) -> bytes:
    """Read a response, reporting ``(bytes_done, bytes_total)`` (total 0 if unknown)."""
    if progress is None:
        return resp.read()
    total = int(resp.headers.get("Content-Length") or 0)
    chunks: list[bytes] = []
    done = 0
    progress(0, total)
    while chunk := resp.read(1 << 16):
        chunks.append(chunk)
        done += len(chunk)
        progress(done, total)
    return b"".join(chunks)


def load_remote(
    repository: str,
    *,
    refresh: bool = False,
    max_age: float = CACHE_MAX_AGE,
    progress: Callable[[int, int], None] | None = None,
) -> Tlpdb:
    """Load ``<repository>/tlpkg/texlive.tlpdb.xz`` through the cache.

    The cache (``<cache_dir>/<sha1(repo)>``) holds the downloaded ``.xz``, a
    ``.json`` sidecar (ETag, Last-Modified, fetch time, final URL) and the
    ``.sqlite`` index built from them. The index is used without parsing the
    text; a missing, corrupt or outdated (schema version) index is rebuilt from
    the ``.xz`` without network access. A copy younger than ``max_age`` seconds
    is used without a request (unless ``refresh``), an older one is revalidated
    with a conditional GET, and used as a fallback when the network fails.

    Raises
    ------
    TlpdbError
        If the download fails and no cache exists.
    """
    repo = normalize_repository(repository)
    key = hashlib.sha1(repo.encode()).hexdigest()
    cdir = cache_dir()
    xz = cdir / f"{key}.xz"
    meta_path = cdir / f"{key}.json"
    db_path = cdir / f"{key}.sqlite"
    meta: dict[str, Any] = {}
    now = datetime.now(UTC).timestamp()

    def fetched_ts() -> float:
        for p in (xz, db_path):
            if p.is_file():
                return float(meta.get("fetched", p.stat().st_mtime))
        return 0.0

    def open_cached(*, from_cache: bool, offline: bool = False) -> Tlpdb | None:
        db: Tlpdb | None = None
        if db_path.is_file():
            try:
                db = _open_index(db_path)
            except sqlite3.DatabaseError:
                _unlink(db_path)
        if db is None and xz.is_file():
            try:
                text, comp = _decode(xz.read_bytes())
            except TlpdbError, OSError:
                _unlink(xz)
                return None
            _write_index(db_path, text, comp, repo, meta, fetched_ts())
            try:
                db = _open_index(db_path)
            except sqlite3.Error as e:
                _unlink(db_path)
                msg_0 = f"cannot open the index {db_path}: {e}"
                raise TlpdbError(msg_0) from e
        if db is None:
            return None
        db.repository = repo
        db.cache_path = xz
        db.source = str(
            meta.get("url") or db.source or f"{repo}/tlpkg/texlive.tlpdb.xz"
        )
        db.fetched = datetime.fromtimestamp(fetched_ts(), tz=UTC)
        db.from_cache = from_cache
        db.offline = offline
        return db

    have_cache = xz.is_file() or db_path.is_file()
    if have_cache:
        meta = _read_meta(meta_path)
    if have_cache and not refresh and now - fetched_ts() < max_age:
        db = open_cached(from_cache=True)
        if db:
            return db
        meta = {}

    url = f"{repo}/tlpkg/texlive.tlpdb.xz"
    headers = {"User-Agent": USER_AGENT}
    if have_cache:
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

    def from_stale(**kw: bool) -> Tlpdb | None:
        return open_cached(from_cache=True, **kw) if have_cache else None

    try:
        with urlopen(Request(url, headers=headers), timeout=TIMEOUT) as resp:
            body = _read_body(resp, progress)
            final = resp.geturl() if hasattr(resp, "geturl") else url
            etag = resp.headers.get("ETag")
            last_mod = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304 and have_cache:
            meta["fetched"] = now
            _write_meta(meta_path, meta)
            db = from_stale()
            if db:
                return db
        else:
            db = from_stale(offline=True)
            if db:
                return db
        msg = f"cannot download {url}: HTTP {e.code} {e.reason}"
        raise TlpdbError(msg) from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        db = from_stale(offline=True)
        if db:
            return db
        reason = getattr(e, "reason", e)
        msg = (
            f"cannot download {url}: {reason}. You seem to be offline and there is "
            "no cached copy; use --local PATH or try again with a connection."
        )
        raise TlpdbError(msg) from e

    text, comp = _decode(body)  # validate before replacing the cache
    previous: bytes | None = None
    old_revisions: dict[str, int] | None = None
    try:
        previous = xz.read_bytes() if xz.is_file() else None
        if db_path.is_file():
            old = _open_index(db_path)
            try:
                old_revisions = old.revisions()
            finally:
                old._conn.close()
    except OSError, sqlite3.Error:
        pass
    # The redirector may hand us a mirror that is behind the copy we already have.
    # Ask again (another mirror answers); if all are older keep the cached index.
    old_top = max(old_revisions.values(), default=0) if old_revisions else 0
    for _ in range(STALE_RETRIES):
        if _newest_revision(text) >= old_top:
            break
        try:
            with urlopen(
                Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT
            ) as r2:
                body2 = _read_body(r2, progress)
                final2 = r2.geturl() if hasattr(r2, "geturl") else url
                etag, last_mod = r2.headers.get("ETag"), r2.headers.get("Last-Modified")
            text2, comp2 = _decode(body2)
        except urllib.error.URLError, OSError, TimeoutError, TlpdbError:
            break
        body, text, comp, final = body2, text2, comp2, final2
    if old_top and _newest_revision(text) < old_top:
        kept = open_cached(from_cache=True)
        if kept is not None:
            kept.stale_mirror = True
            return kept
    cdir.mkdir(parents=True, exist_ok=True)
    tmp = xz.with_suffix(".xz.tmp")
    tmp.write_bytes(body)
    tmp.replace(xz)
    meta = {"url": final, "fetched": now}
    if etag:
        meta["etag"] = etag
    if last_mod:
        meta["last_modified"] = last_mod
    _write_meta(meta_path, meta)
    _write_index(db_path, text, comp, repo, meta, now)
    db = open_cached(from_cache=False)
    if db is None:
        msg = f"cannot open the index {db_path}"
        raise TlpdbError(msg)
    db.changed = None if previous is None else previous != body
    db.previous_revisions = old_revisions
    return db


def load(
    source: str | Path | None = None,
    *,
    local: Path | str | None = None,
    refresh: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> Tlpdb:
    """Load a database from a repository URL or a local file.

    Parameters
    ----------
    source
        Repository URL (``http(s)://``, ``file://``, or an alias), or a local
        path when ``local`` is not given and it exists on disk.
    local
        Local tlpdb file (plain or ``.xz``); takes precedence.
    refresh
        Ignore the cache age for remote repositories.
    progress
        Called with ``(bytes_done, bytes_total)`` while a remote index downloads.
    """
    if local is not None:
        return load_local(local)
    if source is None:
        source = DEFAULT_REPOSITORY
    s = str(source)
    if not re.match(r"^(https?|file)://", s) and s.lower() not in {"ctan", "tlnet"}:
        return load_local(s)
    return load_remote(s, refresh=refresh, progress=progress)
