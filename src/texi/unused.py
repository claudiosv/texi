r"""Which packages in texproject.toml does the project not use?

Evidence that a package is used:

* a file of the package was read by the last build (``.fls`` / ``.fdb_latexmk`` inputs
  and the file paths in the TeX ``.log``, which also lists fonts that were embedded);
* a ``\\usepackage`` / ``\\RequirePackage`` / ``\\documentclass`` in the sources resolves to it.

A package is also needed when a used package depends on it. Collections and schemes
count as used when they supply a used package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from texi import deptree

#: Tools that leave no file in the build record; their packages are kept if the tool ran.
ALWAYS_USED = frozenset({"latexmk"})

_LOG_PATH = r"[^\s()<>{}\[\]\"]+"


@dataclass
class Report:
    """Where each dependency in texproject.toml stands."""

    #: Used directly (a file was read, or the sources name it).
    used: list[str] = field(default_factory=list)
    #: Collections and schemes that supply something used: ``name -> a used member``.
    via_collection: dict[str, str] = field(default_factory=dict)
    #: Not used, but a used package depends on it: ``name -> the packages that need it``.
    needed: dict[str, list[str]] = field(default_factory=dict)
    #: No evidence at all: likely safe to remove.
    unused: list[str] = field(default_factory=list)
    #: In texproject.toml but not installed in ``.texenv/``.
    not_installed: list[str] = field(default_factory=list)


def file_owners(env_dir: Path) -> dict[str, str]:
    """``path relative to .texenv/ -> package`` for every runfile in the tree's tlpdb."""
    owners: dict[str, str] = {}
    text = (env_dir / "tlpkg" / "texlive.tlpdb").read_text(encoding="utf-8")
    for stanza in re.split(r"\n\s*\n", text):
        name = None
        in_run = False
        for line in stanza.splitlines():
            if line.startswith("name "):
                name = line[5:].strip()
            elif line.startswith(" "):
                if in_run and name:
                    owners.setdefault(line.split()[0], name)
            else:
                in_run = line.startswith("runfiles")
    return owners


def log_paths(text: str, roots: list[str]) -> set[str]:
    """Absolute paths under any of ``roots`` that a TeX ``.log`` mentions."""
    found: set[str] = set()
    for root in roots:
        found.update(
            m.group(0).rstrip(".,;:")
            for m in re.finditer(re.escape(root.rstrip("/")) + "/" + _LOG_PATH, text)
        )
    return found


def owners_of(paths: set[str], env_dir: Path, owners: dict[str, str]) -> set[str]:
    """The packages that own the files in ``paths`` (paths outside ``env_dir`` are ignored)."""
    roots = {str(env_dir), str(env_dir.resolve())}
    used: set[str] = set()
    for p in paths:
        for root in roots:
            if p.startswith(root + "/"):
                pkg = owners.get(p[len(root) + 1 :])
                if pkg:
                    used.add(pkg)
                break
    return used


def analyze(
    g: deptree.Graph,
    dependencies: list[str],
    used: set[str],
    categories: dict[str, str] | None = None,
) -> Report:
    """Sort ``dependencies`` into used, needed, collection and unused.

    Parameters
    ----------
    g
        The installed graph (:func:`texi.deptree.read`).
    dependencies
        ``dependencies`` of texproject.toml.
    used
        Packages with evidence of use.
    categories
        ``name -> category`` (``Collection``/``Scheme`` are handled as collections).
    """
    categories = categories or {}
    used = {u for u in used | ALWAYS_USED if u in g.revisions}
    needed_by: dict[str, set[str]] = {}
    for u in used:
        for d in deptree.closure(g, [u]) - {u}:
            needed_by.setdefault(d, set()).add(u)
    rep = Report()
    for name in dict.fromkeys(dependencies):
        if name not in g.revisions:
            rep.not_installed.append(name)
        elif name in used:
            rep.used.append(name)
        elif categories.get(name) in {"Collection", "Scheme"}:
            members = sorted((deptree.closure(g, [name]) - {name}) & used)
            if members:
                rep.via_collection[name] = members[0]
            else:
                rep.unused.append(name)
        elif name in needed_by:
            rep.needed[name] = sorted(needed_by[name])
        else:
            rep.unused.append(name)
    return rep
