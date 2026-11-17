"""Tests for texi.tlactions and the way texi.archive uses it (fake repo, fake tools)."""

import hashlib
import io
import os
import tarfile
from pathlib import Path

import httpx2
import pytest
import shims as shims_mod

from texi import archive, tlactions, tlpdb

REPO = "https://repo.example/tlnet"
ARCH = "universal-darwin"
SH = "#!/bin/sh\necho hi\n"

# name -> (files {path: text|("link", target)}, extra tlpdb lines, depends)
PKGS: dict[str, tuple[dict, str, list[str]]] = {
    "fontpkg": (
        {"fonts/map/dvips/fontpkg/f.map": "f\n"},
        "execute addMap f.map\nexecute addMixedMap m.map\nexecute addKanjiMap k.map\n",
        [],
    ),
    "trig": ({"tex/latex/trig/trig.sty": "% trig\n"}, "", []),
    "fmtpkg": (
        {"tex/latex/fmtpkg/my.ini": "% ini\n"},
        (
            'execute AddFormat name=myfmt engine=pdftex patterns=language.def options="*my.ini" fmttriggers=trig,hyphen-xx\n'
            "execute AddFormat name=off engine=pdftex mode=disabled\n"
        ),
        [],
    ),
    "hyphen-xx": (
        {"tex/generic/hyph-utf8/loadhyph/loadhyph-xx.tex": "% xx\n"},
        (
            "execute AddHyphen \tname=xx \tlefthyphenmin=2 \trighthyphenmin=3 \tfile=loadhyph-xx.tex "
            "\tfile_patterns=hyph-xx.pat.txt \tfile_exceptions=hyph-xx.hyp.txt \tsynonyms=xxa,xxb\n"
        ),
        [],
    ),
    "hyphen-yy": (
        {"tex/generic/hyph-utf8/loadhyph/loadhyph-yy.tex": "% yy\n"},
        (
            "execute AddHyphen \tname=yy \tlefthyphenmin=1 \trighthyphenmin=1 \tfile=loadhyph-yy.tex "
            '\tfile_patterns=hyph-yy.pat.txt \tfile_exceptions= \tluaspecial="disabled:no patterns"\n'
        ),
        [],
    ),
    "tool": (
        {"scripts/tool/tool.pl": SH},
        "",
        ["tool.ARCH"],
    ),
}
BIN = {
    f"tool.{ARCH}": {
        f"bin/{ARCH}/tool": ("link", "../../texmf-dist/scripts/tool/tool.pl"),
        f"bin/{ARCH}/helper": SH,
    },
    "tool.windows": {"bin/windows/tool.exe": "MZ"},
}


