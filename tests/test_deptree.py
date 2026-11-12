"""texi tree: the dependency graph of the packages in .texenv/."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from texi import cli, deptree

runner = CliRunner()

TLPDB = """\
name 00texlive.installation
category TLCore
depend opt_location:http://x

name app
category Package
revision 3
depend lib
depend tool
depend app.ARCH

name app.universal-darwin
category Package
revision 3

name lib
category Package
revision 2
depend base

name tool
category Package
revision 5
depend base
depend ghost

name base
category Package
revision 1
depend core

name core
category Package
revision 1

name loop
category Package
revision 9
depend loop2

name loop2
category Package
revision 9
depend loop

name unrelated
category Package
revision 4
depend base
"""


@pytest.fixture
def env(tmp_path: Path) -> Path:
    e = tmp_path / ".texenv"
    (e / "tlpkg").mkdir(parents=True)
    (e / "tlpkg" / "texlive.tlpdb").write_text(TLPDB)
    return e


def names(nodes: list[deptree.Node]) -> list[str]:
    return [n.name for n in nodes]


def test_graph_hides_binary_variants_arch_placeholders_and_uninstalled(env: Path) -> None:
    g = deptree.read(env, "universal-darwin")
    assert "app.universal-darwin" not in g.revisions
    assert "00texlive.installation" not in g.revisions
    assert g.deps["app"] == ["lib", "tool"]
    assert g.deps["tool"] == ["base"]  # ghost is not installed
    assert g.dependents["base"] == ["lib", "tool", "unrelated"]
    assert "app" in g.roots() and "core" not in g.roots()


def test_tree_dependencies_depth_and_repeats(env: Path) -> None:
    g = deptree.read(env, "universal-darwin")
    (app,) = deptree.build(g, ["app"])
    assert names(app.children) == ["lib", "tool"]
    assert names(app.children[0].children) == ["base"]
    assert names(app.children[0].children[0].children) == ["core"]
    assert app.children[1].children[0].seen  # base already shown under lib
    (cut,) = deptree.build(g, ["app"], depth=1)
    assert names(cut.children) == ["lib", "tool"]
    assert cut.children[0].cut and cut.children[0].children == []
    (flat,) = deptree.build(g, ["app"], depth=0)
    assert flat.children == [] and flat.cut


def test_invert_lists_dependents(env: Path) -> None:
    g = deptree.read(env, "universal-darwin")
    (base,) = deptree.build(g, ["base"], invert=True)
    assert names(base.children) == ["lib", "tool", "unrelated"]
    assert names(base.children[0].children) == ["app"]


def test_cycles_terminate(env: Path) -> None:
    g = deptree.read(env, "universal-darwin")
    (loop,) = deptree.build(g, ["loop"])
    assert names(loop.children) == ["loop2"] and loop.children[0].children == []


def test_closure_for_explicit(env: Path) -> None:
    g = deptree.read(env, "universal-darwin")
    assert deptree.closure(g, ["lib"]) == {"lib", "base", "core"}


@pytest.fixture
def project(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proj = env.parent
    (proj / "texproject.toml").write_text(
        '[project]\nname = "p"\ndependencies = ["lib", "missing"]\n'
    )
    monkeypatch.chdir(proj)
    monkeypatch.setattr(cli, "_target_arch", lambda e: "universal-darwin")
    return proj


def test_tree_command_explicit_and_package(project: Path) -> None:
    r = runner.invoke(cli.app, ["tree", "--explicit"])
    assert r.exit_code == 0, r.output
    assert "lib" in r.output and "base" in r.output
    assert "unrelated" not in r.output and "app r3" not in r.output
    assert "missing is in texproject.toml but not installed" in r.output

    r = runner.invoke(cli.app, ["tree", "--package", "app", "--depth", "1"])
    assert "app r3" in r.output and "lib r2" in r.output and "base" not in r.output

    r = runner.invoke(cli.app, ["tree", "-p", "base", "--invert"])
    assert "packages that depend on base" in r.output and "app r3" in r.output


def test_tree_command_errors(project: Path) -> None:
    assert runner.invoke(cli.app, ["tree", "--invert"]).exit_code == 1
    r = runner.invoke(cli.app, ["tree", "-p", "nope"])
    assert r.exit_code == 1 and "not installed" in r.output


def test_build_tree_is_the_old_tree_command() -> None:
    names = [c.name or c.callback.__name__ for c in cli.app.registered_commands]
    assert "build-tree" in names and "tree" in names
