"""Dependency resolution for TeX Live packages, modeled on ``TLPDB::expand_dependencies``.

Two layers, both pure (no I/O):

* :func:`resolve` is the generic closure over a ``name -> dependencies`` mapping.
* :func:`resolve_packages` works on a package index (see :class:`PackageSource`,
  implemented by :class:`texi.tlpdb.Tlpdb`) and understands what TeX Live puts in
  ``depend`` lines: ``name.ARCH`` placeholders, per-architecture binary packages,
  collections and schemes (their ``depend`` lines are their members), and the
  infrastructure packages that are never installed into a project.

The result, a :class:`Resolution`, lists the packages in install order
(dependencies before dependents, ties broken by name, cycles tolerated), what
was skipped and why, which names the index does not know, and the edges that
explain why each package is there.

Pseudo-records
--------------
The tlpdb overloads ``depend``: ``00texlive.*`` entries hold settings and
release info in it (``opt_location:__MASTER__``, ``release/2026``). Those are
never dependency edges; :func:`is_pseudo_record` is the rule.

Scope
-----
Resolution works against exactly one index, that is one repository. Resolving
across several repositories needs package identity and repository selection
before traversal and is not done here.
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

META_CATEGORIES = ("Collection", "Scheme")
INFRA_PREFIXES = ("texlive.infra", "tlpkg", "tlperl", "tlgs", "tlshell")
_METADATA_PREFIX = "00texlive."

REASON_SYSTEM = "installed in the system TeX Live"
REASON_INFRA = "infrastructure"
REASON_FOREIGN = "foreign architecture"
REASON_METADATA = "tlpdb metadata record"

#: Names the system-installed callback is asked about, once per level.
SystemInstalled = Callable[[list[str]], set[str]]


class UnknownPackageError(ValueError):
    """A package is not in the index (or mapping) it is resolved against."""


class PackageInfo(Protocol):
    """What the resolver needs to know about a package."""

    @property
    def category(self) -> str:
        """``Package``, ``Collection``, ``Scheme``, ..."""
        ...

    @property
    def depends(self) -> Sequence[str]:
        """``depend`` values in order (pseudo-records are ignored)."""
        ...


class PackageSource(Protocol):
    """A package index: :class:`texi.tlpdb.Tlpdb`, or a fake in tests."""

    def get(self, name: str) -> PackageInfo | None:
        """The package called ``name``, or None."""
        ...

    def has(self, name: str) -> bool:
        """True if an entry called ``name`` exists."""
        ...


def is_pseudo_record(owner: str, value: str) -> bool:
    """True if a ``depend`` value of package ``owner`` is not a package name.

    The entries ``00texlive.*`` keep settings in ``depend``; elsewhere a value
    with ``:`` or ``/`` (``opt_x:y``, ``release/2026``) is one too, because
    package names never contain either.
    """
    return owner.startswith(_METADATA_PREFIX) or ":" in value or "/" in value


def skip_reason(name: str) -> str | None:
    """Why ``name`` is never installed into a project (infrastructure), or None."""
    return REASON_INFRA if name.startswith(INFRA_PREFIXES) else None


# --- generic closure ----------------------------------------------------------


def resolve(roots: Iterable[str], dependencies: Mapping[str, set[str]]) -> set[str]:
    """Transitive closure of ``roots`` over ``dependencies``.

    Parameters
    ----------
    roots
        Package names to start from.
    dependencies
        ``name -> names it depends on``.

    Returns
    -------
    set[str]
        The roots and everything they depend on, directly or not.

    Raises
    ------
    UnknownPackageError
        A root or a dependency is not a key of ``dependencies``.
    """
    resolved: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in resolved:
            continue
        if name not in dependencies:
            raise UnknownPackageError(name)
        resolved.add(name)
        pending.extend(dependencies[name] - resolved)
    return resolved


# --- package resolution ---------------------------------------------------------


@dataclass
class Resolution:
    """Outcome of :func:`resolve_packages`.

    Parameters
    ----------
    roots
        The requested names, deduplicated, in the order given.
    order
        Install order: dependencies before dependents, ties broken by name.
        Collections and schemes are left out unless they are roots (a root
        collection is installed as a record without files).
    meta
        Collections and schemes that were traversed.
    skipped
        ``name -> reason`` for packages left out: infrastructure, installed in
        the system tree, foreign architecture, or no binary package for the
        selected architecture.
    missing
        ``name -> requirers`` for names the index does not know. A requested
        name that nothing depends on has no requirers.
    edges
        ``name -> dependencies`` between resolved packages (meta included), sorted.
        Its keys are all resolved packages.
    cycles
        Groups of packages that depend on each other, each sorted by name.
    """

    roots: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    meta: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)
    missing: dict[str, set[str]] = field(default_factory=dict)
    edges: dict[str, list[str]] = field(default_factory=dict)
    cycles: list[list[str]] = field(default_factory=list)

    def why(self, name: str) -> list[str]:
        """One path from a root to ``name``, shortest first, ties by name.

        Returns
        -------
        list[str]
            ``[root, ..., name]``; ``[name]`` for a root; empty if ``name`` was not
            reached. A missing name is reached through its first requirer.
        """
        if name in self.missing and name not in self.edges:
            via = sorted(self.missing[name])
            for requirer in via:
                if path := self.why(requirer):
                    return [*path, name]
            return [name] if name in self.roots else []
        parent: dict[str, str | None] = {r: None for r in self.roots if r in self.edges}
        queue = deque(parent)
        while queue:
            cur = queue.popleft()
            if cur == name:
                path = [cur]
                while (up := parent[path[-1]]) is not None:
                    path.append(up)
                return path[::-1]
            for nxt in self.edges.get(cur, ()):
                if nxt not in parent:
                    parent[nxt] = cur
                    queue.append(nxt)
        return []


def _components(
    nodes: Sequence[str], edges: Mapping[str, Sequence[str]]
) -> list[list[str]]:
    """Strongly connected components (iterative Tarjan), deterministic for sorted input."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[str]] = []
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(edges.get(start, ())))]
        index[start] = low[start] = len(index)
        stack.append(start)
        on_stack.add(start)
        while work:
            node, it = work[-1]
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = len(index)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(edges.get(nxt, ()))))
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        comp.append(member)
                        if member == node:
                            break
                    out.append(sorted(comp))
    return out


