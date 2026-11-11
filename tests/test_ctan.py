from pathlib import Path

import httpx2
import pytest

from texi import cli
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


def test_tlmgr_default_ctan_disambiguates(monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console

    found: dict[str, list[str]] = {}
    ctan: dict[str, list[str] | None] = {}
    ctan_calls: list[str] = []

    def fake_ctan_lookup(n: str) -> list[str] | None:
        ctan_calls.append(n)
        return ctan[n]

    monkeypatch.setattr(cli, "_search_bundles_tlmgr", lambda n, c, e: found[n])
    monkeypatch.setattr(cli, "find_texlive_for_ctan", fake_ctan_lookup)
    cache: dict[str, list[str]] = {}
    con = Console()

    # one tlmgr hit: CTAN is not asked
    found["a"] = ["pkg-a"]
    assert cli.find_bundle_for_sty("a", con, cache) == ["pkg-a"]
    assert ctan_calls == []

    # several hits, CTAN texlive field is one of them: it wins
    found["b"], ctan["b"] = ["x", "kastrup", "y"], ["kastrup"]
    assert cli.find_bundle_for_sty("b", con, cache) == ["kastrup"]

    # several hits, CTAN names none of them: all returned for the prompt
    found["c"], ctan["c"] = ["x", "y"], ["other"]
    assert cli.find_bundle_for_sty("c", con, cache) == ["x", "y"]

    # several hits, CTAN offline: all returned, not cached
    found["d"], ctan["d"] = ["x", "y"], None
    assert cli.find_bundle_for_sty("d", con, cache) == ["x", "y"]
    assert "d" not in cache

    # no tlmgr hit: CTAN is the fallback
    found["e"], ctan["e"] = [], ["kastrup"]
    assert cli.find_bundle_for_sty("e", con, cache) == ["kastrup"]

    # nothing anywhere, offline: not cached
    found["f"], ctan["f"] = [], None
    assert cli.find_bundle_for_sty("f", con, cache) == []
    assert "f" not in cache
