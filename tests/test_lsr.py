"""texi's own mktexlsr: ls-R files that kpathsea reads."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from texi import cli, lsr

runner = CliRunner()


def make_tree(root: Path) -> None:
    for rel in (
        "tex/latex/foo/foo.sty",
        "tex/latex/foo/foo-extra.sty",
        "fonts/tfm/a.tfm",
        ".git/config",
        "CVS/Entries",
        "tex/latex/.hidden",
    ):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")


def test_format_matches_mktexlsr(tmp_path: Path) -> None:
    make_tree(tmp_path)
    lsr.write(tmp_path)
    text = (tmp_path / "ls-R").read_text()
    assert text.splitlines()[:3] == [lsr.MAGIC, "", "./:"]
    assert "./tex/latex/foo:\nfoo-extra.sty\nfoo.sty\n" in text
    assert "\n./fonts/tfm:\na.tfm\n" in text
    assert ".hidden" in text  # dotfiles are indexed, like mktexlsr does
    assert ".git" not in text and "CVS" not in text
    # the root lists ls-R itself, as it will exist after writing
    assert text.split("\n\n")[1].split("\n").count("ls-R") == 1


def test_second_run_is_identical_and_lists_ls_r_once(tmp_path: Path) -> None:
    make_tree(tmp_path)
    first = lsr.write(tmp_path).read_text()
    assert lsr.write(tmp_path).read_text() == first


def test_symlinked_dirs_are_followed_but_loops_are_not(tmp_path: Path) -> None:
    make_tree(tmp_path / "tree")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "s.tex").write_text("x")
    (tmp_path / "tree" / "link").symlink_to(shared)
    (tmp_path / "tree" / "loop").symlink_to(tmp_path / "tree")
    text = lsr.render(tmp_path / "tree")
    assert "./link:\ns.tex\n" in text
    assert text.count("./loop:") <= 1 and "./loop/loop" not in text


def test_errors(tmp_path: Path) -> None:
    with pytest.raises(lsr.LsrError):
        lsr.write(tmp_path / "missing")
    assert lsr.write_all([tmp_path / "missing"]) == []


@pytest.mark.skipif(shutil.which("kpsewhich") is None, reason="needs kpsewhich")
def test_kpathsea_finds_files_through_our_ls_r(tmp_path: Path) -> None:
    make_tree(tmp_path)
    lsr.write(tmp_path)
    env = {**os.environ, "TEXMFCNF": str(tmp_path), "TEXMF": f"!!{tmp_path}", "TEXMFDBS": f"{tmp_path}"}
    (tmp_path / "texmf.cnf").write_text(
        f"TEXMF = !!{tmp_path}\nTEXMFDBS = {{!!{tmp_path}}}\nTEXINPUTS = $TEXMF/tex//\n"
    )
    out = subprocess.run(
        ["kpsewhich", "-progname=latex", "foo.sty"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert out.stdout.strip() == str(tmp_path / "tex/latex/foo/foo.sty"), out.stderr


def test_command_indexes_the_texenv_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", os.environ.copy())  # texi sets TEXMF* for a standalone tree
    env = tmp_path / ".texenv"
    for d in ("texmf-dist/tex", "texmf-var", "texmf-local", "tlpkg"):
        (env / d).mkdir(parents=True)
    (env / "texmf-local" / "texmf-local-marker").write_text("x")
    (env / "texmf-local").joinpath("x").mkdir()
    (env / "texmf-home").mkdir()
    (env / "bin" / "universal-darwin").mkdir(parents=True)
    (env / "tlpkg" / "texlive.tlpdb").write_text("name x\n")
    r = runner.invoke(cli.app, ["mktexlsr"])
    assert r.exit_code == 0, r.output
    for d in ("texmf-dist", "texmf-var", "texmf-local"):
        assert (env / d / "ls-R").is_file(), d
    assert not (env / "texmf-home" / "ls-R").exists()
    other = tmp_path / "other"
    other.mkdir()
    r = runner.invoke(cli.app, ["mktexlsr", str(other)])
    assert r.exit_code == 0 and (other / "ls-R").is_file()


def _standalone_env(root: Path) -> Path:
    env = root / ".texenv"
    (env / "bin" / "universal-darwin").mkdir(parents=True)
    (env / "tlpkg").mkdir()
    (env / "tlpkg" / "texlive.tlpdb").write_text("name x\n")
    return env


def test_activate_prints_only_shell_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {**os.environ, "PATH": "/usr/bin:/bin", "TEXINPUTS": "/x"})
    env = _standalone_env(tmp_path)
    r = runner.invoke(cli.app, ["activate", "--shell", "bash"])
    assert r.exit_code == 0, r.output
    root = env.resolve()
    lines = r.output.splitlines()
    assert lines[0] == f"export PATH={root}/bin/universal-darwin:/usr/bin:/bin"
    assert f"export TEXMFROOT={root}" in lines
    assert any(l.startswith("unset ") and "TEXINPUTS" in l for l in lines)
    assert all(l.startswith(("export ", "unset ")) for l in lines)  # safe to eval
    # every line is valid shell and puts the tree first
    out = subprocess.run(
        ["sh", "-c", f'eval "$1"; echo "$PATH"; echo "$TEXI_ENV"', "sh", r.output],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert out[0].startswith(f"{root}/bin/universal-darwin:") and out[1] == str(root)


def test_activate_off_and_fish_and_no_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {**os.environ, "PATH": "/usr/bin"})
    assert runner.invoke(cli.app, ["activate"]).exit_code == 1  # no .texenv/: nothing on stdout
    env = _standalone_env(tmp_path)
    root = env.resolve()
    off = runner.invoke(cli.app, ["activate", "--off", "--shell", "bash"]).output
    assert "export PATH=/usr/bin" in off and "unset " in off and "TEXI_ENV" in off
    assert str(root / "bin") not in off.split("\n")[0]
    fish = runner.invoke(cli.app, ["activate", "--shell", "fish"]).output
    assert fish.startswith(f"set -gx PATH '{root}/bin/universal-darwin' '/usr/bin'")
    assert "set -gx TEXMFROOT" in fish and "set -e TEXINPUTS" in fish
