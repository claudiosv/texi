import hashlib
import io
import json
import lzma
import os
import sqlite3
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from texi import tlpdb as tl
from texi.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "tlpdb_sample.tlpdb"
runner = CliRunner()


@pytest.fixture(scope="module")
def db() -> tl.Tlpdb:
    return tl.parse_tlpdb(FIXTURE.read_text(encoding="utf-8"))


# --- parsing -----------------------------------------------------------------


def test_parse_entries(db: tl.Tlpdb) -> None:
    assert len(db.packages) == 9
    assert db.size_bytes == len(FIXTURE.read_bytes())
    assert db.get("nope") is None


def test_parse_header_and_sections(db: tl.Tlpdb) -> None:
    k = db.packages["kastrup"]
    assert (k.category, k.revision) == ("Package", "15878")
    assert k.shortdesc == "Convert numbers into binary, octal and hexadecimal"
    assert set(k.files) == {"docfiles", "srcfiles", "runfiles"}
    assert [f.path for f in k.files["srcfiles"]] == [
        "RELOC/source/generic/kastrup/binhex.drv",
        "RELOC/source/generic/kastrup/binhex.dtx",
        "RELOC/source/generic/kastrup/binhex.ins",
    ]
    assert k.sizes == {"docfiles": 37, "srcfiles": 7, "runfiles": 1}
    assert k.files["docfiles"][0].attrs == {"details": "Package documentation"}
    assert k.fields["catalogue-ctan"] == ["/macros/generic/kastrup"]
    assert k.fields["relocated"] == ["1"]
    assert k.first("catalogue-license") == "other-free"
    assert k.first("nonexistent") is None


def test_reloc_expansion(db: tl.Tlpdb) -> None:
    f = db.packages["kastrup"].files["runfiles"][0]
    assert f.path == "RELOC/tex/generic/kastrup/binhex.tex"
    assert f.full_path == "texmf-dist/tex/generic/kastrup/binhex.tex"
    assert f.basename == "binhex.tex"
    assert tl.expand_reloc("bin/windows/latexmk.exe") == "bin/windows/latexmk.exe"


def test_longdesc_joined(db: tl.Tlpdb) -> None:
    assert db.packages["kastrup"].longdesc == (
        "Provides expandable macros for both fixed-width and minimum-width "
        "numbers to bases 2, 4, 8 and 16."
    )


def test_catalogue_default_and_explicit(db: tl.Tlpdb) -> None:
    assert db.packages["kastrup"].catalogue == "binhex"
    assert db.packages["amsmath"].catalogue == "latex-amsmath"
    assert db.packages["latexmk"].catalogue_key == ""
    assert db.packages["latexmk"].catalogue == "latexmk"


def test_depends_and_attrs(db: tl.Tlpdb) -> None:
    assert db.packages["latexmk"].depends == ["latexmk.ARCH"]
    a = db.packages["amsmath"]
    doc = next(f for f in a.files["docfiles"] if f.path.endswith("README.md"))
    assert doc.attrs == {"details": "General README", "language": "en"}
    assert doc.path.endswith("README.md")


def test_binfiles_per_arch(db: tl.Tlpdb) -> None:
    inst = db.packages["00texlive.installer"]
    archs = {s.split(":", 1)[1] for s in inst.files if s.startswith("binfiles:")}
    assert {"windows", "universal-darwin", "x86_64-linux"} <= archs
    assert inst.sizes["binfiles:aarch64-linux"] == 47
    assert [
        f.path for f in db.packages["latexmk.windows"].files["binfiles:windows"]
    ] == ["bin/windows/latexmk.exe"]


def test_repeated_section_and_keys_lossless() -> None:
    text = (
        "name x\ncategory Package\nrevision 1\nshortdesc s\n"
        "execute addMap x.map\nexecute addMap y.map\npostaction shortcut\n"
        "binfiles arch=a size=1\n bin/a/one\n"
        "binfiles arch=a size=2\n bin/a/two\n\n\n"
    )
    p = tl.parse_tlpdb(text).packages["x"]
    assert p.fields["execute"] == ["addMap x.map", "addMap y.map"]
    assert p.fields["postaction"] == ["shortcut"]
    assert [f.path for f in p.files["binfiles:a"]] == ["bin/a/one", "bin/a/two"]
    assert p.sizes["binfiles:a"] == 3
    assert p.to_dict()["fields"]["execute"] == ["addMap x.map", "addMap y.map"]


