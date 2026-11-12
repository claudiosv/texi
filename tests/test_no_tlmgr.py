"""Archive mode never runs tlmgr: system detection, arch mapping, usertree init, flows."""

import functools
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from texi import cli, texenv, tlpdb, tools
from test_archive import REPO, _db_text, make_client

runner = CliRunner()

SYSTEM_TLPDB = """\
name 00texlive.config
category TLCore
revision 1
depend minrelease/2016
depend release/2026

name 00texlive.installation
category TLCore
revision 2
depend opt_location:http://mirror.ctan.org/systems/texlive/tlnet
depend setting_available_architectures:universal-darwin

name sysdep
category Package
revision 42

name collection-basic
category Collection
revision 7

name latexmk.universal-darwin
category Package
revision 9
"""


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from texi import archive

    monkeypatch.setattr(archive, "_native_disabled", False)
    return tmp_path / "cache" / "texi" / "archive"


@pytest.fixture
def system_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "texlive" / "2026"
    (root / "tlpkg").mkdir(parents=True)
    (root / "tlpkg" / "texlive.tlpdb").write_text(SYSTEM_TLPDB)
    monkeypatch.setattr(tlpdb, "texlive_root", lambda: root)
    tlpdb.reset_system_cache()
    return root


# --- system tlpdb ----------------------------------------------------------------


def test_system_installed_from_tlpdb(system_root: Path) -> None:
    assert tlpdb.system_installed(["sysdep", "collection-basic", "nope"]) == {
        "sysdep",
        "collection-basic",
    }
    info = tlpdb.load_local_installed()
    assert info is not None
    assert info.release == "2026"
    assert info.revisions["sysdep"] == 42
    assert info.architectures == ["universal-darwin"]
    # http://mirror.ctan.org is upgraded to https, as the installer's default
    assert info.location == "https://mirror.ctan.org/systems/texlive/tlnet"


def test_system_tlpdb_cache_follows_mtime(system_root: Path) -> None:
    assert "newpkg" not in tlpdb.system_installed(["newpkg"])
    path = system_root / "tlpkg" / "texlive.tlpdb"
    path.write_text(SYSTEM_TLPDB + "\nname newpkg\ncategory Package\nrevision 1\n")
    os.utime(path, ns=(1, path.stat().st_mtime_ns + 5_000_000_000))
    assert tlpdb.system_installed(["newpkg"]) == {"newpkg"}


def test_no_system_tex_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tlpdb, "texlive_root", lambda: None)
    assert tlpdb.system_installed(["a"]) == set()
    monkeypatch.setattr(tlpdb, "texlive_root", lambda: tmp_path)  # no tlpkg/
    assert tlpdb.load_local_installed() is None


@pytest.mark.parametrize(
    ("platform", "machine", "musl", "arch"),
    [
        ("darwin", "arm64", None, "universal-darwin"),
        ("darwin", "x86_64", None, "universal-darwin"),
        ("linux", "x86_64", False, "x86_64-linux"),
        ("linux", "x86_64", True, "x86_64-linuxmusl"),
        ("linux", "aarch64", False, "aarch64-linux"),
        ("linux", "armv7l", False, "armhf-linux"),
        ("linux", "i686", False, "i386-linux"),
        ("win32", "AMD64", None, "windows"),
        ("freebsd14", "amd64", None, "amd64-freebsd"),
        ("netbsd10", "amd64", None, "amd64-netbsd"),
        ("linux", "riscv64", False, None),
        ("sunos5", "i86pc", None, None),
    ],
)
def test_platform_arch(platform: str, machine: str, musl: bool | None, arch) -> None:
    assert tlpdb.platform_arch(platform, machine, musl) == arch


def test_system_arch_prefers_the_only_installed_arch(
    system_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tlpdb, "platform_arch", lambda: "x86_64-linux")
    assert tlpdb.system_arch() == "universal-darwin"  # the tree's only architecture
    monkeypatch.setattr(tlpdb, "platform_arch", lambda: "universal-darwin")
    assert tlpdb.system_arch() == "universal-darwin"


# --- usertree skeleton -------------------------------------------------------------


def test_init_usertree_skeleton(tmp_path: Path) -> None:
    env = tmp_path / ".texenv"
    texenv.init_usertree(env, "https://r/tlnet")
    for sub in ("tlpkg/tlpobj", "web2c", "texmf-var", "texmf-config"):
        assert (env / sub).is_dir()
    text = (env / "tlpkg/texlive.tlpdb").read_text()
    assert "depend opt_location:https://r/tlnet\n" in text
    assert "depend setting_usertree:1\n" in text
    assert texenv.is_usertree(env)
    (env / "tlpkg/texlive.tlpdb").write_text("kept")
    texenv.init_usertree(env, "https://r/tlnet")  # never overwrites
    assert (env / "tlpkg/texlive.tlpdb").read_text() == "kept"