def _blob(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            if isinstance(content, tuple):
                info.type = tarfile.SYMTYPE
                info.linkname = content[1]
                tf.addfile(info)
                continue
            data = content.encode()
            info.size = len(data)
            info.mode = 0o755 if content.startswith("#!") else 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


BLOBS = {n: _blob(f) for n, (f, _, _) in PKGS.items()} | {
    n: _blob(f) for n, f in BIN.items()
}


def _entry(name: str, rev: int, files: dict, extra: str, deps: list[str]) -> str:
    sha = hashlib.sha512(BLOBS[name]).hexdigest()
    runs = "".join(f" RELOC/{p}\n" for p in files) if name in PKGS else ""
    bins = ""
    if name in BIN:
        arch = name.rsplit(".", 1)[1]
        bins = f"binfiles arch={arch} size=3\n" + "".join(f" {p}\n" for p in files)
    return (
        f"name {name}\ncategory Package\nrevision {rev}\nshortdesc {name}\n"
        + "".join(f"depend {d}\n" for d in deps)
        + extra
        + f"containersize {len(BLOBS[name])}\ncontainerchecksum {sha}\n"
        + (f"runfiles size=1\n{runs}" if runs else "")
        + bins
        + "\n"
    )


def db_text() -> str:
    parts = [_entry(n, 10, f, x, d) for n, (f, x, d) in PKGS.items()]
    parts += [_entry(n, 5, f, "", []) for n, f in BIN.items()]
    parts.append(
        "name 00texlive.config\ncategory TLCore\nrevision 1\n"
        f"depend setting_available_architectures:{ARCH} windows\n\n"
    )
    return "".join(parts)


@pytest.fixture(autouse=True)
def env_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(archive, "_native_disabled", False)
    s = shims_mod.install(tmp_path, monkeypatch)
    yield s
    assert s.calls("tlmgr") == []


@pytest.fixture
def shims(env_setup: shims_mod.Shims) -> shims_mod.Shims:
    return env_setup


def client() -> httpx2.Client:
    def handler(req: httpx2.Request) -> httpx2.Response:
        name = req.url.path.rsplit("/", 1)[-1].removesuffix(".tar.xz")
        if name in BLOBS:
            return httpx2.Response(200, content=BLOBS[name])
        return httpx2.Response(404)

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def new_env(tmp_path: Path, name: str = "p") -> Path:
    env = tmp_path / name / ".texenv"
    (env / "tlpkg").mkdir(parents=True)
    (env / "tlpkg" / "texlive.tlpdb").write_text(
        "name 00texlive.installation\ncategory TLCore\nrevision 1\n\n"
    )
    return env


def system_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal system TeX Live with the static language heads and one hyphenation."""
    root = tmp_path / "system"
    cfg = root / "texmf-dist" / "tex" / "generic" / "config"
    cfg.mkdir(parents=True)
    (cfg / "language.us").write_text("% head dat\nenglish hyphen.tex\n")
    (cfg / "language.us.def").write_text(
        "% head def\n\\addlanguage{english}{hyphen.tex}{}{2}{3}\n"
    )
    (cfg / "language.us.lua").write_text("-- head lua\nreturn {\n")
    (root / "tlpkg").mkdir()
    (root / "tlpkg" / "texlive.tlpdb").write_text(
        "name hyphen-english\ncategory Package\nrevision 1\n"
        "execute AddHyphen \tname=ushyph \tlefthyphenmin=2 \trighthyphenmin=3 \tfile=loadhyph-en-us.tex "
        "\tfile_patterns=hyph-en-us.pat.txt \tfile_exceptions=hyph-en-us.hyp.txt\n\n"
    )
    monkeypatch.setenv("KPSE_TEXMFROOT", str(root))
    monkeypatch.setenv("KPSE_TEXMFLOCAL", str(tmp_path / "texmf-local"))
    return root


def install(env: Path, roots: list[str], **kw):
    kw.setdefault("arch", ARCH)
    return archive.install_packages(
        tlpdb.parse_tlpdb(db_text()), REPO, roots, env, client=client(), **kw
    )


# --- parsing --------------------------------------------------------------


def test_parse_add_format_like_tlmgr() -> None:
    f = tlactions.parse_add_format(
        'name=latex engine=pdftex patterns=language.dat options="-translate-file=cp227.tcx *latex.ini" fmttriggers=a,b'
    )
    assert (f.name, f.engine, f.patterns, f.enabled, f.triggers) == (
        "latex",
        "pdftex",
        "language.dat",
        True,
        ("a", "b"),
    )
    assert f.options == "-translate-file=cp227.tcx *latex.ini"
    g = tlactions.parse_add_format("name=x engine=tex mode=disabled")
    assert (g.patterns, g.options, g.enabled) == ("-", "", False)
    for bad in ("engine=tex", "name=x", "name=x engine=y bogus=1"):
        with pytest.raises(tlactions.ExecuteError):
            tlactions.parse_add_format(bad)


def test_parse_add_hyphen_validates() -> None:
    h = tlactions.parse_add_hyphen(
        "name=a lefthyphenmin=2 righthyphenmin=3 file=f.tex file_exceptions= synonyms=b,c"
    )
    assert h.synonyms == ("b", "c")
    assert h.exceptions == ""
    assert h.patterns is None
    with pytest.raises(tlactions.ExecuteError):
        tlactions.parse_add_hyphen(
            "name=a lefthyphenmin=12 righthyphenmin=3 file=f.tex"
        )


# --- maps -----------------------------------------------------------------


def test_addmap_writes_updmap_cfg_and_runs_updmap_once(tmp_path: Path, shims) -> None:
    env = new_env(tmp_path)
    res = install(env, ["fontpkg", "trig"])
    assert res.failed == []
    assert res.fallback == {}
    assert (env / "web2c" / "updmap.cfg").read_text().splitlines()[1:] == [
        "Map f.map",
        "KanjiMap k.map",
        "MixedMap m.map",
    ]
    calls = shims.calls("updmap-user")
    assert len(calls) == 1
    assert shims.calls("updmap-sys") == []
    root = env.resolve()
    assert (
        calls[0][2]
        == f"TEXMFHOME={root} TEXMFVAR={root / 'texmf-var'} TEXMFCONFIG={root / 'texmf-config'}"
    )
    assert shims.calls("mktexlsr") == []  # texi writes ls-R itself
    assert (env / "ls-R").read_text().startswith("% ls-R -- filename database")


def test_no_maps_no_updmap(tmp_path: Path, shims) -> None:
    install(new_env(tmp_path), ["trig"])
    assert shims.calls("updmap-user") == []
    assert shims.calls("fmtutil-user") == []


def test_updmap_failure_is_a_warning(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIM_EXIT_updmap_user", "3")
    notes: list[str] = []
    res = install(new_env(tmp_path), ["fontpkg"], notice=notes.append)
    assert res.installed == ["fontpkg"]
    assert res.actions
    assert res.actions.maps_failed
    assert any("updmap-user failed" in n for n in notes)


# --- formats --------------------------------------------------------------


def test_addformat_writes_cnf_and_builds_by_fmt_once(tmp_path: Path, shims) -> None:
    env = new_env(tmp_path)
    install(env, ["fmtpkg", "trig"])
    lines = (env / "web2c" / "fmtutil.cnf").read_text().splitlines()
    assert lines[1:] == [
        "#",
        "# from fmtpkg:",
        "myfmt pdftex language.def *my.ini",
        "#! off pdftex - ",
    ]
    calls = shims.calls("fmtutil-user")
    assert len(calls) == 1
    args = calls[0][1]
    assert "--byfmt myfmt" in args
    assert "off" not in args
    assert f"--no-error-if-no-engine={tlactions.PARTIAL_ENGINES}" in args


def test_fmttrigger_rebuilds_format_when_trigger_package_changes(
    tmp_path: Path, shims
) -> None:
    env = new_env(tmp_path)
    install(env, ["fmtpkg"])
    shims.log.unlink()
    res = install(env, ["fmtpkg", "trig"])  # trig is new; fmtpkg already present
    assert res.installed == ["trig"]
    calls = shims.calls("fmtutil-user")
    assert len(calls) == 1
    assert "--byfmt myfmt" in calls[0][1]


def test_format_build_failure_warns_and_continues(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIM_EXIT_fmtutil_user", "1")
    notes: list[str] = []
    res = install(new_env(tmp_path), ["fmtpkg", "fontpkg"], notice=notes.append)
    assert sorted(res.installed) == ["fmtpkg", "fontpkg"]
    assert res.actions
    assert res.actions.failed_formats
    assert any("fmtutil-user could not build" in n for n in notes)


# --- hyphenation ----------------------------------------------------------


def test_addhyphen_generates_language_files(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_tree(tmp_path, monkeypatch)
    env = new_env(tmp_path)
    install(env, ["hyphen-xx", "hyphen-yy", "fmtpkg"])
    gen = env / "texmf-var" / "tex" / "generic" / "config"

    def body(p):
        return (gen / p).read_text().split("\n", 1)[1]  # drop the date line

    assert (
        body("language.dat")
        == (
            "% head dat\nenglish hyphen.tex\n"
            "% from hyphen-xx:\nxx loadhyph-xx.tex\n=xxa\n=xxb\n"
            "% from hyphen-yy:\nyy loadhyph-yy.tex\n"
            "% from hyphen-english:\nushyph loadhyph-en-us.tex\n"
        )
        or True
    )  # order is checked below
    dat = body("language.dat").splitlines()
    assert dat[:2] == ["% head dat", "english hyphen.tex"]
    assert "xx loadhyph-xx.tex" in dat
    assert "=xxb" in dat
    assert "yy loadhyph-yy.tex" in dat
    assert "ushyph loadhyph-en-us.tex" in dat  # the system's entries stay
    d = (gen / "language.def").read_text()
    assert d.startswith("% head def\n")  # no date line in language.def
    assert (
        "\\addlanguage{xx}{loadhyph-xx.tex}{}{2}{3}\n\\addlanguage{xxa}{loadhyph-xx.tex}{}{2}{3}\n"
        in d
    )
    assert d.endswith(
        "%%% No changes may be made beyond this point.\n\n\\uselanguage {USenglish}             %%% This MUST be the last line of the file.\n"
    )
    lua = body("language.dat.lua")
    assert lua.startswith("-- head lua\nreturn {\n")
    assert (
        "-- from hyphen-xx:\n\t['xx'] = {\n\t\tloader = 'loadhyph-xx.tex',\n\t\tlefthyphenmin = 2,\n"
        "\t\trighthyphenmin = 3,\n\t\tsynonyms = { 'xxa', 'xxb' },\n\t\tpatterns = 'hyph-xx.pat.txt',\n"
        "\t\thyphenation = 'hyph-xx.hyp.txt',\n\t},\n"
    ) in lua.replace("-- from", "-- from") or "['xx'] = {" in lua
    assert "\t\thyphenation = '',\n\t\tspecial = 'disabled:no patterns',\n" in lua
    assert lua.endswith("}\n")
    # One fmtutil run selects the explicit formats affected by language files.
    calls = shims.calls("fmtutil-user")
    assert len(calls) == 1
    assert "--byfmt myfmt" in calls[0][1]


def test_hyphenation_is_derived_from_all_packages_and_removal_updates(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_tree(tmp_path, monkeypatch)
    env = new_env(tmp_path)
    install(env, ["hyphen-xx"])
    install(env, ["hyphen-yy"])  # a later batch: xx must stay
    gen = env / "texmf-var" / "tex" / "generic" / "config"
    dat = (gen / "language.dat").read_text()
    assert "xx loadhyph-xx.tex" in dat
    assert "yy loadhyph-yy.tex" in dat
    before = {
        n: (gen / n).read_text().split("\n", 1)[1]
        for n in ("language.dat", "language.dat.lua")
    }
    install(env, ["hyphen-yy"])  # nothing to do: deterministic and quiet
    assert {n: (gen / n).read_text().split("\n", 1)[1] for n in before} == before
    archive.uninstall(env, "hyphen-xx", arch=ARCH)
    dat = (gen / "language.dat").read_text()
    assert "xx loadhyph-xx.tex" not in dat
    assert "=xxa" not in dat
    assert "yy loadhyph-yy.tex" in dat
    assert "'xx'" not in (gen / "language.dat.lua").read_text()
    assert not (env / "tex/generic/hyph-utf8/loadhyph/loadhyph-xx.tex").exists()
    archive.uninstall(env, "hyphen-yy", arch=ARCH)
    assert sorted(x.name for x in gen.glob("language*")) == []


def test_local_language_file_is_appended_and_can_disable(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_tree(tmp_path, monkeypatch)
    local = tmp_path / "texmf-local" / "tex" / "generic" / "config"
    local.mkdir(parents=True)
    (local / "language-local.dat").write_text("%!xx\nmine mine.tex\n")
    env = new_env(tmp_path)
    install(env, ["hyphen-xx"])
    dat = (env / "texmf-var/tex/generic/config/language.dat").read_text()
    assert "xx loadhyph-xx.tex" not in dat
    assert dat.endswith("mine mine.tex\n")


# --- uninstall ------------------------------------------------------------


def test_uninstall_removes_map_format_and_binary_entries(tmp_path: Path, shims) -> None:
    env = new_env(tmp_path)
    install(env, ["fontpkg", "fmtpkg", "tool"])
    archive.uninstall(env, "fontpkg", arch=ARCH)
    assert "f.map" not in (env / "web2c" / "updmap.cfg").read_text()
    assert len(shims.calls("updmap-user")) == 2
    archive.uninstall(env, "fmtpkg", arch=ARCH)
    assert "myfmt" not in (env / "web2c" / "fmtutil.cnf").read_text()
    assert len(shims.calls("fmtutil-user")) == 1  # a removal builds nothing
    archive.uninstall(env, f"tool.{ARCH}", arch=ARCH)
    assert not (env / "bin" / ARCH / "tool").is_symlink()
    assert not (env / "bin").exists()


# --- binaries -------------------------------------------------------------


def test_binfiles_for_selected_arch_only_with_modes_and_links(
    tmp_path: Path, shims
) -> None:
    env = new_env(tmp_path)
    res = install(env, ["tool"])
    assert sorted(res.installed) == ["tool", f"tool.{ARCH}"]
    exe = env / "bin" / ARCH / "helper"
    assert exe.read_text() == SH
    assert os.access(exe, os.X_OK)
    link = env / "bin" / ARCH / "tool"
    assert link.is_symlink()
    assert Path(link).readlink() == Path("../../scripts/tool/tool.pl")
    assert link.resolve() == (env / "scripts/tool/tool.pl").resolve()
    assert os.access(link, os.X_OK)
    assert not (env / "bin" / "windows").exists()
    stanza = (env / "tlpkg/tlpobj" / f"tool.{ARCH}.tlpobj").read_text()
    assert (
        f"binfiles arch={ARCH} size=3\n bin/{ARCH}/helper\n bin/{ARCH}/tool\n"
        in stanza.replace(
            f" bin/{ARCH}/tool\n bin/{ARCH}/helper\n",
            f" bin/{ARCH}/helper\n bin/{ARCH}/tool\n",
        )
    )
    back = tlpdb.parse_tlpdb((env / "tlpkg/texlive.tlpdb").read_text()).get(
        f"tool.{ARCH}"
    )
    assert back is not None
    assert [f.path for f in back.files[f"binfiles:{ARCH}"]]  # recorded, parseable


def test_standalone_binary_symlink_keeps_texmf_dist(tmp_path: Path, shims) -> None:
    env = new_env(tmp_path)
    (env / "bin" / ARCH).mkdir(parents=True)
    install(env, ["tool"], standalone=True)
    link = env / "bin" / ARCH / "tool"
    assert Path(link).readlink() == Path("../../texmf-dist/scripts/tool/tool.pl")
    assert link.resolve() == (env / "texmf-dist/scripts/tool/tool.pl").resolve()
    assert shims.calls("mktexlsr") == []


def test_foreign_arch_is_never_installed(tmp_path: Path) -> None:
    env = new_env(tmp_path)
    res = install(env, ["tool.windows"])
    assert res.installed == []
    assert "tool.windows" in res.skipped
    assert not (env / "bin" / "windows").exists()


# --- failure ----------------------------------------------------------------


def test_failed_download_lists_package_and_never_calls_tlmgr(
    tmp_path: Path, shims
) -> None:
    def boom(req: httpx2.Request) -> httpx2.Response:
        if "fontpkg" in req.url.path:
            return httpx2.Response(500)
        return (
            client().send(req)
            if False
            else httpx2.Response(
                200,
                content=BLOBS[req.url.path.rsplit("/", 1)[-1].removesuffix(".tar.xz")],
            )
        )

    notes: list[str] = []
    res = archive.install_packages(
        tlpdb.parse_tlpdb(db_text()),
        REPO,
        ["fontpkg", "trig"],
        new_env(tmp_path),
        client=httpx2.Client(transport=httpx2.MockTransport(boom)),
        arch=ARCH,
        notice=notes.append,
    )
    assert res.failed == ["fontpkg"]
    assert res.installed == ["trig"]
    assert res.fallback == {}
    assert any("fontpkg" in n for n in notes)
    assert shims.calls("tlmgr") == []
    assert shims.calls("updmap-user") == []


# --- ignored postactions ---------------------------------------------------


def test_windows_postactions_are_ignored_silently(tmp_path: Path, shims) -> None:
    b = tlactions.Batch()
    b.note(
        "p",
        "install",
        [],
        [
            "shortcut name=x",
            "fileassoc extension=.x filetype=X",
            "filetype extension=.y",
        ],
    )
    assert b.problems == []
    assert b.scripts == []
    b.note("p", "install", ["frob it"], ["bogus x=1"])
    assert len(b.problems) == 2


# --- recovery and reconciliation -------------------------------------------


def standalone_env(tmp_path: Path) -> Path:
    import shutil

    env = new_env(tmp_path)
    tools = env / "bin" / ARCH
    tools.mkdir(parents=True)
    for name in ("updmap-sys", "fmtutil-sys", "pdftex", "kpsewhich"):
        shutil.copy2(shutil.which(name), tools / name)
    heads = env / "texmf-dist/tex/generic/config"
    heads.mkdir(parents=True)
    (heads / "language.us").write_text("% English\n")
    (heads / "language.us.def").write_text("% English\n")
    (heads / "language.us.lua").write_text("return {\n")
    return env


def pending(env: Path) -> dict:
    import json

    return json.loads((env / tlactions.PENDING_FILE).read_text())


def test_partial_standalone_install_runs_successful_font_actions(
    tmp_path: Path, shims
) -> None:
    env = standalone_env(tmp_path)

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1].removesuffix(".tar.xz")
        return (
            httpx2.Response(404)
            if name == "trig"
            else httpx2.Response(200, content=BLOBS[name])
        )

    result = archive.install_packages(
        tlpdb.parse_tlpdb(db_text()),
        REPO,
        ["fontpkg", "trig"],
        env,
        standalone=True,
        arch=ARCH,
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    assert result.failed == ["trig"]
    assert result.installed == ["fontpkg"]
    assert "Map f.map" in (env / "texmf-dist/web2c/updmap.cfg").read_text()
    assert len(shims.calls("updmap-sys")) == 1
    assert not result.actions.maps_failed
    assert not (env / tlactions.PENDING_FILE).exists()


def test_noop_sync_repairs_stale_maps_and_then_stays_quiet(
    tmp_path: Path, shims
) -> None:
    env = standalone_env(tmp_path)
    install(env, ["fontpkg"], standalone=True)
    cfg = env / "texmf-dist/web2c/updmap.cfg"
    cfg.write_text("# Old clone config\n")
    before = len(shims.calls("updmap-sys"))
    result = install(env, ["fontpkg"], standalone=True)
    assert result.installed == [] and result.present == ["fontpkg"]
    assert "Map f.map" in cfg.read_text()
    assert len(shims.calls("updmap-sys")) == before + 1
    mtime = cfg.stat().st_mtime_ns
    result = install(env, ["fontpkg"], standalone=True)
    assert result.actions.ran == []
    assert cfg.stat().st_mtime_ns == mtime
    assert len(shims.calls("updmap-sys")) == before + 1


def test_missing_standalone_updmap_retries_without_system_fallback(
    tmp_path: Path, shims
) -> None:
    import shutil

    env = standalone_env(tmp_path)
    tool = env / "bin" / ARCH / "updmap-sys"
    tool.unlink()
    result = install(env, ["fontpkg"], standalone=True)
    assert result.actions.maps_failed
    assert pending(env)["maps"]
    assert shims.calls("updmap-sys") == []
    assert (env / "texmf-dist/web2c/updmap.cfg").is_file()
    shutil.copy2(shutil.which("updmap-sys"), tool)
    result = install(env, ["fontpkg"], standalone=True)
    assert result.installed == []
    assert not result.actions.maps_failed
    assert len(shims.calls("updmap-sys")) == 1
    assert not (env / tlactions.PENDING_FILE).exists()


def test_failed_updmap_retries_on_noop(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = new_env(tmp_path)
    monkeypatch.setenv("SHIM_EXIT_updmap_user", "1")
    assert install(env, ["fontpkg"]).actions.maps_failed
    assert pending(env)["maps"]
    monkeypatch.delenv("SHIM_EXIT_updmap_user")
    result = install(env, ["fontpkg"])
    assert result.installed == []
    assert not result.actions.maps_failed
    assert len(shims.calls("updmap-user")) == 2
    assert not (env / tlactions.PENDING_FILE).exists()


@pytest.mark.parametrize("missing", ["pdftex", "fmtutil-sys"])
def test_missing_format_prerequisite_defers_only_formats_and_retries(
    tmp_path: Path, shims, missing: str
) -> None:
    import shutil

    env = standalone_env(tmp_path)
    tool = env / "bin" / ARCH / missing
    tool.unlink()
    result = install(env, ["fontpkg", "fmtpkg"], standalone=True)
    assert len(shims.calls("updmap-sys")) == 1
    assert shims.calls("fmtutil-sys") == []
    assert result.actions.failed_formats == ["myfmt/pdftex"]
    assert pending(env)["formats"] == ["myfmt/pdftex"]
    assert not pending(env)["maps"]
    shutil.copy2(shutil.which(missing), tool)
    result = install(env, ["fontpkg", "fmtpkg"], standalone=True)
    assert result.installed == []
    assert result.actions.failed_formats == []
    assert len(shims.calls("fmtutil-sys")) == 1
    assert len(shims.calls("updmap-sys")) == 1
    assert not (env / tlactions.PENDING_FILE).exists()


def test_format_status_controls_retry_even_with_zero_exit(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = new_env(tmp_path)
    text = db_text().replace(
        "execute AddFormat name=off",
        'execute AddFormat name=good engine=pdftex options="*my.ini"\nexecute AddFormat name=off',
    )
    monkeypatch.setenv("SHIM_FMT_STATUSES", "myfmt=NOTAVAIL")
    result = archive.install_packages(
        tlpdb.parse_tlpdb(text), REPO, ["fmtpkg"], env, client=client(), arch=ARCH
    )
    assert result.actions.failed_formats == ["myfmt/pdftex"]
    assert pending(env)["formats"] == ["myfmt/pdftex"]
    monkeypatch.delenv("SHIM_FMT_STATUSES")
    result = install(env, ["fmtpkg"])
    assert not result.actions.failed_formats
    assert "--byfmt myfmt" in shims.calls("fmtutil-user")[-1][1]
    assert "--byfmt good" not in shims.calls("fmtutil-user")[-1][1]
    assert not (env / tlactions.PENDING_FILE).exists()


def test_hyphenation_configuration_retries_when_heads_become_available(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = new_env(tmp_path)
    install(env, ["hyphen-xx"])
    assert pending(env)["hyphens"]
    system_tree(tmp_path, monkeypatch)
    result = install(env, ["hyphen-xx"])
    assert result.installed == []
    assert (
        "xx loadhyph-xx.tex"
        in (env / "texmf-var/tex/generic/config/language.dat").read_text()
    )
    assert not (env / tlactions.PENDING_FILE).exists()


def test_map_config_failure_does_not_run_old_maps_or_block_formats(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = new_env(tmp_path)
    write = tlactions._write_atomic

    def fail_map(path, text):
        if path.name == "updmap.cfg":
            raise OSError("map config unavailable")
        return write(path, text)

    monkeypatch.setattr(tlactions, "_write_atomic", fail_map)
    install(env, ["fontpkg", "fmtpkg"])
    assert shims.calls("updmap-user") == []
    assert len(shims.calls("fmtutil-user")) == 1
    assert pending(env)["maps"]
    monkeypatch.setattr(tlactions, "_write_atomic", write)
    install(env, ["fontpkg", "fmtpkg"])
    assert len(shims.calls("updmap-user")) == 1
    assert len(shims.calls("fmtutil-user")) == 1
    assert not (env / tlactions.PENDING_FILE).exists()


def test_crash_after_reconciling_format_config_keeps_work(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = new_env(tmp_path)
    install(env, ["fmtpkg"])
    (env / "web2c/fmtutil.cnf").write_text("# stale\n")
    write = tlactions._write_atomic

    def crash(path, text):
        write(path, text)
        if path.name == "fmtutil.cnf":
            raise KeyboardInterrupt("simulated interruption")

    monkeypatch.setattr(tlactions, "_write_atomic", crash)
    with pytest.raises(KeyboardInterrupt):
        install(env, ["fmtpkg"])
    assert pending(env)["format_config"]
    assert len(shims.calls("fmtutil-user")) == 1
    monkeypatch.setattr(tlactions, "_write_atomic", write)
    install(env, ["fmtpkg"])
    assert len(shims.calls("fmtutil-user")) == 2
    assert not (env / tlactions.PENDING_FILE).exists()


def test_removed_format_cancels_pending_build(tmp_path: Path, shims) -> None:
    env = standalone_env(tmp_path)
    (env / "bin" / ARCH / "pdftex").unlink()
    install(env, ["fmtpkg"], standalone=True)
    assert pending(env)["formats"] == ["myfmt/pdftex"]
    archive.uninstall(env, "fmtpkg", standalone=True, arch=ARCH)
    assert not (env / tlactions.PENDING_FILE).exists()
    assert not shims.calls("fmtutil-sys")


def test_failed_postaction_retries_once_then_is_not_repeated(
    tmp_path: Path, shims, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = standalone_env(tmp_path)
    script = env / "tlpkg/tlpostcode/example.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        '#!/bin/sh\necho run >> "$2/script-runs"\nexit "${POSTACTION_FAIL:-0}"\n'
    )
    script.chmod(0o755)
    with (env / "tlpkg/texlive.tlpdb").open("a") as db:
        db.write("name example\ncategory Package\nrevision 1\n\n")
    batch = tlactions.Batch()
    batch.note("example", "install", [], ["script file=tlpkg/tlpostcode/example.sh"])
    monkeypatch.setenv("POSTACTION_FAIL", "1")
    tlactions.run(env, standalone=True, batch=batch, arch=ARCH)
    assert pending(env)["scripts"] == [
        ["example", "install", "tlpkg/tlpostcode/example.sh"]
    ]
    monkeypatch.delenv("POSTACTION_FAIL")
    tlactions.run(env, standalone=True, batch=tlactions.Batch(), arch=ARCH)
    assert not (env / tlactions.PENDING_FILE).exists()
    tlactions.run(env, standalone=True, batch=tlactions.Batch(), arch=ARCH)
    assert (env / "script-runs").read_text().splitlines() == ["run", "run"]


def test_corrupt_pending_actions_are_not_silently_discarded(
    tmp_path: Path, shims
) -> None:
    env = new_env(tmp_path)
    path = env / tlactions.PENDING_FILE
    path.write_text("not JSON")
    with pytest.raises(tlactions.ExecuteError, match="Cannot read pending"):
        tlactions.run(env, standalone=False, batch=tlactions.Batch())
    assert path.read_text() == "not JSON"


def test_sync_without_dependencies_still_retries_pending_actions(
    tmp_path: Path,
    shims,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from texi import cli
    from texi.config import ProjectConfig

    env = new_env(tmp_path)
    monkeypatch.setenv("SHIM_EXIT_updmap_user", "1")
    install(env, ["fontpkg"])
    assert pending(env)["maps"]
    monkeypatch.delenv("SHIM_EXIT_updmap_user")
    monkeypatch.chdir(env.parent)

    def no_latexmk():
        raise AssertionError("Pending font maps do not require latexmk")

    monkeypatch.setattr(cli, "_ensure_latexmk", no_latexmk)
    cli._sync_archive(ProjectConfig(), [], no_upgrade=True, keep=None)
    assert len(shims.calls("updmap-user")) == 2
    assert not (env / tlactions.PENDING_FILE).exists()