# --- queries -----------------------------------------------------------------


def test_by_catalogue(db: tl.Tlpdb) -> None:
    assert [p.name for p in db.by_catalogue("binhex")] == ["kastrup"]
    assert [p.name for p in db.by_catalogue("latexmk")] == ["latexmk"]
    assert db.by_catalogue("kastrup") == []


def test_search_ranking(db: tl.Tlpdb) -> None:
    hits = db.search("amsmath")
    assert [h.package.name for h in hits][:2] == ["amsmath", "latex-amsmath-dev"]
    assert hits[0].matched_on == "name"
    assert hits[0].score > hits[1].score


def test_search_name_prefix_before_substring(db: tl.Tlpdb) -> None:
    hits = db.search("latexmk")
    names = [h.package.name for h in hits]
    assert names[0] == "latexmk"  # exact
    assert "latexmk.windows" in names  # prefix match ranks after the exact one
    assert hits[0].score > hits[1].score


def test_search_description_and_catalogue(db: tl.Tlpdb) -> None:
    hits = db.search("hexadecimal")
    assert [h.package.name for h in hits] == ["kastrup"]
    assert hits[0].matched_on in ("shortdesc", "longdesc")
    assert db.search("binhex")[0].package.name == "kastrup"
    assert db.search("binhex")[0].matched_on == "catalogue"
    assert db.search("hexadecimal", in_desc=False) == []


def test_search_all_terms_must_match(db: tl.Tlpdb) -> None:
    assert [h.package.name for h in db.search("binary octal")] == ["kastrup"]
    assert db.search("binary zzzzzz") == []


def test_search_regex_and_category(db: tl.Tlpdb) -> None:
    assert [h.package.name for h in db.search(r"^kast", regex=True)] == ["kastrup"]
    assert db.search("latexmk", category="TLCore") == []
    assert {h.package.category for h in db.search("latexmk", category="package")} == {
        "Package"
    }
    with pytest.raises(tl.TlpdbError, match="regular expression"):
        db.search("(", regex=True)


def test_search_files(db: tl.Tlpdb) -> None:
    assert db.search("binhex.tex", in_name=False, in_desc=False) == []
    hits = db.search("binhex.tex", in_files=True)
    assert [h.package.name for h in hits] == ["kastrup"]
    assert hits[0].matched_on == "file"
    assert hits[0].files == ["texmf-dist/tex/generic/kastrup/binhex.tex"]


def test_file_lookups(db: tl.Tlpdb) -> None:
    assert db.packages_for_file("binhex.tex") == ["kastrup"]
    assert db.packages_for_file("latexmk") == ["latexmk.universal-darwin"]
    assert db.packages_for_file("latexmk.exe") == ["latexmk.windows"]
    assert db.packages_for_file("nope.sty") == []
    got = db.packages_for_files(["binhex.tex", "nope.sty", "binhex.tex"])
    assert got == {"binhex.tex": ["kastrup"]}
    assert db.packages_for_file("binhex.tex", section="runfiles") == ["kastrup"]
    assert db.packages_for_file("binhex.dtx", section="runfiles") == []
    assert db.packages_for_file("binhex.dtx", section="srcfiles") == ["kastrup"]


def test_name_and_description_lookups(db: tl.Tlpdb) -> None:
    assert db.has("kastrup") and not db.has("binhex")
    assert len(db.names()) == 9 and db.count() == 9 == len(db.packages)
    assert "kastrup" in db.packages and "binhex" not in db.packages
    with pytest.raises(KeyError):
        db.packages["binhex"]
    assert db.shortdesc(["kastrup", "nope"]) == {
        "kastrup": "Convert numbers into binary, octal and hexadecimal"
    }
    assert db.categories(["kastrup", "latexmk", "nope"]) == {
        "kastrup": "Package",
        "latexmk": "Package",
    }
    assert db.names_by_catalogue("binhex") == ["kastrup"]
    assert db.names_by_catalogue_nocase("BinHex") == ["kastrup"]
    assert ("binhex", "kastrup") in db.catalogue_keys()
    assert db.get("kastrup") is db.get("kastrup")  # materialized once


