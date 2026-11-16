import tomllib

import pytest
from typer.testing import CliRunner

from texi import cli, texscan


@pytest.mark.parametrize(
    ("paths", "src_dir", "targets"),
    [
        (["src/paper.tex"], "src", ["paper.tex"]),
        (["paper.tex"], ".", ["paper.tex"]),
        (["src/z.tex", "a.tex"], ".", ["a.tex", "src/z.tex"]),
        ([], "src", ["main.tex"]),
    ],
)
def test_init_discovers_targets(tmp_path, monkeypatch, paths, src_dir, targets):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(texscan, "_parser", lambda: None)
    monkeypatch.setattr(cli, "_prefetch_grammar", lambda: None)
    monkeypatch.setattr(cli, "resolve_dependencies", lambda *args: [])
    monkeypatch.setattr(cli, "_create_env", lambda *args: None)
    for name in paths + ["build/old.tex", ".hidden/old.tex", ".texenv/old.tex"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r"\documentclass{article}")
    (tmp_path / "chapter.tex").write_text("% \\documentclass{article}\nText")
    (tmp_path / "example.cls").write_text(r"\documentclass{article}")
    result = CliRunner().invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.output
    project = tomllib.loads((tmp_path / "texproject.toml").read_text())["project"]
    assert project["src_dir"] == src_dir
    assert project["targets"] == targets
