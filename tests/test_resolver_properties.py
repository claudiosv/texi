"""Property tests of dependency resolution, checked against brute force."""

from dataclasses import dataclass
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from texi import resolver

NAMES = ["a", "b", "c", "d", "e", "f"]
nodes_st = st.sets(st.sampled_from(NAMES))
edges_st = st.dictionaries(
    st.sampled_from(NAMES), st.lists(st.sampled_from([*NAMES, "zz"]), max_size=4)
)


def reach(start: str, edges: dict[str, list[str]], allowed: set[str]) -> set[str]:
    """Nodes reachable from ``start`` (itself included) by a fixed point."""
    seen = {start}
    while True:
        more = {d for n in seen for d in edges.get(n, ()) if d in allowed} - seen
        if not more:
            return seen
        seen |= more


@settings(max_examples=150, deadline=None)
@given(nodes=nodes_st, edges=edges_st)
def test_install_order_against_brute_force(
    nodes: set[str], edges: dict[str, list[str]]
) -> None:
    order, cycles = resolver.install_order(nodes, edges)
    assert sorted(order) == sorted(nodes)
    closure = {n: reach(n, edges, nodes) for n in nodes}
    group = {n: tuple(sorted(m for m in closure[n] if n in closure[m])) for n in nodes}
    assert cycles == sorted(list(g) for g in set(group.values()) if len(g) > 1)
    # Rebuild the documented order: among the groups whose dependencies are all
    # placed, take the one with the smallest name and emit it together.
    expected: list[str] = []
    while len(expected) < len(nodes):
        ready = [
            group[n]
            for n in nodes - set(expected)
            if all(closure[m] <= set(expected) | set(group[n]) for m in group[n])
        ]
        expected += min(ready)
    assert order == expected


@given(nodes=nodes_st, edges=edges_st, data=st.data())
def test_install_order_ignores_input_order(
    nodes: set[str], edges: dict[str, list[str]], data: st.DataObject
) -> None:
    shuffled_nodes = data.draw(st.permutations(sorted(nodes)))
    keys = data.draw(st.permutations(list(edges)))
    shuffled = {k: data.draw(st.permutations(edges[k])) for k in keys}
    assert resolver.install_order(shuffled_nodes, shuffled) == resolver.install_order(
        nodes, edges
    )


@given(
    roots=st.lists(st.sampled_from(NAMES), max_size=4),
    deps=st.dictionaries(st.sampled_from(NAMES), st.sets(st.sampled_from(NAMES))),
)
def test_resolve_is_the_transitive_closure(
    roots: list[str], deps: dict[str, set[str]]
) -> None:
    edges = {k: list(v) for k, v in deps.items()}
    everything = set().union(*(reach(r, edges, set(NAMES)) for r in roots))
    if everything <= set(deps):
        assert resolver.resolve(roots, deps) == everything
    else:
        with pytest.raises(resolver.UnknownPackageError) as caught:
            resolver.resolve(roots, deps)
        assert caught.value.args[0] in everything - set(deps)


@dataclass
class Pkg:
    category: str
    depends: list[str]


class Db:
    def __init__(self, packages: dict[str, Pkg]) -> None:
        self.packages = packages

    def get(self, name: str) -> Pkg | None:
        return self.packages.get(name)

    def has(self, name: str) -> bool:
        return name in self.packages


ARCH = "universal-darwin"
OTHER = "windows"
INDEXED = [*NAMES, "scheme-x", "tlperl", f"a.{ARCH}", f"b.{OTHER}"]
targets = st.sampled_from([
    *INDEXED,
    "missing",
    "a.ARCH",
    "b.ARCH",
    "release/2026",
    "00texlive.config",
])
packages_st = st.dictionaries(
    st.sampled_from(INDEXED),
    st.builds(
        Pkg,
        st.sampled_from(["Package", "Package", "Collection", "Scheme"]),
        st.lists(targets, max_size=4),
    ),
)
roots_st = st.lists(targets, max_size=4)


@settings(max_examples=150, deadline=None)
@given(
    packages=packages_st,
    roots=roots_st,
    system=st.sets(st.sampled_from(INDEXED), max_size=2),
    infrastructure=st.booleans(),
)
def test_resolve_packages_invariants(
    packages: dict[str, Pkg],
    roots: list[str],
    system: set[str],
    infrastructure: bool,
) -> None:
    res = resolver.resolve_packages(
        Db(packages),
        roots,
        arch=ARCH,
        architectures=[ARCH, OTHER],
        system_installed=lambda names: system & set(names),
        infrastructure=infrastructure,
    )
    included = set(res.edges)
    assert res.roots == list(dict.fromkeys(roots))
    assert len(res.order) == len(set(res.order))
    assert set(res.order) == (included - res.meta) | (included & set(res.roots))
    assert included <= set(packages)
    assert not included & set(res.skipped)
    assert not included & set(res.missing)
    assert not set(res.missing) & set(packages)
    assert res.meta == {n for n in included if packages[n].category != "Package"}
    assert not included & (system - res.meta)
    assert f"b.{OTHER}" not in included
    assert infrastructure or "tlperl" not in included
    position = {n: i for i, n in enumerate(res.order)}
    cycle_of = {n: i for i, c in enumerate(res.cycles) for n in c}
    for n, deps in res.edges.items():
        assert deps == sorted(set(deps))
        assert set(deps) <= included
        assert n not in deps
        for d in deps:
            same = n in cycle_of and cycle_of.get(d) == cycle_of[n]
            if n in position and d in position and not same:
                assert position[d] < position[n]
    for name in included | set(res.missing):
        path = res.why(name)
        assert path
        assert path[-1] == name
        assert path[0] in res.roots
        for up, down in pairwise(path):
            assert down in res.edges[up] or up in res.missing[down]
    assert res.why("never-heard-of-it") == []


@given(packages=packages_st, roots=roots_st, data=st.data())
def test_resolve_packages_ignores_input_order(
    packages: dict[str, Pkg], roots: list[str], data: st.DataObject
) -> None:
    def run(index: dict[str, Pkg], wanted: list[str]) -> resolver.Resolution:
        return resolver.resolve_packages(
            Db(index), wanted, arch=ARCH, architectures=[ARCH, OTHER]
        )

    shuffled = {
        name: Pkg(pkg.category, data.draw(st.permutations(pkg.depends)))
        for name, pkg in packages.items()
    }
    one = run(packages, roots)
    two = run(shuffled, data.draw(st.permutations(roots)))
    assert (one.order, one.edges, one.cycles) == (two.order, two.edges, two.cycles)
    assert (one.meta, one.skipped, one.missing) == (two.meta, two.skipped, two.missing)


# --- regressions --------------------------------------------------------------


def test_install_order_orders_only_the_given_nodes() -> None:
    assert resolver.install_order({"a"}, {"a": ["b"]})[0] == ["a"]