@pytest.mark.skipif(not shutil.which("tlmgr"), reason="needs tlmgr as an oracle")
def test_init_usertree_accepted_by_real_tlmgr(tmp_path: Path) -> None:
    env = tmp_path / "ut"
    texenv.init_usertree(env)  # our code; tlmgr below only checks the result
    out = subprocess.run(
        ["tlmgr", "--usertree", str(env), "--usermode", "list", "--only-installed"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "setting_usertree" not in out.stderr


# --- the ban itself ----------------------------------------------------------------


def test_guard_raises_in_archive_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    tools.set_forbid_tlmgr(True)
    with pytest.raises(AssertionError, match="tlmgr called in archive mode"):
        cli._tl()
    with pytest.raises(AssertionError):
        cli._tlmgr_info("--data", "name", "x")  # type: ignore[arg-type]
    with pytest.raises(AssertionError):
        cli._search_bundles_tlmgr("x", cli.console, (".sty",))
    tools.set_forbid_tlmgr(False)
    monkeypatch.setenv("TEXI_FORBID_TLMGR", "1")
    with pytest.raises(AssertionError):
        cli._tl()


def test_install_method_sets_the_ban() -> None:
    cli._apply_install_method(cli.ProjectConfig())
    assert tools.tlmgr_forbidden()
    cli._apply_install_method(cli.ProjectConfig(install_method="tlmgr"))
    assert not tools.tlmgr_forbidden()
    cli._apply_install_method(None)  # no texproject.toml yet (texi init)
    assert tools.tlmgr_forbidden()


# --- flows with a tlmgr that fails loudly ---------------------------------------------


@pytest.fixture
def shims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """First on PATH: a tlmgr that logs and exits 99, plus latexmk/mktexlsr stand-ins."""
    bin_dir = tmp_path / "shims"
    bin_dir.mkdir()
    log = tmp_path / "tlmgr.log"
    script = {
        "tlmgr": f'#!/bin/sh\necho "$@" >> {log}\nexit 99\n',
        "latexmk": "#!/bin/sh\nexit 0\n",
        "mktexlsr": "#!/bin/sh\nexit 0\n",
        "updmap-user": "#!/bin/sh\nexit 0\n",
        "updmap-sys": "#!/bin/sh\nexit 0\n",
        "fmtutil-sys": "#!/bin/sh\nexit 0\n",
    }
    for name, text in script.items():
        f = bin_dir / name
        f.write_text(text)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cli, "tlmgr_path", str(bin_dir / "tlmgr"))
    return log


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_root: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{foo}\n\\begin{document}x\\end{document}\n"
    )
    (proj / "texproject.toml").write_text(
        f'[project]\nname = "p"\nrepository = "{REPO}"\ndependencies = ["foo"]\n'
    )
    monkeypatch.chdir(proj)
    monkeypatch.setattr(cli, "latexmk_path", None)  # _activate_env rebinds these
    db = tlpdb.parse_tlpdb(_db_text())
    monkeypatch.setattr(cli, "_resolver_tlpdb", lambda console: db)
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda name: [])
    monkeypatch.setattr(cli, "load_sty_cache", dict)
    monkeypatch.setattr(cli, "save_sty_cache", lambda cache: None)
    from texi import archive

    monkeypatch.setattr(
        archive,
        "install_packages",
        functools.partial(archive.install_packages, client=make_client()),
    )
    return proj


def test_sync_never_runs_tlmgr(project: Path, shims: Path) -> None:
    texenv.init_usertree(project / ".texenv", REPO)  # an existing usertree stays one
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert not shims.exists(), shims.read_text()
    env = project / ".texenv"
    assert (env / "tex/latex/foo/foo.sty").exists()
    assert (env / "tex/latex/bar/bar.sty").exists()
    assert texenv.is_usertree(env)
    # sysdep is provided by the system tlpdb and must not be installed
    assert not (env / "tlpkg/tlpobj/sysdep.tlpobj").exists()
    lock = (project / "texi.lock").read_text()
    assert 'texlive = "2026"' in lock and "foo = 100" in lock
    # a second sync is a no-op and still does not touch tlmgr
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert not shims.exists()