def install_order(
    nodes: Collection[str], edges: Mapping[str, Sequence[str]]
) -> tuple[list[str], list[list[str]]]:
    """Topological order (dependencies first) and the cycles of a graph.

    Ties are broken by name. Members of a cycle are emitted together, by name,
    so the result is the same however the input was ordered.

    Returns
    -------
    tuple
        ``(order, cycles)``; ``cycles`` holds each group of two or more mutually
        dependent packages.
    """
    ordered = sorted(nodes)
    comps = _components(ordered, {n: sorted(edges.get(n, ())) for n in ordered})
    comp_of = {n: i for i, comp in enumerate(comps) for n in comp}
    needs: list[set[int]] = [set() for _ in comps]
    users: list[set[int]] = [set() for _ in comps]
    for n in ordered:
        for d in edges.get(n, ()):
            if d in comp_of and comp_of[d] != comp_of[n]:
                needs[comp_of[n]].add(comp_of[d])
                users[comp_of[d]].add(comp_of[n])
    heap = [(comp[0], i) for i, comp in enumerate(comps) if not needs[i]]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        _, i = heapq.heappop(heap)
        order.extend(comps[i])
        for u in users[i]:
            needs[u].discard(i)
            if not needs[u]:
                heapq.heappush(heap, (comps[u][0], u))
    cycles = sorted(c for c in comps if len(c) > 1)
    return order, cycles


