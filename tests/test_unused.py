"""texi unused: dependencies that the build and the sources do not use."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from texi import cli, deptree, unused

runner = CliRunner()

TLPDB = """\
name 00texlive.installation
category TLCore

name used
category Package
revision 1
depend helper
runfiles size=1
 texmf-dist/tex/latex/used/used.sty

name helper
category Package
revision 1
runfiles size=1
 texmf-dist/tex/latex/helper/helper.sty

name viafont
category Package
revision 1
runfiles size=1
 texmf-dist/fonts/type1/viafont/f.pfb

name lonely
category Package
revision 1
runfiles size=1
 texmf-dist/tex/latex/lonely/lonely.sty

name collection-x
category Collection
revision 1
depend viafont
depend lonely

name collection-none
category Collection
revision 1
depend lonely
"""


@pytest.fixture
def env(tmp_path: Path) -> Path:
    e = tmp_path / ".texenv"
    (e / "tlpkg").mkdir(parents=True)
    (e / "tlpkg" / "texlive.tlpdb").write_text(TLPDB)
    return e


def test_owners_and_log_paths(env: Path) -> None:
    owners = unused.file_owners(env)
    assert owners["texmf-dist/tex/latex/used/used.sty"] == "used"
    root = str(env)
    log = f"(./main.tex ({root}/texmf-dist/tex/latex/used/used.sty) <{root}/texmf-dist/fonts/type1/viafont/f.pfb>"
    paths = unused.log_paths(log, [root])
    assert unused.owners_of(paths, env, owners) == {"used", "viafont"}
    assert unused.owners_of({"/elsewhere/x.sty"}, env, owners) == set()


def test_analyze_sorts_dependencies(env: Path) -> None:
    g = deptree.read(env)
    cats = {"collection-x": "Collection", "collection-none": "Collection"}
    rep = unused.analyze(
        g,
        ["used", "helper", "lonely", "collection-x", "collection-none", "ghost"],
        {"used", "viafont"},
        cats,
    )
    assert rep.used == ["used"]
    assert rep.needed == {"helper": ["used"]}  # used depends on it
    assert rep.unused == ["lonely", "collection-none"]
    assert rep.via_collection == {"collection-x": "viafont"}
    assert rep.not_installed == ["ghost"]


def test_command_needs_a_build_record(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = env.parent
    (proj / "texproject.toml").write_text('[project]\nname = "p"\ndependencies = ["used"]\n')
    monkeypatch.chdir(proj)
    monkeypatch.setattr(cli, "_target_arch", lambda e: "universal-darwin")
    r = runner.invoke(cli.app, ["unused"])
    assert r.exit_code == 1 and "Run `texi build` first" in " ".join(r.output.split())


def test_command_lists_unused(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = env.parent
    (proj / "texproject.toml").write_text(
        '[project]\nname = "p"\nsrc_dir = "."\ndependencies = ["used", "lonely"]\n'
    )
    (proj / "main.tex").write_text("\\documentclass{article}\n")
    aux = proj / "build" / "aux"
    aux.mkdir(parents=True)
    root = env.resolve()
    (aux / "p.fls").write_text(
        f"PWD {proj}\nINPUT {root}/texmf-dist/tex/latex/used/used.sty\n"
    )
    monkeypatch.chdir(proj)
    monkeypatch.setattr(cli, "_target_arch", lambda e: "universal-darwin")
    monkeypatch.setattr(cli, "resolve_dependencies", lambda names, console, *a: sorted(names))
    r = runner.invoke(cli.app, ["unused"])
    flat = " ".join(r.output.split())
    assert r.exit_code == 0, r.output
    assert "1 unused: likely safe to remove" in flat and "lonely" in flat
    assert "1 used, 1 unused" in flat