def test_search_hit_is_summary(db: tl.Tlpdb) -> None:
    hit = db.search("kastrup")[0]
    assert hit.package.name == "kastrup" and hit.package.catalogue_key == "binhex"
    assert hit.package.files == {}
    assert db.get("kastrup").files  # type: ignore[union-attr]


def test_search_like_fallback_matches_fts(
    db: tl.Tlpdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert db._has_fts
    monkeypatch.setattr(tl, "_FTS_ENABLED", False)
    plain = tl.parse_tlpdb(FIXTURE.read_text(encoding="utf-8"))
    assert not plain._has_fts
    for q, kw in (
        ("amsmath", {}),
        ("binary octal", {}),
        ("hexadecimal", {}),
        ("ab", {}),  # shorter than a trigram: LIKE even with FTS
        ("100%_x", {}),
        ("binhex.tex", {"in_files": True}),
        ("sty$", {"regex": True, "in_files": True}),
    ):
        a = [
            (h.package.name, h.score, h.matched_on, h.files) for h in db.search(q, **kw)
        ]  # type: ignore[arg-type]
        b = [
            (h.package.name, h.score, h.matched_on, h.files)
            for h in plain.search(q, **kw)
        ]  # type: ignore[arg-type]
        assert a == b, q


def test_search_special_characters_are_literal(db: tl.Tlpdb) -> None:
    assert db.search("%") == [] and db.search("k%p") == []
    assert db.search("kast_up") == [] and db.search("kast_up", in_files=True) == []
    assert [h.package.name for h in db.search('"xz"')] == ["00texlive.config"]
    assert db.search("binhex.tex", in_files=True)  # "." is not a wildcard
    assert db.search("binhexXtex", in_files=True) == []


def test_stats(db: tl.Tlpdb) -> None:
    st = db.stats()
    assert st.entries == 9
    assert sum(st.categories.values()) == 9
    assert st.categories["Package"] == 6
    assert st.categories["TLCore"] == 3
    assert st.release == "2026"
    assert st.minrelease == "2016"
    assert st.frozen == "0"
    assert st.revision == "81079"
    assert (
        st.with_binfiles == 3
    )  # 00texlive.installer, latexmk.universal-darwin, .windows
    assert "windows" in st.architectures
    assert st.files["runfiles"] > 0 and st.files["binfiles"] > 0
    assert st.files["srcfiles"] >= 3


# --- loading -----------------------------------------------------------------


def test_load_local_plain_and_xz(tmp_path: Path) -> None:
    plain = tl.load(local=FIXTURE)
    assert len(plain.packages) == 9
    assert plain.compressed_bytes is None
    assert plain.source == str(FIXTURE.resolve())
    assert plain.fetched is not None

    xz = tmp_path / "texlive.tlpdb.xz"
    xz.write_bytes(lzma.compress(FIXTURE.read_bytes()))
    db = tl.load(local=xz)
    assert len(db.packages) == 9
    assert db.compressed_bytes == xz.stat().st_size
    assert db.size_bytes == plain.size_bytes
    # a bare path as source works too, as does a repository directory
    assert len(tl.load(str(xz)).packages) == 9
    (tmp_path / "repo" / "tlpkg").mkdir(parents=True)
    (tmp_path / "repo" / "tlpkg" / "texlive.tlpdb.xz").write_bytes(xz.read_bytes())
    assert len(tl.load(tmp_path / "repo").packages) == 9


def test_load_local_missing(tmp_path: Path) -> None:
    with pytest.raises(tl.TlpdbError, match="not found"):
        tl.load(local=tmp_path / "nope")


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, url: str, headers: dict[str, str]) -> None:
        super().__init__(body)
        self._url = url
        self.headers = headers

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResp":
        return self


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "texi" / "tlpdb"