def resolve_packages(
    db: PackageSource,
    roots: Iterable[str],
    *,
    arch: str | None,
    architectures: Collection[str] = (),
    system_installed: SystemInstalled | None = None,
    strict: bool = False,
    infrastructure: bool = False,
) -> Resolution:
    """Resolve ``roots`` and their dependencies against one package index.

    Each level of the traversal is handled together, so ``system_installed`` is
    called once per level, not once per package. Packages the system tree has
    are neither installed nor followed. Everything else is followed, whether it
    will be installed from the store or left to ``tlmgr``.

    Parameters
    ----------
    db
        The package index.
    roots
        Requested package names. A collection or scheme among them is expanded.
    arch
        Target architecture (``universal-darwin``): ``foo.ARCH`` becomes
        ``foo.<arch>``. None if unknown; ``.ARCH`` dependencies are then skipped.
    architectures
        All valid architectures of the index; an explicit ``foo.<other>`` for a
        member of this collection other than ``arch`` is skipped.
    system_installed
        Reports which of some package names the system TeX Live already has.
    strict
        Raise :class:`UnknownPackageError` for any unknown name instead of
        recording it in :attr:`Resolution.missing`.
    infrastructure
        Keep infrastructure packages (``texlive.infra``, ...). A standalone tree
        needs them: ``fmtutil`` and ``updmap`` load ``tlpkg/TeXLive/*.pm``.

    Returns
    -------
    Resolution
        Install order, skipped and missing packages, and edges.

    Raises
    ------
    UnknownPackageError
        With ``strict``, a root or dependency is not in the index.
    """
    root_list = list(dict.fromkeys(roots))
    others = set(architectures) - {arch}
    res = Resolution(roots=root_list)
    requirers: dict[str, set[str]] = {}
    deps: dict[str, list[str]] = {}
    seen: set[str] = set()
    frontier = root_list

    while frontier:
        level = [n for n in dict.fromkeys(frontier) if n not in seen]
        seen.update(level)
        found: dict[str, PackageInfo] = {}
        for n in level:
            if n.startswith(_METADATA_PREFIX):
                if n in root_list:
                    res.skipped[n] = REASON_METADATA
            elif not infrastructure and (reason := skip_reason(n)):
                res.skipped[n] = reason
            elif "." in n and n.rpartition(".")[2] in others:
                res.skipped[n] = REASON_FOREIGN
            elif (pkg := db.get(n)) is None:
                if strict:
                    raise UnknownPackageError(_unknown(n, requirers.get(n)))
                res.missing[n] = set()
            else:
                found[n] = pkg
        real = [n for n, p in found.items() if p.category not in META_CATEGORIES]
        system = system_installed(real) if system_installed and real else set()
        frontier = []
        for n, pkg in found.items():
            if n in system:
                res.skipped[n] = REASON_SYSTEM
                continue
            if pkg.category in META_CATEGORIES:
                res.meta.add(n)
            mine: dict[str, None] = {}
            for d in pkg.depends:
                if is_pseudo_record(n, d):
                    continue
                if d.endswith(".ARCH"):
                    d = _expand_arch(db, d, arch, res)
                    if d is None:
                        continue
                if d == n or d.startswith(_METADATA_PREFIX):
                    continue
                requirers.setdefault(d, set()).add(n)
                mine[d] = None
            deps[n] = list(mine)
            frontier += mine

    for n in res.missing:
        res.missing[n] = requirers.get(n, set())
    included = set(deps)
    res.edges = {
        n: sorted(d for d in ds if d in included) for n, ds in sorted(deps.items())
    }
    topo, res.cycles = install_order(included, res.edges)
    res.order = [n for n in topo if n not in res.meta or n in root_list]
    return res


def _expand_arch(
    db: PackageSource, dep: str, arch: str | None, res: Resolution
) -> str | None:
    """``foo.ARCH`` as ``foo.<arch>``; None (and a ``skipped`` entry) if it does not exist."""
    base = dep.removesuffix(".ARCH")
    if not arch:
        res.skipped[dep] = "architecture unknown"
        return None
    name = f"{base}.{arch}"
    if not db.has(name):
        res.skipped[name] = f"no {arch} binary package in the index"
        return None
    return name


def _unknown(name: str, requirers: Collection[str] | None) -> str:
    if requirers:
        return f"{name} (needed by {', '.join(sorted(requirers))})"
    return name
