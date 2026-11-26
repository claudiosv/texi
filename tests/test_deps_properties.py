"""Property tests of the dependency graph and the unused-dependency report."""

from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from texi import deptree, unused

NAMES = ["a", "b", "c", "d", "e", "f", "latexmk"]


def graph(deps: dict[str, list[str]]) -> deptree.Graph:
    """A Graph as deptree.read() makes it: sorted edges, no self-loops."""
    g = deptree.Graph(revisions=dict.fromkeys(deps, "1"))
    for name in sorted(deps):
        g.deps[name] = sorted(set(deps[name]) - {name})
        for d in g.deps[name]:
            g.dependents.setdefault(d, []).append(name)
    return g


@st.composite
def graphs(draw: st.DrawFn) -> deptree.Graph:
    names = draw(st.lists(st.sampled_from(NAMES), unique=True, max_size=len(NAMES)))
    edges = st.lists(st.sampled_from(names or ["a"]), max_size=3)
    return graph({n: [d for d in draw(edges) if d in names] for n in names})


some_names = st.lists(st.sampled_from([*NAMES, "ghost"]), max_size=6)


def fixpoint(edges: dict[str, list[str]], roots: set[str]) -> set[str]:
    found = set(roots)
    while True:
        more = {d for n in found for d in edges.get(n, [])} - found
        if not more:
            return found
        found |= more


@given(g=graphs(), roots=some_names)
def test_closure_is_the_fixpoint(g: deptree.Graph, roots: list[str]) -> None:
    assert deptree.closure(g, roots) == fixpoint(g.deps, set(roots) & set(g.revisions))


def walk(
    nodes: list[deptree.Node], path: tuple[str, ...] = ()
) -> list[tuple[deptree.Node, tuple[str, ...]]]:
    out = []
    for node in nodes:
        here = (*path, node.name)
        out.append((node, here))
        out += walk(node.children, here)
    return out


@given(
    g=graphs(),
    roots=some_names,
    depth=st.one_of(st.none(), st.integers(0, 5)),
    invert=st.booleans(),
    only=st.one_of(st.none(), st.sets(st.sampled_from(NAMES))),
)
def test_build_follows_edges_within_depth(
    g: deptree.Graph,
    roots: list[str],
    depth: int | None,
    invert: bool,
    only: set[str] | None,
) -> None:
    tree = deptree.build(g, roots, depth=depth, invert=invert, only=only)
    edges = g.dependents if invert else g.deps
    assert [n.name for n in tree] == [r for r in roots if r in g.revisions]
    expanded: list[str] = []
    for node, path in walk(tree):
        assert len(set(path)) == len(path)
        assert depth is None or len(path) - 1 <= depth
        for parent, child in pairwise(path):
            assert child in edges[parent]
            assert only is None or child in only
        assert not (node.children and (node.seen or node.cut))
        if depth is None:
            assert not node.cut
        if node.children:
            expanded.append(node.name)
    assert len(expanded) == len(set(expanded))
    if depth is None:
        keep = {
            n: [c for c in cs if only is None or c in only] for n, cs in edges.items()
        }
        shown = {node.name for node, _ in walk(tree)}
        assert shown == fixpoint(keep, set(roots) & set(g.revisions))


@given(
    g=graphs(),
    roots=some_names,
    depth=st.integers(0, 5),
    invert=st.booleans(),
    only=st.one_of(st.none(), st.sets(st.sampled_from(NAMES))),
)
def test_build_depth_shows_every_package_that_near(
    g: deptree.Graph,
    roots: list[str],
    depth: int,
    invert: bool,
    only: set[str] | None,
) -> None:
    edges = g.dependents if invert else g.deps
    near = set(roots) & set(g.revisions)
    for _ in range(depth):
        near |= {d for n in near for d in edges.get(n, []) if only is None or d in only}
    tree = deptree.build(g, roots, depth=depth, invert=invert, only=only)
    assert {node.name for node, _ in walk(tree)} == near


def test_build_depth_keeps_a_package_reachable_by_a_shorter_path() -> None:
    g = graph({"a": ["b", "c"], "b": ["c"], "c": ["d"], "d": []})
    tree = deptree.build(g, ["a"], depth=2)
    # a -> c -> d is two levels.
    assert "d" in {node.name for node, _ in walk(tree)}


@given(g=graphs(), keep=st.sets(st.sampled_from([*NAMES, "ghost"])))
def test_contract_links_through_unlisted_packages(
    g: deptree.Graph, keep: set[str]
) -> None:
    out = deptree.contract(g, keep)
    kept = keep & set(g.revisions)
    assert set(out.revisions) == kept
    unlisted = {n: ds for n, ds in g.deps.items() if n not in keep}
    for a in kept:
        through = fixpoint(unlisted, set(g.deps[a]) - {a})
        assert out.deps[a] == sorted((through & keep) - {a})
    # Reachability between listed packages survives.
    for a in kept:
        assert fixpoint(out.deps, {a}) == deptree.closure(g, [a]) & kept
    assert {(a, b) for a, bs in out.deps.items() for b in bs} == {
        (a, b) for b, parents in out.dependents.items() for a in parents
    }
    assert all(v == sorted(set(v)) for v in out.dependents.values())


@given(
    g=graphs(),
    dependencies=some_names,
    used=st.sets(st.sampled_from([*NAMES, "ghost"])),
    categories=st.dictionaries(
        st.sampled_from(NAMES), st.sampled_from(["Package", "Collection", "Scheme"])
    ),
)
def test_analyze_partitions_the_dependencies(
    g: deptree.Graph,
    dependencies: list[str],
    used: set[str],
    categories: dict[str, str],
) -> None:
    rep = unused.analyze(g, dependencies, used, categories)
    buckets = [
        rep.used,
        list(rep.via_collection),
        list(rep.needed),
        rep.unused,
        rep.not_installed,
    ]
    flat = [name for bucket in buckets for name in bucket]
    assert sorted(flat) == sorted(set(dependencies))

    live = (used | unused.ALWAYS_USED) & set(g.revisions)
    assert set(rep.not_installed) == set(dependencies) - set(g.revisions)
    assert set(rep.used) == set(dependencies) & live
    for name, member in rep.via_collection.items():
        assert categories[name] in {"Collection", "Scheme"}
        assert member in live
        assert member in deptree.closure(g, [name])
    for name, needers in rep.needed.items():
        assert needers
        assert needers == sorted(
            u for u in live - {name} if name in deptree.closure(g, [u])
        )
    for name in rep.unused:
        needed = any(name in deptree.closure(g, [u]) for u in live - {name})
        assert not needed


def test_a_collection_needed_by_a_used_package_is_not_unused() -> None:
    g = graph({"a": ["collection-c"], "collection-c": []})
    rep = unused.analyze(g, ["collection-c"], {"a"}, {"collection-c": "Collection"})
    assert "collection-c" not in rep.unused


def test_a_line_separator_in_longdesc_does_not_rename_a_package(
    tmp_path: Path,
) -> None:
    (tmp_path / "tlpkg").mkdir()
    (tmp_path / "tlpkg" / "texlive.tlpdb").write_text(
        f"name good\nrevision 1\nlongdesc x{chr(0x2028)}name evil\n"
        "runfiles size=1\n texmf-dist/tex/good.sty\n",
        encoding="utf-8",
    )
    assert set(deptree.read(tmp_path).revisions) == {"good"}
    assert unused.file_owners(tmp_path) == {"texmf-dist/tex/good.sty": "good"}