REPO = "https://mirror.example/tlnet"
FINAL = "https://m1.example/tex/tlnet/tlpkg/texlive.tlpdb.xz"


def _xz() -> bytes:
    return lzma.compress(FIXTURE.read_bytes())


def test_remote_download_then_cache_hit(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def fake(req, timeout):  # type: ignore[no-untyped-def]
        calls.append(req)
        assert timeout == 20
        assert req.full_url == REPO + "/tlpkg/texlive.tlpdb.xz"
        assert req.get_header("User-agent").startswith("texi (+https://github.com/")
        return FakeResp(_xz(), FINAL, {"ETag": '"abc"', "Last-Modified": "Mon"})

    monkeypatch.setattr(tl, "urlopen", fake)
    db = tl.load(REPO + "/")
    assert len(db.packages) == 9
    assert db.source == FINAL  # the redirected URI
    assert db.repository == REPO
    assert not db.from_cache
    assert db.cache_path is not None and db.cache_path.parent == cache
    assert db.cache_path.is_file()
    assert db.compressed_bytes == len(_xz())

    db2 = tl.load(REPO)  # fresh: no network
    assert len(calls) == 1
    assert db2.from_cache and db2.source == FINAL and not db2.offline


def test_remote_stale_cache_conditional_get_304(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tl, "urlopen", lambda req, timeout: FakeResp(_xz(), FINAL, {"ETag": '"abc"'})
    )
    tl.load(REPO)
    meta = next(cache.glob("*.json"))
    data = json.loads(meta.read_text())
    data["fetched"] -= 3 * 24 * 3600
    meta.write_text(json.dumps(data))

    seen: dict[str, str | None] = {}

    def fake304(req, timeout):  # type: ignore[no-untyped-def]
        seen["inm"] = req.get_header("If-none-match")
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(tl, "urlopen", fake304)
    db = tl.load(REPO)
    assert seen["inm"] == '"abc"'
    assert db.from_cache and not db.offline
    assert len(db.packages) == 9
    assert db.fetched is not None
    assert (datetime.now(timezone.utc) - db.fetched).total_seconds() < 60  # revalidated

    # --refresh forces a request even when fresh
    seen.clear()
    tl.load(REPO, refresh=True)
    assert seen["inm"] == '"abc"'


def test_remote_offline_uses_cache(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tl, "urlopen", lambda req, timeout: FakeResp(_xz(), FINAL, {}))
    tl.load(REPO)

    def boom(req, timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(tl, "urlopen", boom)
    db = tl.load(REPO, refresh=True)
    assert db.from_cache and db.offline
    assert len(db.packages) == 9


def test_remote_offline_no_cache(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(tl, "urlopen", boom)
    with pytest.raises(tl.TlpdbError, match="offline"):
        tl.load(REPO)


def test_remote_corrupt_download_not_cached(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tl,
        "urlopen",
        lambda req, timeout: FakeResp(b"\xfd7zXZ\x00garbage", FINAL, {}),
    )
    with pytest.raises(tl.TlpdbError):
        tl.load(REPO)
    assert not list(cache.glob("*.xz"))


def _prime(monkeypatch: pytest.MonkeyPatch) -> tl.Tlpdb:
    monkeypatch.setattr(tl, "urlopen", lambda req, timeout: FakeResp(_xz(), FINAL, {}))
    return tl.load(REPO)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout):  # type: ignore[no-untyped-def]
        raise AssertionError("network used")

    monkeypatch.setattr(tl, "urlopen", boom)


def test_index_layout(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _prime(monkeypatch)
    key = hashlib.sha1(REPO.encode()).hexdigest()
    assert sorted(p.name for p in cache.iterdir()) == [
        f"{key}.json",
        f"{key}.sqlite",
        f"{key}.xz",
    ]
    assert db.index_path == cache / f"{key}.sqlite"
    assert db.schema_version == tl.SCHEMA_VERSION
    assert db.index_size == db.index_path.stat().st_size
    con = sqlite3.connect(db.index_path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == tl.SCHEMA_VERSION
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert meta["source"] == FINAL and meta["repository"] == REPO
    assert meta["schema"] == str(tl.SCHEMA_VERSION)
    assert int(meta["compressed_bytes"]) == len(_xz())
    assert int(meta["unpacked_bytes"]) == FIXTURE.stat().st_size
    assert float(meta["fetched"]) > 0
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"meta", "pkg", "depend", "file", "pkg_fts", "file_base"} <= tables
    assert con.execute(
        "SELECT section, base FROM file WHERE pkg = 'kastrup' AND base = 'binhex.tex'"
    ).fetchall() == [("runfiles", "binhex.tex")]
    con.close()


def test_cached_run_does_not_parse_text(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch)
    _no_network(monkeypatch)

    def no_parse(text):  # type: ignore[no-untyped-def]
        raise AssertionError("text parsed")

    monkeypatch.setattr(tl, "_parse_packages", no_parse)
    db = tl.load(REPO)
    assert db.from_cache and db.get("kastrup") is not None
    assert db.stats().entries == 9
    assert db.packages_for_file("binhex.tex") == ["kastrup"]
    assert db.source == FINAL and db.compressed_bytes == len(_xz())


def test_schema_bump_rebuilds_from_xz_offline(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch)
    _no_network(monkeypatch)
    monkeypatch.setattr(tl, "SCHEMA_VERSION", tl.SCHEMA_VERSION + 1)
    db = tl.load(REPO)  # fresh cache: no request, index rebuilt from the .xz
    assert db.schema_version == tl.SCHEMA_VERSION
    assert db.from_cache and not db.offline
    assert db.source == FINAL
    assert len(db.packages) == 9


def test_corrupt_index_is_rebuilt(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _prime(monkeypatch)
    assert db.index_path is not None
    db.close()
    db.index_path.write_bytes(b"this is not a database" * 100)
    _no_network(monkeypatch)
    db2 = tl.load(REPO)
    assert len(db2.packages) == 9 and db2.get("kastrup") is not None


def test_corrupt_index_and_xz_redownloads(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _prime(monkeypatch)
    assert db.index_path is not None and db.cache_path is not None
    db.close()
    db.index_path.write_bytes(b"garbage")
    db.cache_path.write_bytes(b"\xfd7zXZ\x00garbage")
    db2 = _prime(monkeypatch)  # fresh sidecar, but nothing usable: downloads again
    assert not db2.from_cache and len(db2.packages) == 9
    # and with no network at all that is an error
    db2.close()
    db2.index_path.write_bytes(b"garbage")  # type: ignore[union-attr]
    db2.cache_path.write_bytes(b"\xfd7zXZ\x00garbage")  # type: ignore[union-attr]
    monkeypatch.setattr(
        tl, "urlopen", lambda req, timeout: (_ for _ in ()).throw(OSError("down"))
    )
    with pytest.raises(tl.TlpdbError, match="offline"):
        tl.load(REPO)


def test_index_is_replaced_atomically(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    real = os.replace

    def spy(src, dst):  # type: ignore[no-untyped-def]
        calls.append((Path(src), Path(dst)))
        if str(dst).endswith(".sqlite"):
            assert Path(src).is_file()  # complete before it becomes visible
            assert not Path(dst).exists()
        real(src, dst)

    monkeypatch.setattr(tl.os, "replace", spy)
    _prime(monkeypatch)
    ((src, dst),) = [c for c in calls if str(c[1]).endswith(".sqlite")]
    assert src.parent == dst.parent and src != dst
    assert not src.exists()


def test_failed_build_leaves_no_partial_index(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn, packages, meta):  # type: ignore[no-untyped-def]
        conn.execute("CREATE TABLE half(x)")
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(tl, "_populate", broken)
    monkeypatch.setattr(tl, "urlopen", lambda req, timeout: FakeResp(_xz(), FINAL, {}))
    with pytest.raises(tl.TlpdbError, match="cannot build the index"):
        tl.load(REPO)
    assert not list(cache.glob("*.sqlite")) and not list(cache.glob(".*"))


def test_stale_index_after_304_uses_new_fetch_time(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch)
    meta = next(cache.glob("*.json"))
    data = json.loads(meta.read_text())
    data["fetched"] -= 3 * 24 * 3600
    meta.write_text(json.dumps(data))

    def fake304(req, timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(tl, "urlopen", fake304)
    db = tl.load(REPO)
    assert db.fetched is not None
    assert (datetime.now(timezone.utc) - db.fetched).total_seconds() < 60
    assert tl.has_cache(REPO) and not tl.has_cache(REPO + "/other")


def _system(location: str) -> tl.SystemTexLive:
    return tl.SystemTexLive(
        Path("/x"),
        Path("/x/tlpkg/texlive.tlpdb"),
        frozenset(),
        {"opt_location": location},
        {},
    )


def test_resolve_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tl, "load_local_installed", lambda root=None: None)
    monkeypatch.setattr(tl, "tlmgr_repository", lambda: pytest.fail("tlmgr ran"))
    assert tl.resolve_repository("ctan", tmp_path) == (
        tl.DEFAULT_REPOSITORY,
        "--repository",
    )
    assert tl.resolve_repository(None, tmp_path) == (tl.DEFAULT_REPOSITORY, "default")
    (tmp_path / "texproject.toml").write_text(
        f'[project]\nname = "x"\nrepository = "{REPO}/"\n'
    )
    assert tl.resolve_repository(None, tmp_path) == (REPO, "texproject.toml")
    assert tl.resolve_repository("https://o/", tmp_path)[0] == "https://o"
    monkeypatch.chdir(tmp_path)  # project_dir=None means the cwd
    assert tl.resolve_repository(None, None)[1] == "texproject.toml"
    (tmp_path / "texproject.toml").unlink()
    # no project: the system tlpdb's opt_location, never `tlmgr option repository`
    monkeypatch.setattr(
        tl, "load_local_installed", lambda root=None: _system("http://t/tlnet/")
    )
    assert tl.resolve_repository(None, None) == ("http://t/tlnet", "system tlpdb")
    # __MASTER__ means "the installer's own directory": keep the default mirror
    monkeypatch.setattr(
        tl, "load_local_installed", lambda root=None: _system("__MASTER__")
    )
    assert tl.resolve_repository(None, None) == (tl.DEFAULT_REPOSITORY, "default")
    # the project repository still wins over the system's
    (tmp_path / "texproject.toml").write_text(
        f'[project]\nname = "x"\nrepository = "{REPO}"\n'
    )
    assert tl.resolve_repository(None, None) == (REPO, "texproject.toml")


def test_resolve_repository_tlmgr_project_asks_tlmgr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "texproject.toml").write_text(
        '[project]\nname = "x"\ninstall_method = "tlmgr"\n'
    )
    monkeypatch.setattr(tl, "tlmgr_repository", lambda: "http://t/tlnet")
    monkeypatch.setattr(tl, "load_local_installed", lambda root=None: None)
    assert tl.resolve_repository(None, tmp_path) == (
        "http://t/tlnet",
        "tlmgr option repository",
    )


def test_tlmgr_repository_forbidden_in_archive_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEXI_FORBID_TLMGR", "1")
    with pytest.raises(AssertionError, match="archive mode"):
        tl.tlmgr_repository()


def test_normalize_repository() -> None:
    assert tl.normalize_repository("ctan") == tl.DEFAULT_REPOSITORY
    assert tl.normalize_repository("tlnet") == tl.DEFAULT_REPOSITORY
    assert tl.normalize_repository("https://x/y/") == "https://x/y"


def test_tlmgr_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        def __init__(self, out: str) -> None:
            self.stdout = out

    def run_with(out: str) -> str | None:
        monkeypatch.setattr(tl.shutil, "which", lambda _: "/bin/tlmgr")
        monkeypatch.setattr(tl.subprocess, "run", lambda *a, **k: R(out))
        return tl.tlmgr_repository()

    pre = "Default package repository (repository): "
    assert run_with(pre + "http://mirror.ctan.org/systems/texlive/tlnet\n") == (
        "http://mirror.ctan.org/systems/texlive/tlnet"
    )
    assert run_with(pre + "ctan\n") == tl.DEFAULT_REPOSITORY
    assert run_with(pre + "/local/dir\n") is None
    assert run_with("garbage") is None


# --- CLI ---------------------------------------------------------------------


def cli(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(
        app,
        ["tlpdb", *args],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
        terminal_width=200,
    )


def test_cli_stats() -> None:
    for args in (["stats", "--local", str(FIXTURE)],):
        r = cli(*args)
        assert r.exit_code == 0, r.output
        assert str(FIXTURE.resolve()) in r.output.replace("\n", "")
        assert "Entries" in r.output and "9" in r.output
        assert "TeX Live 2026" in r.output
        assert "Package" in r.output and "TLCore" in r.output
        assert "binfiles" in r.output
        assert "Index" not in r.output  # --local is an in-memory index


def test_cli_search() -> None:
    r = cli("search", "hexadecimal", "--local", str(FIXTURE))
    assert r.exit_code == 0, r.output
    assert "kastrup" in r.output and "(CTAN: binhex)" in r.output


def test_cli_search_file_regex_exact_limit() -> None:
    r = cli("search", "binhex.tex", "-f", "--local", str(FIXTURE))
    assert r.exit_code == 0, r.output
    assert (
        "kastrup" in r.output
        and "texmf-dist/tex/generic/kastrup/binhex.tex" in r.output
    )
    r = cli("search", "^lat.*dev$", "-e", "--local", str(FIXTURE))
    assert "latex-amsmath-dev" in r.output and "kastrup" not in r.output
    r = cli("search", "latexmk", "--exact", "--local", str(FIXTURE))
    assert "latexmk.windows" not in r.output and "latexmk" in r.output
    r = cli("search", "latex", "-n", "1", "--local", str(FIXTURE))
    assert "Showing 1 of" in r.output
    r = cli("search", "zzzzqq", "--local", str(FIXTURE))
    assert r.exit_code == 1
    r = cli("search", "(", "-e", "--local", str(FIXTURE))
    assert r.exit_code == 1 and "regular expression" in r.output


def test_cli_info() -> None:
    r = cli("info", "kastrup", "--local", str(FIXTURE))
    assert r.exit_code == 0, r.output
    for s in (
        "kastrup",
        "r15878",
        "Convert numbers into binary",
        "Catalogue",
        "binhex",
        "/macros/generic/kastrup",
        "other-free",
        "1,388 bytes",
        "docfiles: 1 file(s)",
        "srcfiles: 3 file(s)",
        "texmf-dist/tex/generic/kastrup/binhex.tex",
    ):
        assert s in r.output, s
    assert "RELOC" not in r.output
    # CTAN key lookup
    r = cli("info", "binhex", "--local", str(FIXTURE), "--no-files")
    assert r.exit_code == 0 and "r15878" in r.output
    assert "binhex.tex" not in r.output


def test_cli_info_truncates_long_lists() -> None:
    r = cli("info", "00texlive.installer", "--local", str(FIXTURE))
    assert r.exit_code == 0, r.output
    assert "more (use --all-files)" in r.output
    assert "binfiles:windows" in r.output
    r = cli("info", "00texlive.installer", "--all-files", "--local", str(FIXTURE))
    assert "more (use --all-files)" not in r.output


def test_cli_info_json() -> None:
    r = cli("info", "kastrup", "--json", "--local", str(FIXTURE))
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["name"] == "kastrup"
    assert data["catalogue"] == "binhex"
    assert data["fields"]["catalogue-license"] == ["other-free"]
    assert data["files"]["runfiles"][0]["path"] == (
        "texmf-dist/tex/generic/kastrup/binhex.tex"
    )


def test_cli_info_unknown_suggests() -> None:
    r = cli("info", "kastrp", "--local", str(FIXTURE))
    assert r.exit_code == 1
    assert "Did you mean" in r.output and "kastrup" in r.output
    r = cli("info", "qqqqqqqq", "--local", str(FIXTURE))
    assert r.exit_code == 1 and "qqqqqqqq" in r.output


def test_cli_group_without_subcommand_runs_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tl.shutil, "which", lambda _: None)  # no tlmgr
    monkeypatch.setattr(tl, "urlopen", lambda req, timeout: FakeResp(_xz(), FINAL, {}))
    r = cli()
    assert r.exit_code == 0, r.output
    flat = r.output.replace("\n", "")
    assert FINAL in flat and ".sqlite" in flat
    assert "Schema version" in r.output and "Index size" in r.output


def test_cli_offline_without_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    def boom(req, timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(tl, "urlopen", boom)
    r = cli("stats", "-r", REPO)
    assert r.exit_code == 1
    assert "Error" in r.output and "offline" in r.output


def test_cli_repository_from_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "texproject.toml").write_text(
        f'[project]\nname = "x"\nrepository = "{REPO}"\n'
    )
    urls: list[str] = []

    def fake(req, timeout):  # type: ignore[no-untyped-def]
        urls.append(req.full_url)
        return FakeResp(_xz(), req.full_url, {})

    monkeypatch.setattr(tl, "urlopen", fake)
    r = cli("stats")
    assert r.exit_code == 0, r.output
    assert urls == [REPO + "/tlpkg/texlive.tlpdb.xz"]


# --- settings (pseudo-records) -------------------------------------------------


def test_settings_are_separate_from_depends(db: tl.Tlpdb) -> None:
    cfg = db.packages["00texlive.config"]
    assert cfg.depends == []
    assert "container_format/xz" in cfg.settings
    assert "release/2026" in cfg.settings
    assert db.packages["latexmk"].depends == ["latexmk.ARCH"]
    assert db.packages["latexmk"].settings == []
    assert cfg.to_dict()["depends"] == [] and cfg.to_dict()["settings"] == cfg.settings


def test_settings_in_sqlite_index(db: tl.Tlpdb) -> None:
    con = db._conn
    lines = [r[0] for r in con.execute("SELECT line FROM setting ORDER BY rowid")]
    assert lines == db.packages["00texlive.config"].settings
    assert con.execute(
        "SELECT count(*) FROM depend WHERE pkg LIKE '00texlive%'"
    ).fetchone() == (0,)
    assert db.settings()["release"] == "2026"
    assert tl.split_setting("opt_backupdir:tlpkg/backups") == (
        "opt_backupdir",
        "tlpkg/backups",
    )
    assert tl.split_setting("opt_sys_bin:/usr/local/bin") == (
        "opt_sys_bin",
        "/usr/local/bin",
    )
    assert tl.split_setting("revision/81047") == ("revision", "81047")
    assert "windows" in db.available_architectures()


def test_old_schema_index_is_rebuilt_offline(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(tl, "SCHEMA_VERSION", 1)
        _prime(m)
    _no_network(monkeypatch)
    db = tl.load(REPO)
    assert db.schema_version == tl.SCHEMA_VERSION == 2
    assert db.from_cache and not db.offline
    assert db.packages["00texlive.config"].settings
    assert db.packages["00texlive.config"].depends == []


def test_cli_info_shows_settings_section() -> None:
    r = cli("info", "00texlive.config", "--local", str(FIXTURE))
    assert r.exit_code == 0, r.output
    assert "Settings" in r.output and "release/2026" in r.output
    assert "Depends" not in r.output
    r = cli("info", "latexmk", "--local", str(FIXTURE))
    assert "Depends" in r.output and "Settings" not in r.output
    data = json.loads(
        cli("info", "00texlive.config", "--json", "--local", str(FIXTURE)).output
    )
    assert data["depends"] == [] and "frozen/0" in data["settings"]


def test_real_index_pseudo_records_stay_out_of_edges() -> None:
    from texi import resolver

    repo, _ = tl.resolve_repository(None, None)
    if not tl.has_cache(repo):
        pytest.skip("no cached TeX Live index")
    real = tl.load(repo)
    cfg = real.get("00texlive.config")
    assert cfg is not None and cfg.depends == [] and cfg.settings
    for name in real.names():
        pkg = real.get(name)
        assert pkg is not None
        assert not any(resolver.is_pseudo_record(name, d) for d in pkg.depends), name