def test_sync_clones_standalone_tree(
    project: Path, shims: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(texenv, "CLONE_ROOTS", ["bar"])
    (project / "texproject.toml").write_text(
        f'[project]\nname = "p"\nrepository = "{REPO}"\ndependencies = ["bar"]\n'
    )
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert not shims.exists(), shims.read_text()
    env = project / ".texenv"
    assert not texenv.is_usertree(env)
    assert cli._standalone(env)
    # runfiles live under texmf-dist; the empty stand-in trees exist
    assert (env / "texmf-dist/tex/latex/bar/bar.sty").exists()
    for d in ("texmf-var", "texmf-config", "texmf-local", "texmf-home"):
        assert (env / d).is_dir()
    db = (env / "tlpkg/texlive.tlpdb").read_text()
    assert "00texlive.installation" in db and "setting_usertree" not in db
    # a second sync keeps the tree
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert not shims.exists()


def test_clone_env_scrubs_and_points_into_tree(tmp_path: Path) -> None:
    env = texenv.clone_env(
        tmp_path / ".texenv",
        "universal-darwin",
        {"PATH": "/usr/bin", "TEXINPUTS": "/x", "TEXMFHOME": "/home/x", "HOME": "/h"},
    )
    root = (tmp_path / ".texenv").resolve()
    assert "TEXINPUTS" not in env and env["HOME"] == "/h"
    assert env["TEXMFHOME"] == str(root / "texmf-home")
    assert env["TEXMFCNF"] == str(root / "texmf-dist/web2c")
    assert env["TEXMFSYSVAR"] == env["TEXMFVAR"] == str(root / "texmf-var")
    assert env["PATH"] == f"{root}/bin/universal-darwin{os.pathsep}/usr/bin"


def test_add_never_runs_tlmgr(project: Path, shims: Path) -> None:
    texenv.init_usertree(project / ".texenv", REPO)
    r = runner.invoke(cli.app, ["add", "bar"])
    assert r.exit_code == 0, r.output
    assert not shims.exists()
    assert '"bar"' in (project / "texproject.toml").read_text()
    assert (project / ".texenv/tex/latex/bar/bar.sty").exists()


def test_scan_and_init_never_run_tlmgr(
    project: Path, shims: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = runner.invoke(cli.app, ["scan"])
    assert r.exit_code == 0, r.output
    assert "foo" in r.output
    (project / "texproject.toml").unlink()
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 0, r.output
    assert 'install_method = "archive"' in (project / "texproject.toml").read_text()
    assert not shims.exists()


def test_latexmk_comes_from_the_archive_when_missing(
    project: Path, shims: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (shims.parent / "shims" / "latexmk").unlink()
    monkeypatch.setenv("PATH", str(shims.parent / "shims"))
    calls: list[list[str]] = []
    tools.set_forbid_tlmgr(True)

    def fake_install(prog, repository, env_dir, **kw):  # type: ignore[no-untyped-def]
        calls.append(kw["roots"])
        bindir = env_dir / "bin" / "x86_64-linux"
        bindir.mkdir(parents=True)
        (bindir / "latexmk").write_text("#!/bin/sh\n")
        (bindir / "latexmk").chmod(0o755)
        return cli.archive.Result()

    monkeypatch.setattr(cli, "_archive_install", fake_install)
    cli._ensure_latexmk()
    assert calls == [["latexmk"]]
    assert shutil.which("latexmk") == str(project / ".texenv/bin/x86_64-linux/latexmk")
    assert not shims.exists()


def test_usertree_with_bin_is_not_standalone(tmp_path: Path) -> None:
    env = tmp_path / ".texenv"
    texenv.init_usertree(env)
    (env / "bin" / "x86_64-linux").mkdir(parents=True)
    assert texenv.env_bin(env) is not None
    assert not cli._standalone(env)
    (env / "tlpkg/texlive.tlpdb").write_text("name x\n")  # no setting_usertree
    assert cli._standalone(env)


def test_tlmgr_project_keeps_using_tlmgr(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "texproject.toml").write_text(
        '[project]\nname = "p"\ninstall_method = "tlmgr"\ndependencies = ["foo"]\n'
    )
    (project / ".texenv/tlpkg").mkdir(parents=True)
    (project / ".texenv/tlpkg/texlive.tlpdb").write_text("name x\n")
    seen: list[list[str]] = []

    def fake_run(cmd):  # type: ignore[no-untyped-def]
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "tlmgr_path", "/bin/tlmgr")
    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "_ensure_latexmk", lambda: None)
    monkeypatch.setattr(cli, "filter_packages", lambda p, e, prog: ([], [], []))
    monkeypatch.setattr(cli, "_updmap_local", lambda e: None)
    monkeypatch.setattr(cli, "_update_lock", lambda *a: None)
    r = runner.invoke(cli.app, ["sync", "--no-update"])
    assert r.exit_code == 0, r.output
    assert any("tlmgr" in c[0] and "update" in c for c in seen)


# --- texlive_env = install / copy ---------------------------------------------------


def test_install_env_without_tlmgr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(
        texenv, "_fetch_installer", lambda base: _fake_installer(tmp_path)
    )
    monkeypatch.setattr(
        texenv.subprocess,
        "run",
        lambda cmd, **kw: _record(ran, cmd, tmp_path / "env"),
    )
    texenv.install_env(tmp_path / "env", "https://r/tlnet", tlmgr=False)
    assert len(ran) == 1 and ran[0][0] == "perl" and "--scheme=minimal" in ran[0]
    texenv.install_env(tmp_path / "env", "https://r/tlnet")
    assert any("tlmgr" in c[0] for c in ran[1:])  # the old behavior is still there


def _record(ran: list[list[str]], cmd: list[str], env: Path):  # type: ignore[no-untyped-def]
    ran.append(cmd)
    (env / "bin" / "x86_64-linux").mkdir(parents=True, exist_ok=True)
    return subprocess.CompletedProcess(cmd, 0)


def _fake_installer(tmp_path: Path) -> Path:
    import io
    import tarfile

    tarball = tmp_path / "install-tl-unx.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        data = b"#!/usr/bin/perl\n"
        info = tarfile.TarInfo("install-tl-20260101/install-tl")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return tarball


def test_create_env_install_uses_archive_for_latex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in (
        "PATH",
        "TEXMFHOME",
        "TEXMFVAR",
        "TEXMFCONFIG",
    ):  # _activate_env sets these
        monkeypatch.setenv(var, os.environ.get(var, ""))
    monkeypatch.setattr(cli, "tlmgr_path", None)
    monkeypatch.setattr(cli, "latexmk_path", None)
    tools.set_forbid_tlmgr(True)
    got: dict[str, object] = {}

    def fake_install_env(env_dir, repository=None, scheme="minimal", *, tlmgr=True):  # type: ignore[no-untyped-def]
        got["tlmgr"] = tlmgr
        (env_dir / "bin" / "x86_64-linux").mkdir(parents=True)
        (env_dir / "tlpkg").mkdir()
        (env_dir / "tlpkg/texlive.tlpdb").write_text("name 00texlive.config\n")

    def fake_archive(prog, repository, env_dir, **kw):  # type: ignore[no-untyped-def]
        got["roots"] = kw["roots"]
        got["standalone"] = kw["standalone"]

    monkeypatch.setattr(texenv, "install_env", fake_install_env)
    monkeypatch.setattr(cli, "_archive_install", fake_archive)
    monkeypatch.setattr(
        cli,
        "_tl",
        lambda: pytest.fail("tlmgr"),
    )
    cli._create_env("install", "https://r/tlnet")
    assert got == {"tlmgr": False, "roots": ["latex", "latexmk"], "standalone": True}


def test_copy_env_does_not_call_tlmgr() -> None:
    import inspect

    assert "tlmgr" not in inspect.getsource(texenv.create_env).replace(
        "tlmgr-managed", ""
    )


def test_build_reruns_full_after_previous_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def fake_captured(cmd, env, rc_input):
        # the generated rc says whether this is the --full run
        calls.append(bool(re.search(r"\$go_mode\s*=\s*1\b", rc_input or "")))
        if len(calls) == 1:
            return 12, "Latexmk: 'pdflatex' gave an error in previous invocation\n"
        return 0, "Latexmk: All targets (main.pdf) are up-to-date\n"

    monkeypatch.setattr(cli, "_run_captured", fake_captured)
    monkeypatch.setattr(cli, "_ensure_latexmk", lambda: None)
    monkeypatch.setattr(cli, "_check_type3", lambda config: None)
    r = runner.invoke(cli.app, ["build"])
    assert len(calls) == 2, r.output
    assert calls[0] is False and calls[1] is True, calls
    assert "rebuilding with --full" in r.output
    assert r.exit_code == 0, r.output


def test_all_warnings_flag_lists_everything(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diags = [
        cli.Diagnostic(kind="warning", message=f"w{i}", file="main.tex", line=i + 1)
        for i in range(20)
    ]
    config = cli.load_project_config(project / "texproject.toml")
    cli._print_diagnostics(diags, config)
    short = capsys.readouterr().out
    cli._print_diagnostics(diags, config, all_warnings=True)
    full = capsys.readouterr().out
    assert "5 more warnings" in short and "--all-warnings" in short
    assert "more warnings" not in full and "w19" in full
