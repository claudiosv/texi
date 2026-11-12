"""Tests for texi.resolver with a dict-backed fake index (no network)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from texi import resolver, tlpdb
from texi.resolver import (
    UnknownPackageError,
    is_pseudo_record,
    resolve,
    resolve_packages,
)

ARCHS = ["universal-darwin", "x86_64-linux", "windows"]

# the exact depend lines of 00texlive.config
CONFIG_LINES = [
    "opt_autobackup:1",
    "opt_backupdir:tlpkg/backups",
    "opt_create_formats:1",
    "opt_location:__MASTER__",
    "opt_sys_bin:/usr/local/bin",
    "setting_available_architectures:aarch64-linux amd64-freebsd i386-linux universal-darwin windows x86_64-linux",
    "container_format/xz",
    "container_split_doc_files/1",
    "frozen/0",
    "minrelease/2016",
    "release/2026",
    "revision/81047",
]


@dataclass
class Pkg:
    name: str
    depends: list[str] = field(default_factory=list)
    category: str = "Package"


class FakeDb:
    def __init__(self, *pkgs: Pkg) -> None:
        self.pkgs = {p.name: p for p in pkgs}

    def get(self, name: str) -> Pkg | None:
        return self.pkgs.get(name)

    def has(self, name: str) -> bool:
        return name in self.pkgs


def res(db: FakeDb, roots, arch: str | None = "universal-darwin", **kw):
    kw.setdefault("architectures", ARCHS)
    return resolve_packages(db, roots, arch=arch, **kw)


def test_resolve_sketch_semantics() -> None:
    deps = {"a": {"b", "c"}, "b": {"c"}, "c": set(), "d": {"a"}}
    assert resolve(["a"], deps) == {"a", "b", "c"}
    assert resolve([], deps) == set()
    assert resolve(["a", "d"], deps) == {"a", "b", "c", "d"}
    assert resolve({"c"}, deps) == {"c"}
    assert resolve(["a"], {"a": {"a"}}) == {"a"}  # self-dependency
    assert resolve(["a"], {"a": {"b"}, "b": {"a"}}) == {"a", "b"}  # cycle


def test_resolve_unknown_raises() -> None:
    with pytest.raises(UnknownPackageError):
        resolve(["nope"], {"a": set()})
    with pytest.raises(UnknownPackageError):
        resolve(["a"], {"a": {"nope"}})
    assert issubclass(UnknownPackageError, ValueError)


def test_diamond_order() -> None:
    db = FakeDb(
        Pkg("top", ["left", "right"]),
        Pkg("left", ["base"]),
        Pkg("right", ["base"]),
        Pkg("base"),
    )
    r = res(db, ["top"])
    assert r.order == ["base", "left", "right", "top"]
    assert r.edges["top"] == ["left", "right"]
    assert r.cycles == []
    assert r.missing == {}
    assert r.skipped == {}
    assert r.why("base") == ["top", "left", "base"]
    assert r.why("top") == ["top"]
    assert r.why("elsewhere") == []


def test_cycle_is_tolerated_and_reported() -> None:
    db = FakeDb(
        Pkg("root", ["b"]),
        Pkg("a", ["b"]),
        Pkg("b", ["c"]),
        Pkg("c", ["a", "leaf"]),
        Pkg("leaf"),
    )
    r = res(db, ["root"])
    assert r.cycles == [["a", "b", "c"]]
    assert r.order == ["leaf", "a", "b", "c", "root"]


def test_self_dependency_ignored() -> None:
    r = res(FakeDb(Pkg("a", ["a", "b"]), Pkg("b", ["b"])), ["a"])
    assert r.order == ["b", "a"]
    assert r.cycles == []
    assert r.edges["a"] == ["b"]


@pytest.mark.parametrize("seed", range(6))
def test_order_independent_of_insertion_order(seed: int) -> None:
    pkgs = [
        Pkg("r", ["x", "y", "z", "w"]),
        Pkg("x", ["w", "c1"]),
        Pkg("y", ["w"]),
        Pkg("z", ["c1"]),
        Pkg("w"),
        Pkg("c1", ["c2"]),
        Pkg("c2", ["c1"]),
    ]
    expected = res(FakeDb(*pkgs), ["r"]).order
    rng = random.Random(seed)
    shuffled = [Pkg(p.name, rng.sample(p.depends, len(p.depends))) for p in pkgs]
    rng.shuffle(shuffled)
    got = res(FakeDb(*shuffled), ["r"])
    assert got.order == expected
    assert expected.index("w") < expected.index("x") < expected.index("r")


def test_unknown_root_and_dependency() -> None:
    db = FakeDb(Pkg("a", ["ghost", "b"]), Pkg("b", ["ghost"]))
    r = res(db, ["a", "nothing"])
    assert r.missing == {"ghost": {"a", "b"}, "nothing": set()}
    assert r.order == ["b", "a"]
    assert r.why("ghost") == ["a", "ghost"]
    with pytest.raises(UnknownPackageError, match="ghost"):
        res(db, ["a"], strict=True)
    with pytest.raises(UnknownPackageError, match="nothing"):
        res(db, ["nothing"], strict=True)


def test_arch_expansion_for_two_archs() -> None:
    db = FakeDb(
        Pkg("tool", ["tool.ARCH"]),
        Pkg("tool.universal-darwin"),
        Pkg("tool.x86_64-linux"),
    )
    mac = res(db, ["tool"], arch="universal-darwin")
    assert mac.order == ["tool.universal-darwin", "tool"]
    lin = res(db, ["tool"], arch="x86_64-linux")
    assert lin.order == ["tool.x86_64-linux", "tool"]
    assert lin.why("tool.x86_64-linux") == ["tool", "tool.x86_64-linux"]


def test_missing_arch_package_is_skipped_not_an_error() -> None:
    db = FakeDb(Pkg("tool", ["tool.ARCH"]), Pkg("tool.universal-darwin"))
    r = res(db, ["tool"], arch="windows", strict=True)
    assert r.order == ["tool"]
    assert r.missing == {}
    assert "windows" in r.skipped["tool.windows"]
    unknown = res(db, ["tool"], arch=None)
    assert unknown.order == ["tool"]
    assert "tool.ARCH" in unknown.skipped


def test_foreign_arch_explicit_dependency_skipped() -> None:
    db = FakeDb(
        Pkg("a", ["a.windows", "a.universal-darwin", "dotted.name"]),
        Pkg("a.windows"),
        Pkg("a.universal-darwin"),
        Pkg("dotted.name"),
    )
    r = res(db, ["a"])
    assert r.skipped == {"a.windows": resolver.REASON_FOREIGN}
    assert r.order == ["a.universal-darwin", "dotted.name", "a"]


def test_pseudo_records_never_become_edges() -> None:
    config = Pkg("00texlive.config", CONFIG_LINES, "TLCore")
    db = FakeDb(config, Pkg("a", ["00texlive.config", "b"]), Pkg("b"))
    # a defensive check on raw fake data: nothing from the lines is looked up
    r = res(db, ["00texlive.config", "a"], strict=True)
    assert r.order == ["b", "a"]
    assert r.skipped == {"00texlive.config": resolver.REASON_METADATA}
    assert "00texlive.config" not in r.edges
    assert r.missing == {}
    assert all(is_pseudo_record("00texlive.config", d) for d in CONFIG_LINES)
    assert is_pseudo_record("x", "release/2026")
    assert is_pseudo_record("x", "a:b")
    assert not is_pseudo_record("x", "foo.ARCH")
    # a non-00texlive owner with pseudo-looking values: ignored too
    odd = FakeDb(Pkg("p", ["release/2026", "opt_x:1", "q"]), Pkg("q"))
    assert res(odd, ["p"], strict=True).order == ["q", "p"]


def test_parsed_config_entry_has_no_dependency_edges() -> None:
    text = (
        "name 00texlive.config\ncategory TLCore\nrevision 1\n"
        + "".join(f"depend {line}\n" for line in CONFIG_LINES)
        + "\nname a\ncategory Package\nrevision 1\ndepend b\n\n"
        + "name b\ncategory Package\nrevision 1\n\n"
    )
    db = tlpdb.parse_tlpdb(text)
    cfg = db.get("00texlive.config")
    assert cfg is not None
    assert cfg.depends == []
    assert cfg.settings == CONFIG_LINES
    assert db.available_architectures() == [
        "aarch64-linux",
        "amd64-freebsd",
        "i386-linux",
        "universal-darwin",
        "windows",
        "x86_64-linux",
    ]
    r = resolve_packages(
        db, ["00texlive.config", "a"], arch="x86_64-linux", strict=True
    )
    assert r.order == ["b", "a"]


def test_collections_and_schemes_are_traversed_into_meta() -> None:
    db = FakeDb(
        Pkg("app", ["collection-x"]),
        Pkg("collection-x", ["m1", "m2"], "Collection"),
        Pkg("m1"),
        Pkg("m2", ["scheme-s"]),
        Pkg("scheme-s", ["m3"], "Scheme"),
        Pkg("m3"),
    )
    r = res(db, ["app"])
    assert r.meta == {"collection-x", "scheme-s"}
    assert r.order == ["m1", "m3", "m2", "app"]
    assert r.why("m3") == ["app", "collection-x", "m2", "scheme-s", "m3"]


def test_collection_root_is_expanded_and_kept_in_order() -> None:
    db = FakeDb(
        Pkg("collection-x", ["m1", "m2"], "Collection"), Pkg("m1"), Pkg("m2", ["m1"])
    )
    r = res(db, ["collection-x"])
    assert r.order == ["m1", "m2", "collection-x"]
    assert r.meta == {"collection-x"}


def test_infra_and_system_installed_are_skipped_and_not_followed() -> None:
    db = FakeDb(
        Pkg("a", ["texlive.infra", "sys", "plain"]),
        Pkg("texlive.infra", ["never"]),
        Pkg("sys", ["deep"]),
        Pkg("deep"),
        Pkg("plain"),
        Pkg("collection-c", [], "Collection"),
    )
    calls: list[list[str]] = []

    def system(names: list[str]) -> set[str]:
        calls.append(sorted(names))
        return {"sys"} & set(names)

    r = res(db, ["a"], system_installed=system)
    assert r.order == ["plain", "a"]
    assert r.skipped == {
        "texlive.infra": resolver.REASON_INFRA,
        "sys": resolver.REASON_SYSTEM,
    }
    assert calls == [["a"], ["plain", "sys"]]  # once per level
    assert "deep" not in r.edges


def test_system_callback_not_asked_about_collections() -> None:
    db = FakeDb(Pkg("collection-c", ["p"], "Collection"), Pkg("p"))
    calls: list[list[str]] = []
    res(db, ["collection-c"], system_installed=lambda n: calls.append(n) or set())
    assert calls == [["p"]]


def test_closure_of_large_chain_is_iterative() -> None:
    n = 3000
    db = FakeDb(*(Pkg(f"p{i:04d}", [f"p{i + 1:04d}"]) for i in range(n)), Pkg("end"))
    db.pkgs[f"p{n - 1:04d}"].depends = ["end"]
    r = res(db, ["p0000"])
    assert len(r.order) == n + 1
    assert r.order[0] == "end"
    assert len(r.why("end")) == n + 1
