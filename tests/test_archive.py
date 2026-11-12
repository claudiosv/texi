"""Tests for texi.archive using a fake repository (no network)."""

import hashlib
import io
import os
import tarfile
import time
from pathlib import Path
from typing import Never

import httpx
import pytest
import shims as shims_mod

from texi import archive, tlpdb


def _container(files: dict[str, str], name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for path, text in {**files, f"tlpkg/tlpobj/{name}.tlpobj": "x"}.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


REPO = "https://repo.example/tlnet"
FILES = {
    "foo": {
        "tex/latex/foo/foo.sty": "% foo\n",
        "tex/latex/foo/foo-extra.sty": "% extra\n",
        "fonts/tfm/public/foo/foo.tfm": "tfm",
    },
    "bar": {"tex/latex/bar/bar.sty": "% bar\n"},
    "baz": {"tex/latex/baz/baz.sty": "% baz\n"},
}
BLOBS = {n: _container(f, n) for n, f in FILES.items()}


def _entry(name: str, rev: int, deps: list[str], extra: str = "") -> str:
    files = "".join(f" RELOC/{p}\n" for p in FILES.get(name, {}))
    sha = hashlib.sha512(BLOBS.get(name, b"")).hexdigest()
    return (
        f"name {name}\ncategory Package\nrevision {rev}\nshortdesc {name} pkg\n"
        f"longdesc A long description of {name}.\n"
        + "".join(f"depend {d}\n" for d in deps)
        + extra
        + f"containersize {len(BLOBS.get(name, b''))}\ncontainerchecksum {sha}\n"
        f"doccontainerchecksum 00\nrelocated 1\n"
        f"runfiles size={len(FILES.get(name, {}))}\n{files}"
        f"catalogue-license lppl1.3\n\n"
    )


def _db_text(foo_rev: int = 100) -> str:
    return (
        _entry("foo", foo_rev, ["bar", "collection-x", "sysdep", "bin.ARCH", "baz"])
        + _entry("bar", 5, [])
        + _entry("baz", 7, [], "execute addMap baz.map\n")
        + "name collection-x\ncategory Collection\nrevision 1\ndepend foo\n\n"
        + "name sysdep\ncategory Package\nrevision 1\n\n"
    )


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(archive, "_native_disabled", False)
    return tmp_path / "cache" / "texi" / "archive"


@pytest.fixture(autouse=True)
def shims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> shims_mod.Shims:
    """Fake TeX tools; a tlmgr that fails the test if it is ever started."""
    return shims_mod.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def never_tlmgr(shims: shims_mod.Shims):
    yield
    assert shims.calls("tlmgr") == []


def make_client(hits: list[str] | None = None, corrupt: bool = False) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1].removesuffix(".tar.xz")
        if hits is not None:
            hits.append(name)
        if name not in BLOBS:
            return httpx.Response(404)
        data = BLOBS[name]
        return httpx.Response(200, content=b"junk" + data if corrupt else data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def new_env(tmp_path: Path, name: str = "p") -> Path:
    env = tmp_path / name / ".texenv"
    (env / "tlpkg").mkdir(parents=True)
    (env / "tlpkg" / "texlive.tlpdb").write_text(
        "name 00texlive.installation\ncategory TLCore\nrevision 1\n\n"
    )
    return env


def run(db, env, roots=("foo",), **kw):
    kw.setdefault("client", make_client())
    return archive.install_packages(db, REPO, list(roots), env, **kw)


# --- ids and store -------------------------------------------------------


def test_id_deterministic_and_sensitive() -> None:
    a = archive.archive_id("foo", "1", "abcdef0123")
    assert a == "foo-1-abcdef0"
    assert a == archive.archive_id("foo", "1", "abcdef0123")
    assert a != archive.archive_id("foo", "2", "abcdef0123")
    assert a != archive.archive_id("foo", "1", "abcdef9123")
    assert a != archive.archive_id("bar", "1", "abcdef0123")


def test_fetch_stores_package_and_metadata(store: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    pkg = db.get("bar")
    entry, fresh = archive.fetch(REPO, pkg, client=make_client())
    assert fresh
    assert entry.path.parent == store
    assert (store / entry.id / "bar" / "tex/latex/bar/bar.sty").read_text() == "% bar\n"
    meta = (store / entry.id / "texi-archive.json").read_text()
    for key in (
        "schema",
        "bar",
        "5",
        REPO,
        "files",
        "created",
        pkg.first("containerchecksum"),
    ):
        assert str(key) in meta
    assert [p.name for p in store.iterdir()] == [entry.id]  # no temp leftovers


def test_checksum_mismatch_rejected_and_nothing_left(store: Path) -> None:
    pkg = tlpdb.parse_tlpdb(_db_text()).get("bar")
    with pytest.raises(archive.ArchiveError, match="sha512"):
        archive.fetch(REPO, pkg, client=make_client(corrupt=True))
    assert list(store.iterdir()) == []


def test_http_error_leaves_nothing(store: Path) -> None:
    pkg = tlpdb.parse_tlpdb(_db_text()).get("bar")

    def boom(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(archive.ArchiveError):
        archive.fetch(
            REPO, pkg, client=httpx.Client(transport=httpx.MockTransport(boom))
        )
    assert list(store.iterdir()) == []


def test_extract_is_atomic(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tlpdb.parse_tlpdb(_db_text()).get("bar")

    def crash(*a, **k) -> Never:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(tarfile.TarFile, "extractall", crash)
    with pytest.raises(archive.ArchiveError):
        archive.fetch(REPO, pkg, client=make_client())
    assert list(store.iterdir()) == []
    assert archive.lookup(REPO, pkg) is None


def test_offline_reuse(store: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    archive.fetch(REPO, db.get("bar"), client=make_client())

    def offline(req: httpx.Request) -> httpx.Response:
        msg = "offline"
        raise httpx.ConnectError(msg)

    client = httpx.Client(transport=httpx.MockTransport(offline))
    entry, fresh = archive.fetch(REPO, db.get("bar"), client=client)
    assert not fresh
    assert archive.lookup(REPO, db.get("bar")) is not None
    assert entry.package_dir.is_dir()


def test_new_revision_gets_new_dir(store: Path) -> None:
    a = archive.archive_id("foo", "100", "x")
    b = archive.archive_id("foo", "101", "x")
    assert a != b


def test_file_repository(store: Path, tmp_path: Path) -> None:
    pkg = tlpdb.parse_tlpdb(_db_text()).get("bar")
    repo = tmp_path / "repo"
    (repo / "archive").mkdir(parents=True)
    (repo / "archive" / "bar.tar.xz").write_bytes(BLOBS["bar"])
    entry, fresh = archive.fetch(repo.as_uri(), pkg)
    assert fresh
    assert (entry.package_dir / "tex/latex/bar/bar.sty").exists()


# --- install -------------------------------------------------------------


def test_usermode_layout_closure_and_actions(tmp_path: Path, shims) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    res = run(db, env, system_installed=lambda names: {"sysdep"} & set(names))
    assert sorted(res.installed) == ["bar", "baz", "foo"]
    assert sorted(res.downloaded) == ["bar", "baz", "foo"]
    assert (env / "tex/latex/foo/foo.sty").read_text() == "% foo\n"
    assert (env / "tex/latex/bar/bar.sty").exists()
    assert (env / "fonts/tfm/public/foo/foo.tfm").exists()
    assert not (env / "tlpkg" / "tlpobj" / "tlpkg").exists()
    # execute lines are handled here; collections, system and .ARCH packages are skipped
    assert res.fallback == {}
    assert res.failed == []
    assert (env / "tex/latex/baz/baz.sty").exists()
    assert "execute addMap baz.map" in (env / "tlpkg/tlpobj/baz.tlpobj").read_text()
    assert "Map baz.map\n" in (env / "web2c/updmap.cfg").read_text()
    assert len(shims.calls("updmap-user")) == 1


def test_standalone_layout(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    run(db, env, roots=["bar"], standalone=True)
    assert (env / "texmf-dist/tex/latex/bar/bar.sty").exists()
    text = (env / "tlpkg/tlpobj/bar.tlpobj").read_text()
    assert " texmf-dist/tex/latex/bar/bar.sty" in text


def test_bookkeeping_parses(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    run(db, env, roots=["foo"])
    out = tlpdb.parse_tlpdb((env / "tlpkg" / "texlive.tlpdb").read_text())
    assert set(out.names()) >= {"00texlive.installation", "foo", "bar"}
    foo = out.get("foo")
    assert foo.revision == "100"
    assert foo.category == "Package"
    assert foo.depends[0] == "bar"
    assert [f.path for f in foo.files["runfiles"]] == [
        "tex/latex/foo/foo.sty",
        "tex/latex/foo/foo-extra.sty",
        "fonts/tfm/public/foo/foo.tfm",
    ]
    assert foo.first("containerchecksum") == db.get("foo").first("containerchecksum")
    assert "relocated" not in foo.fields
    obj = tlpdb.parse_tlpdb((env / "tlpkg/tlpobj/foo.tlpobj").read_text()).get("foo")
    assert obj.revision == "100"
    assert archive.installed_revision(env, "foo") == 100
    assert archive.managed(env)["foo"]["revision"] == "100"
    from texi.cli import _usertree_packages

    assert {"foo", "bar"} <= _usertree_packages(env)


def test_second_project_downloads_nothing(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    run(db, new_env(tmp_path, "a"))
    hits: list[str] = []
    res = run(db, new_env(tmp_path, "b"), client=make_client(hits))
    assert hits == []
    assert res.downloaded == []
    assert res.cached == len(res.installed) == 3


def test_merge_into_shared_dirs_and_reclone_idempotent(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    run(db, env, roots=["bar", "foo"])
    shared = env / "tex" / "latex"
    assert sorted(p.name for p in shared.iterdir()) == ["bar", "baz", "foo"]
    archive_tree = archive.Tree(env, standalone=False)
    entry = archive.lookup(REPO, db.get("foo"))
    n = archive_tree.install(db.get("foo"), entry)
    archive_tree.flush()
    assert n == 3
    assert sorted(p.name for p in shared.iterdir()) == ["bar", "baz", "foo"]
    names = list(tlpdb.parse_tlpdb((env / "tlpkg/texlive.tlpdb").read_text()).names())
    assert names.count("foo") == 1


def test_edit_does_not_change_store(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    run(db, env, roots=["bar"])
    entry = archive.lookup(REPO, db.get("bar"))
    clone = env / "tex/latex/bar/bar.sty"
    stored = entry.package_dir / "tex/latex/bar/bar.sty"
    assert clone.stat().st_ino != stored.stat().st_ino
    with clone.open("a") as f:
        f.write("edited\n")
    assert stored.read_text() == "% bar\n"


def test_copy_fallback_when_clone_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def nope(src, dst) -> Never:
        raise OSError(18, "cross-device")

    monkeypatch.setattr(archive, "_clone_native", nope)
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    run(db, env, roots=["bar"])
    assert (env / "tex/latex/bar/bar.sty").read_text() == "% bar\n"
    entry = archive.lookup(REPO, db.get("bar"))
    stored = entry.package_dir / "tex/latex/bar/bar.sty"
    assert (env / "tex/latex/bar/bar.sty").stat().st_ino != stored.stat().st_ino


def test_native_clone_is_not_a_hardlink(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.write_text("a")
    dst = tmp_path / "sub" / "d"
    archive.clone_file(src, dst)
    assert dst.read_text() == "a"
    assert Path(dst).stat().st_ino != Path(src).stat().st_ino
    archive.clone_file(src, dst)  # replacing works


def test_uninstall_removes_exactly_recorded_files(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    run(db, env, roots=["foo", "bar"])
    stray = env / "tex/latex/foo/user-note.txt"
    stray.write_text("mine")
    removed = archive.uninstall(env, "foo")
    assert sorted(p.name for p in removed) == ["foo-extra.sty", "foo.sty", "foo.tfm"]
    assert stray.exists()  # unrecorded file stays (and so does its dir)
    assert (env / "tex/latex/bar/bar.sty").exists()
    assert not (env / "fonts").exists()  # emptied dirs pruned
    assert not (env / "tlpkg/tlpobj/foo.tlpobj").exists()
    names = tlpdb.parse_tlpdb((env / "tlpkg/texlive.tlpdb").read_text()).names()
    assert "foo" not in names
    assert "bar" in names
    assert "foo" not in archive.managed(env)


def test_update_replaces_old_revision(tmp_path: Path) -> None:
    env = new_env(tmp_path)
    run(tlpdb.parse_tlpdb(_db_text(100)), env, roots=["bar"])
    # same package, newer revision in the index
    new_text = _db_text().replace("revision 5\n", "revision 6\n", 1)
    new_text = new_text.replace(
        " RELOC/tex/latex/bar/bar.sty\n", " RELOC/tex/latex/bar/bar.sty\n"
    )
    db2 = tlpdb.parse_tlpdb(new_text)
    res = run(db2, env, roots=["bar"])
    assert res.updated == ["bar"]
    assert archive.installed_revision(env, "bar") == 6
    assert archive.managed(env)["bar"]["revision"] == "6"
    res = run(db2, env, roots=["bar"])  # now current
    assert res.installed == []
    assert res.present == ["bar"]
    res = run(
        tlpdb.parse_tlpdb(new_text.replace("revision 6", "revision 7", 1)),
        env,
        roots=["bar"],
        update=False,
    )
    assert res.installed == []
    assert archive.installed_revision(env, "bar") == 6


def test_download_failure_is_reported_never_tlmgr(
    tmp_path: Path, store: Path, shims
) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    notes: list[str] = []
    res = run(
        db, env, roots=["bar"], client=make_client(corrupt=True), notice=notes.append
    )
    assert res.installed == []
    assert res.failed == ["bar"]
    assert res.fallback == {}
    assert notes
    assert "sha512" in notes[0]
    assert "tlmgr" not in notes[0]
    assert list(store.iterdir()) == []
    assert not (env / "tex/latex/bar").exists()
    assert shims.calls("tlmgr") == []


def test_missing_checksum_is_a_failure(tmp_path: Path) -> None:
    text = _db_text().replace("containerchecksum", "xcontainerchecksum", 1)
    notes: list[str] = []
    res = run(
        tlpdb.parse_tlpdb(text), new_env(tmp_path), roots=["foo"], notice=notes.append
    )
    assert "foo" in res.failed
    assert any("no container checksum" in n for n in notes)


def test_root_collection_is_expanded_and_recorded(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    res = run(db, env, roots=["collection-x"])
    assert sorted(res.installed) == ["bar", "baz", "foo"]
    assert res.recorded == ["collection-x"]
    assert "collection-x" not in res.fallback
    assert res.fallback == {}
    obj = (env / "tlpkg/tlpobj/collection-x.tlpobj").read_text()
    assert "category Collection" in obj
    assert "runfiles" not in obj
    assert "collection-x" in (env / "tlpkg/texlive.tlpdb").read_text()
    assert archive.managed(env)["collection-x"]["revision"] == "1"
    res = run(db, env, roots=["collection-x"])  # recorded: nothing left to do
    assert res.recorded == []
    assert "collection-x" in res.present


def test_install_order_is_dependencies_first(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    res = run(db, new_env(tmp_path), roots=["foo"])
    assert res.installed == ["bar", "baz", "foo"]
    assert res.resolution.order.index("bar") < res.resolution.order.index("foo")
    assert res.resolution.why("bar") == ["foo", "bar"]


def test_plan_contract(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    calls: list[list[str]] = []

    def system(names: list[str]) -> set[str]:
        calls.append(sorted(names))
        return {"sysdep"} & set(names)

    pl = archive.plan(
        db, ["foo"], new_env(tmp_path), standalone=False, system_installed=system
    )
    assert pl.install == ["bar", "baz", "foo"]
    assert pl.fallback == {}
    assert pl.present == []
    assert pl.updates == []
    assert pl.resolution.skipped["sysdep"] == "installed in the system TeX Live"
    assert calls == [["foo"], ["bar", "baz", "sysdep"]]  # once per level


def test_unknown_dependency_warns_and_continues(tmp_path: Path) -> None:
    text = _db_text().replace("depend baz\n", "depend baz\ndepend ghost\n", 1)
    assert "depend ghost" in text
    notes: list[str] = []
    res = run(
        tlpdb.parse_tlpdb(text),
        new_env(tmp_path),
        notice=notes.append,
        roots=["foo", "nope"],
    )
    assert "foo" in res.installed
    assert res.missing == {"ghost": {"foo"}, "nope": set()}
    assert res.fallback == {}
    assert [n for n in notes if "ghost" in n] == [
        "ghost is not in the package index (needed by foo)"
    ]


def test_debug_log_shows_order_and_why(tmp_path: Path) -> None:
    lines: list[str] = []
    run(tlpdb.parse_tlpdb(_db_text()), new_env(tmp_path), log=lines.append)
    assert any(ln.startswith("install order: ") for ln in lines)
    assert "bar <- foo" in lines


def test_arch_dependency_goes_to_the_selected_arch(tmp_path: Path) -> None:
    text = (
        _db_text() + "name bin.universal-darwin\ncategory Package\nrevision 1\n"
        "containerchecksum aa\nbinfiles arch=universal-darwin\n bin/x/b\nrunfiles\n RELOC/a\n\n"
        + "name 00texlive.config\ncategory TLCore\nrevision 1\n"
        "depend setting_available_architectures:universal-darwin windows\n\n"
    )
    db = tlpdb.parse_tlpdb(text)
    env = new_env(tmp_path)
    pl = archive.plan(db, ["foo"], env, standalone=False, arch="universal-darwin")
    assert "bin.universal-darwin" in pl.install
    assert pl.fallback == {}
    pl = archive.plan(db, ["foo"], env, standalone=False, arch="windows")
    assert "bin.windows" in pl.resolution.skipped
    assert "bin.universal-darwin" not in pl.install


def test_usertree_non_relocatable_is_skipped(tmp_path: Path) -> None:
    text = _db_text().replace(
        "RELOC/tex/latex/bar/bar.sty", "texmf-dist/tex/latex/bar/bar.sty"
    )
    db = tlpdb.parse_tlpdb(text)
    notes: list[str] = []
    env = new_env(tmp_path)
    res = run(db, env, roots=["bar"], notice=notes.append)
    assert list(res.skipped) == ["bar"]
    assert "not relocatable" in res.skipped["bar"]
    assert res.installed == []
    assert res.fallback == {}
    assert res.failed == []
    assert any("bar not installed" in n for n in notes)
    pl = archive.plan(db, ["bar"], new_env(tmp_path, "full"), standalone=True)
    assert pl.install == ["bar"]
    assert pl.skipped == {}


def test_dependencies_with_actions_are_installed(tmp_path: Path) -> None:
    text = _db_text().replace(
        "name bar\ncategory Package\nrevision 5\n",
        "name bar\ncategory Package\nrevision 5\nexecute addMap bar.map\n",
        1,
    )
    res = run(tlpdb.parse_tlpdb(text), new_env(tmp_path), roots=["bar", "foo"])
    assert {"bar", "foo", "baz"} <= set(res.installed)
    assert res.fallback == {}


def test_no_tlmgr_invocation_in_sources() -> None:
    root = Path(archive.__file__).parent
    for name in ("archive.py", "tlactions.py"):
        text = (root / name).read_text()
        for needle in ('"tlmgr"', "'tlmgr'", "tlmgr_path", 'shutil.which("tlmgr'):
            assert needle not in text, (name, needle)


def test_download_retries_then_succeeds() -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    calls = {"n": 0}
    good = make_client()

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            msg = "[SSL: CERTIFICATE_VERIFY_FAILED]"
            raise httpx.ConnectError(msg)
        return good.send(req)

    notes: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    entry, fresh = archive.fetch(
        REPO, db.get("bar"), client=client, notice=notes.append
    )
    assert fresh
    assert calls["n"] == 3
    assert len(notes) == 2
    assert "retrying" in notes[0]


def test_download_gives_up_and_install_is_not_done(tmp_path: Path) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        msg = "[SSL: CERTIFICATE_VERIFY_FAILED]"
        raise httpx.ConnectError(msg)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    labels: list[tuple[str, int, int]] = []
    res = run(
        db,
        new_env(tmp_path),
        roots=("bar",),
        client=client,
        progress=lambda *a: labels.append(a),
    )
    assert res.failed == ["bar"]
    assert calls["n"] == len(archive.RETRY_DELAYS) + 1
    assert not any(total and done == total for _l, done, total in labels)


def _stale_client() -> httpx.Client:
    """A mirror that serves a valid but older build of bar (a different sha512)."""
    old = _container({"tex/latex/bar/bar.sty": "% old bar\n"}, "bar")
    return httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=old))
    )


def test_hash_mismatch_installs_with_warning_when_not_verifying(
    tmp_path: Path, store: Path
) -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    env = new_env(tmp_path)
    notes: list[str] = []
    res = run(
        db,
        env,
        roots=["bar"],
        client=_stale_client(),
        notice=notes.append,
        verify=False,
    )
    assert res.installed == ["bar"]
    assert res.failed == []
    assert (env / "tex/latex/bar/bar.sty").read_text() == "% old bar\n"
    assert any("hash mismatched" in n and "stale mirror" in n for n in notes)
    # stored under the sha512 it really has, never as the indexed build
    expected = archive.archive_id(
        "bar", "5", tlpdb.parse_tlpdb(_db_text()).get("bar").first("containerchecksum")
    )
    assert expected not in [p.name for p in store.iterdir()]


def test_hash_mismatch_fails_when_verifying(tmp_path: Path, store: Path) -> None:
    res = run(
        tlpdb.parse_tlpdb(_db_text()),
        new_env(tmp_path),
        roots=["bar"],
        client=_stale_client(),
        verify=True,
    )
    assert res.failed == ["bar"]
    assert list(store.iterdir()) == []


def test_verify_checksums_defaults_to_on() -> None:
    from texi.config import ProjectConfig

    assert ProjectConfig().verify_checksums is True


def test_pinned_mirror_from_index_source() -> None:
    redirector = "https://mirror.ctan.org/systems/texlive/tlnet"
    src = "https://mirrors.rit.edu/CTAN/systems/texlive/tlnet/tlpkg/texlive.tlpdb.xz"
    assert archive.pinned_mirror(redirector, src) == (
        "https://mirrors.rit.edu/CTAN/systems/texlive/tlnet"
    )
    assert archive.pinned_mirror(redirector, None) == redirector
    assert archive.pinned_mirror(redirector, "file:///x/texlive.tlpdb.xz") == redirector


def test_first_download_uses_the_index_mirror_then_the_redirector() -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host == "pinned.example":
            return httpx.Response(200, content=b"stale")  # fails the sha512
        return httpx.Response(200, content=BLOBS["bar"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notes: list[str] = []
    entry, fresh = archive.fetch(
        REPO,
        db.get("bar"),
        client=client,
        notice=notes.append,
        mirror="https://pinned.example/tlnet",
    )
    assert fresh
    assert seen == ["pinned.example", "repo.example"]
    assert "retrying" in notes[0]


def test_giving_up_on_a_mismatch_suggests_pinning() -> None:
    db = tlpdb.parse_tlpdb(_db_text())
    with pytest.raises(archive.ArchiveError, match="Pin one with"):
        archive.fetch(REPO, db.get("bar"), client=make_client(corrupt=True))


def _formatter_tgz(payload: bytes = b"#!/bin/sh\necho tex-fmt\n") -> bytes:
    tgz = io.BytesIO()
    with tarfile.open(fileobj=tgz, mode="w:gz") as tf:
        info = tarfile.TarInfo("tex-fmt")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    return tgz.getvalue()


class _Ctan:
    """Fake ctan.org JSON record and mirrors.ctan.org binary."""

    def __init__(self, version: str = "0.5.7") -> None:
        self.version = version
        self.hits: list[str] = []

    def client(self) -> httpx.Client:
        def handler(req: httpx.Request) -> httpx.Response:
            self.hits.append(req.url.path)
            if req.url.host == "ctan.org":
                return httpx.Response(200, json={"version": {"number": self.version}})
            body = f"#!/bin/sh\necho tex-fmt {self.version}\n".encode()
            return httpx.Response(200, content=_formatter_tgz(body))

        return httpx.Client(transport=httpx.MockTransport(handler))


def test_tex_fmt_is_installed_from_a_direct_url_once(
    tmp_path: Path, store: Path
) -> None:
    from texi import texfmt

    ctan = _Ctan()
    client = ctan.client()
    env = tmp_path / "p" / ".texenv"
    got = texfmt.ensure(env, "x86_64-linux", client=client)
    assert got == env / "bin/x86_64-linux/tex-fmt"
    assert os.access(got, os.X_OK)
    assert ctan.hits == [
        "/json/2.0/pkg/latex-formatter",
        "/support/latex-formatter/bin/tex-fmt-x86_64-linux.tar.gz",
    ]
    # a second tree takes it from the store: only the JSON record is read
    texfmt.ensure(tmp_path / "q" / ".texenv", "x86_64-linux", client=client)
    assert sum("tar.gz" in h for h in ctan.hits) == 1


def test_tex_fmt_updates_only_when_ctan_has_a_newer_version(
    tmp_path: Path, store: Path
) -> None:
    from texi import texfmt

    ctan = _Ctan("0.5.7")
    client = ctan.client()
    env = tmp_path / ".texenv"
    texfmt.ensure(env, "x86_64-linux", client=client)
    ctan.hits.clear()
    texfmt.ensure(env, "x86_64-linux", client=client)  # same version: JSON only
    assert ctan.hits == ["/json/2.0/pkg/latex-formatter"]
    ctan.hits.clear()
    texfmt.ensure(env, "x86_64-linux", client=client, update=False)  # no request at all
    assert ctan.hits == []
    ctan.version = "0.6.0"
    texfmt.ensure(env, "x86_64-linux", client=client)
    assert texfmt.installed_version(env) == "0.6.0"
    assert "0.6.0" in (env / "bin/x86_64-linux/tex-fmt").read_text()


def test_tex_fmt_failure_is_only_a_notice(tmp_path: Path, store: Path) -> None:
    from texi import texfmt

    down = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    notes: list[str] = []
    env = tmp_path / ".texenv"
    assert texfmt.ensure(env, "x86_64-linux", client=down, notice=notes.append) is None
    assert notes
    assert "tex-fmt not installed" in notes[0]
    # a tree that has one keeps it, silently, when CTAN cannot be reached
    texfmt.ensure(env, "x86_64-linux", client=_Ctan().client())
    notes.clear()
    assert texfmt.ensure(env, "x86_64-linux", client=down, notice=notes.append)
    assert notes == []
    assert texfmt.asset_name("windows") is None
    assert texfmt.asset_name("x86_64-linuxmusl") == "x86_64-alpine"


def test_prune_keeps_used_and_removes_old_and_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = archive.store_dir()
    for entry_id in ("bar-5-aaaaaaa", "bar-6-bbbbbbb", "baz-1-ccccccc"):
        (root / entry_id).mkdir(parents=True)
        (root / entry_id / archive.META_NAME).write_text("{}")
        (root / entry_id / "payload").write_text("x" * 10)
    (root / "partial").mkdir()  # no metadata: incomplete
    (root / ".tmp-x").mkdir()
    (root / ".dl-y.partial").write_text("x")
    (root / "fresh").mkdir()  # incomplete but recent: may be downloading
    (root / ".DS_Store").write_text("x")
    old = time.time() - 7200
    for n in ("partial", ".tmp-x", ".dl-y.partial"):
        os.utime(root / n, (old, old))
    dry = archive.prune({"bar-6-bbbbbbb"}, dry_run=True)
    assert [i for i, _ in dry] == [
        ".dl-y.partial",
        ".tmp-x",
        "bar-5-aaaaaaa",
        "baz-1-ccccccc",
        "partial",
    ]
    assert (root / "bar-5-aaaaaaa").exists()
    archive.prune({"bar-6-bbbbbbb"})
    assert sorted(p.name for p in root.iterdir()) == [
        ".DS_Store",
        "bar-6-bbbbbbb",
        "fresh",
    ]
