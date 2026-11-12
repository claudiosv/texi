from pathlib import Path

import httpx2
import pytest
from rich.console import Console

from texi import cli
from texi import tlpdb as tl
from texi.ctan import CtanClient


def use_ctan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, routes: dict[str, object]
) -> None:
    """Point cli at a real CtanClient over a mock transport; unknown paths 404."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        key = request.url.path.removeprefix("/json/3.0/")
        if request.url.query:
            key += f"?{request.url.query.decode()}"
        if key in routes:
            return httpx2.Response(200, json=routes[key])
        return httpx2.Response(404, json={"errors": ["Not found"]})

    monkeypatch.setattr(
        cli,
        "CtanClient",
        lambda: CtanClient(transport=httpx2.MockTransport(handler), cache_dir=tmp_path),
    )


def test_ctan_alias_maps_to_texlive_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_ctan(
        monkeypatch,
        tmp_path,
        {
            "packages?key=binhex&include-aliases=true": [
                {"key": "binhex", "name": "binhex", "caption": "Convert numbers"},
                {"key": "binhex-extra", "name": "x", "caption": "y"},
            ],
            "pkg/binhex": {"id": "binhex", "name": "binhex", "texlive": "kastrup"},
        },
    )
    assert cli.find_texlive_for_ctan("binhex") == ["kastrup"]


def test_ctan_package_not_in_texlive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_ctan(
        monkeypatch,
        tmp_path,
        {
            "packages?key=foo&include-aliases=true": [{"key": "foo"}],
            "pkg/foo": {"id": "foo", "name": "foo", "texlive": None},
        },
    )
    assert cli.find_texlive_for_ctan("foo") == []


def test_ctan_no_match_and_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_ctan(monkeypatch, tmp_path, {"packages?key=nope&include-aliases=true": []})
    assert cli.find_texlive_for_ctan("nope") == []
    use_ctan(monkeypatch, tmp_path / "b", {})  # 404 on the list -> unreachable
    assert cli.find_texlive_for_ctan("nope") is None


# --- local tlpdb index first ---------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "tlpdb_sample.tlpdb"
EXTRA = """
name acmart
category Package
revision 1
shortdesc Class for ACM publications
runfiles size=1
 texmf-dist/tex/latex/acmart/acmart.cls
docfiles size=1
 texmf-dist/doc/latex/acmart/acmart-doc.sty

name docsonly
category Package
revision 1
shortdesc Ships foo.sty only as documentation
docfiles size=1
 texmf-dist/doc/latex/docsonly/foo.sty

name pkg-one
category Package
revision 1
shortdesc First provider
runfiles size=1
 texmf-dist/tex/latex/pkg-one/shared.sty

name pkg-two
category Package
revision 1
shortdesc Second provider
runfiles size=1
 texmf-dist/tex/latex/pkg-two/shared.sty

name keyed
category Package
revision 1
shortdesc Has the catalogue key wanted
catalogue wanted

name wanted-impl
category Package
revision 1
shortdesc Ships wanted.sty without the key
runfiles size=1
 texmf-dist/tex/latex/wanted-impl/wanted.sty

name collection-coll
category Collection
revision 1
shortdesc A collection
catalogue coll

name coll-impl
category Package
revision 1
shortdesc Ships coll.sty
runfiles size=1
 texmf-dist/tex/latex/coll-impl/coll.sty

