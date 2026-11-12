"""The dependency graph of the packages installed in ``.texenv/``, read from its tlpdb."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from texi import resolver

_ARCH_PLACEHOLDER = ".ARCH"


@dataclass
class Graph:
    """Installed packages and their ``depend`` edges (only between installed packages)."""

    #: ``name -> revision`` of every installed package, binary variants excluded.
    revisions: dict[str, str] = field(default_factory=dict)
    #: ``name -> names it depends on`` (installed ones, sorted).
    deps: dict[str, list[str]] = field(default_factory=dict)
    #: ``name -> names that depend on it`` (installed ones, sorted).
    dependents: dict[str, list[str]] = field(default_factory=dict)

    def roots(self) -> list[str]:
        """Packages that nothing else depends on."""
        return sorted(n for n in self.revisions if not self.dependents.get(n))


@dataclass
class Node:
    """One line of the rendered tree."""

    name: str
    revision: str
    #: True if this package was already shown above (its subtree is not repeated).
    seen: bool = False
    #: True if the subtree was cut by ``--depth``.
    cut: bool = False
    children: list[Node] = field(default_factory=list)


def read(env_dir: Path, arch: str | None = None) -> Graph:
    """Parse ``env_dir/tlpkg/texlive.tlpdb``.

    Packages whose name ends in ``.<arch>`` are the binary halves of another
    package and are left out, as are ``foo.ARCH`` dependencies on them.
    """
    text = (env_dir / "tlpkg" / "texlive.tlpdb").read_text(encoding="utf-8")
    suffix = f".{arch}" if arch else None
    g = Graph()
    raw: dict[str, list[str]] = {}
    for stanza in re.split(r"\n\s*\n", text):
        name = rev = None
        deps: list[str] = []
        for line in stanza.splitlines():
            if line.startswith("name "):
                name = line[5:].strip()
            elif line.startswith("revision "):
                rev = line[9:].strip()
            elif line.startswith("depend ") and name is not None:
                value = line[7:].strip()
                if not resolver.is_pseudo_record(name, value):
                    deps.append(value)
        if name is None or name.startswith("00texlive."):
            continue
        if suffix and name.endswith(suffix):
            continue
        g.revisions[name] = rev or "?"
        raw[name] = deps
    for name, deps in raw.items():
        kept = sorted(
            {d for d in deps if not d.endswith(_ARCH_PLACEHOLDER) and d in g.revisions}
            - {name}
        )
        g.deps[name] = kept
        for d in kept:
            g.dependents.setdefault(d, []).append(name)
    for d in g.dependents:
        g.dependents[d].sort()
    return g


def contract(g: Graph, keep: set[str]) -> Graph:
    """The graph of only the ``keep`` packages.

    An edge ``a -> b`` is kept when ``a`` reaches ``b`` through packages that are
    not in ``keep`` (or directly), so a dependency that runs through a package
    you did not list still shows up as a link between two that you did.
    """
    out = Graph(revisions={n: r for n, r in g.revisions.items() if n in keep})
    for n in out.revisions:
        found: set[str] = set()
        seen: set[str] = set()
        todo = list(g.deps.get(n, []))
        while todo:
            d = todo.pop()
            if d in seen or d == n:
                continue
            seen.add(d)
            if d in keep:
                found.add(d)
            else:
                todo += g.deps.get(d, [])
        out.deps[n] = sorted(found)
        for d in found:
            out.dependents.setdefault(d, []).append(n)
    for d in out.dependents:
        out.dependents[d].sort()
    return out


def closure(g: Graph, roots: list[str]) -> set[str]:
    """The roots and everything they depend on."""
    seen: set[str] = set()
    todo = [r for r in roots if r in g.revisions]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += g.deps.get(n, [])
    return seen


def build(
    g: Graph,
    roots: list[str],
    *,
    depth: int | None = None,
    invert: bool = False,
    only: set[str] | None = None,
) -> list[Node]:
    """The tree below ``roots``: dependencies, or with ``invert`` dependents.

    Parameters
    ----------
    depth
        Levels below a root to show; None for all. 0 shows the roots only.
    only
        Restrict the tree to these names (``--explicit``).
    """
    edges = g.dependents if invert else g.deps
    shown: set[str] = set()

    def visit(name: str, level: int, path: tuple[str, ...]) -> Node:
        node = Node(name, g.revisions.get(name, "?"))
        children = [
            c
            for c in edges.get(name, [])
            if c not in path and (only is None or c in only)
        ]
        if name in shown and children:
            node.seen = True
            return node
        shown.add(name)
        if depth is not None and level >= depth:
            node.cut = bool(children)
            return node
        node.children = [visit(c, level + 1, (*path, name)) for c in children]
        return node

    return [visit(r, 0, ()) for r in roots if r in g.revisions]