name onlycoll
category Collection
revision 1
shortdesc Collection with a catalogue key
catalogue onlycoll-key
"""


def _no_fallback(*a: object, **k: object) -> None:
    msg = "fallback pipeline used"
    raise AssertionError(msg)


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> tl.Tlpdb:
    """The sample tlpdb plus a few synthetic entries; the fallbacks must not run."""
    db = tl.parse_tlpdb(FIXTURE.read_text(encoding="utf-8") + "\n" + EXTRA)
    monkeypatch.setattr(cli, "_resolver_tlpdb", lambda console: db)
    monkeypatch.setattr(cli, "find_texlive_for_ctan", _no_fallback)
    return db


def test_index_catalogue_key_maps_name_to_package(index: tl.Tlpdb) -> None:
    cache: dict[str, list[str]] = {}
    # kastrup has `catalogue binhex` (and ships binhex.tex)
    assert cli.find_bundle_for_sty("binhex", Console(), cache) == ["kastrup"]
    assert cache == {"binhex": ["kastrup"]}
    # the catalogue key alone is enough when no .tex extension is searched
    assert cli.find_bundle_for_sty("binhex", Console(), {}, exts=(".sty",)) == [
        "kastrup"
    ]


def test_index_run_file_class(index: tl.Tlpdb) -> None:
    assert cli.find_bundle_for_sty("acmart", Console(), {}) == ["acmart"]


def test_index_exact_name_wins_over_other_providers(index: tl.Tlpdb) -> None:
    # amsmath.sty is shipped by amsmath and latex-amsmath-dev
    assert cli.find_bundle_for_sty("amsmath", Console(), {}) == ["amsmath"]


def test_index_ignores_doc_files(
    index: tl.Tlpdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    # acmart-doc.sty is listed under docfiles only: not a provider, so the index
    # has no candidate and the pipeline is asked
    calls: list[str] = []
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda n: calls.append(n) or [])
    assert cli.find_bundle_for_sty("acmart-doc", Console(), {}) == []
    assert calls == ["acmart-doc"]


def test_index_multiple_candidates_all_returned(index: tl.Tlpdb) -> None:
    cache: dict[str, list[str]] = {}
    assert cli.find_bundle_for_sty("shared", Console(), cache) == ["pkg-one", "pkg-two"]
    assert cache["shared"] == ["pkg-one", "pkg-two"]


def test_index_catalogue_key_breaks_ties(index: tl.Tlpdb) -> None:
    # wanted.sty is in wanted-impl, but `keyed` has the catalogue key `wanted`
    assert cli.find_bundle_for_sty("wanted", Console(), {}) == ["keyed"]


def test_index_drops_collections_unless_nothing_else(index: tl.Tlpdb) -> None:
    assert cli.find_bundle_for_sty("coll", Console(), {}) == ["coll-impl"]
    assert cli.find_bundle_for_sty("onlycoll-key", Console(), {}) == ["onlycoll"]


def test_index_cache_hit_skips_everything(
    index: tl.Tlpdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_index(console: object) -> None:
        msg = "index consulted"
        raise AssertionError(msg)

    monkeypatch.setattr(cli, "_resolver_tlpdb", no_index)
    assert cli.find_bundle_for_sty("x", Console(), {"x": ["cached"]}) == ["cached"]


def test_index_miss_uses_pipeline_and_caches_answer(
    index: tl.Tlpdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "find_texlive_for_ctan", lambda n: calls.append("ctan") or ["from-ctan"]
    )
    cache: dict[str, list[str]] = {}
    assert cli.find_bundle_for_sty("nonexistentpkg", Console(), cache) == ["from-ctan"]
    assert calls == ["ctan"]
    # a consulted index plus an answering pipeline: even "nothing" is remembered
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda n: [])
    assert cli.find_bundle_for_sty("nothing", Console(), cache) == []
    assert cache["nothing"] == []
    # unreachable CTAN: not remembered
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda n: None)
    assert cli.find_bundle_for_sty("offline", Console(), cache) == []
    assert "offline" not in cache


def test_no_index_negative_result_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_resolver_tlpdb", lambda console: None)
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda n: [])
    cache: dict[str, list[str]] = {}
    assert cli.find_bundle_for_sty("nope", Console(), cache) == []
    assert cache == {}  # tlpdb not consulted: do not remember the miss
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda n: ["kastrup"])
    assert cli.find_bundle_for_sty("binhex", Console(), cache) == ["kastrup"]
    assert cache == {"binhex": ["kastrup"]}  # positive answers are remembered


def test_resolver_tlpdb_loaded_once_and_failure_memoized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tl.parse_tlpdb(FIXTURE.read_text(encoding="utf-8"))
    loads: list[str] = []

    def load(repo: str) -> tl.Tlpdb:
        loads.append(repo)
        return db

    monkeypatch.setattr(tl, "resolve_repository", lambda r, d: ("https://r/tlnet", "x"))
    monkeypatch.setattr(tl, "has_cache", lambda repo: False)
    monkeypatch.setattr(tl, "load", load)
    cli._reset_tlpdb_memo()
    con = Console(force_terminal=False)
    assert cli._resolver_tlpdb(con) is db
    assert cli._resolver_tlpdb(con) is db
    assert loads == ["https://r/tlnet"]
    assert "Building TeX Live package index" in capsys.readouterr().out

    def broken(repo: str) -> tl.Tlpdb:
        loads.append("again")
        msg = "offline"
        raise tl.TlpdbError(msg)

    monkeypatch.setattr(tl, "load", broken)
    cli._reset_tlpdb_memo()
    assert cli._resolver_tlpdb(con) is None
    assert cli._resolver_tlpdb(con) is None
    assert loads.count("again") == 1
    cli._reset_tlpdb_memo()


def test_short_descriptions_come_from_the_index(index: tl.Tlpdb) -> None:
    got = cli._short_descriptions(["kastrup", "other"])
    assert got == {"kastrup": "Convert numbers into binary, octal and hexadecimal"}


def test_tl_package_exists_uses_only_the_index(index: tl.Tlpdb) -> None:
    assert cli._tl_package_exists("kastrup")
    assert not cli._tl_package_exists("binhex")  # a catalogue key, not a package
    assert not cli._tl_package_exists("ghost")


def test_fallback_pipeline_without_tlpdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tlpdb unavailable: CTAN decides."""
    from rich.console import Console

    ctan: dict[str, list[str] | None] = {}
    monkeypatch.setattr(cli, "_resolver_tlpdb", lambda console, refresh=False: None)
    monkeypatch.setattr(cli, "find_texlive_for_ctan", lambda n: ctan[n])
    cache: dict[str, list[str]] = {}
    con = Console()

    ctan["e"] = ["kastrup"]
    assert cli.find_bundle_for_sty("e", con, cache) == ["kastrup"]

    # nothing anywhere, offline: not cached
    ctan["f"] = None
    assert cli.find_bundle_for_sty("f", con, cache) == []
    assert "f" not in cache
