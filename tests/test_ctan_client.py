import json
from pathlib import Path

import httpx2
import pytest

from texi.ctan import CtanClient, CtanError, Package

FIX = Path(__file__).parent / "fixtures" / "ctan"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class Mock:
    """Route table keyed by 'path?query' (query optional); records requests."""

    def __init__(self, routes: dict[str, tuple[int, str]]) -> None:
        self.routes = routes
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        full = request.url.path + (
            f"?{request.url.query.decode()}" if request.url.query else ""
        )
        for k in (full, request.url.path):
            if k in self.routes:
                status, body = self.routes[k]
                return httpx2.Response(
                    status, content=body, headers={"content-type": "application/json"}
                )
        return httpx2.Response(404, content=fx("not_found.json"))


def make(
    tmp_path: Path, routes: dict[str, tuple[int, str]], cache: bool = True
) -> tuple[CtanClient, Mock]:
    m = Mock(routes)
    c = CtanClient(
        transport=httpx2.MockTransport(m), cache_dir=tmp_path / "http", cache=cache
    )
    return c, m


API = "/json/3.0"


def test_package_full_record(tmp_path: Path) -> None:
    c, _ = make(tmp_path, {f"{API}/pkg/amsmath": (200, fx("pkg_amsmath.json"))})
    p = c.package("amsmath")
    assert p is not None
    assert p.id == "amsmath"
    assert p.name == "AMS math"
    assert p.license == ["lppl1.3c"]
    assert p.version
    assert p.version.number == "2.17z"
    assert p.copyright[1].owner.startswith("LaTeX3")
    assert {d.language for d in p.documentation} == {"en", None}
    assert p.ctan
    assert p.ctan.path.endswith("amsmath")
    assert p.install == "/macros/latex/required/latex-amsmath.tds.zip"
    assert p.texlive == "amsmath"
    assert p.also == ["nath"]
    assert p.topics == ["maths", "tagged-pdf-partially"]
    assert p.authors[0].id == "latex"
    assert p.authors[0].active is True


def test_package_missing_fields_and_license_list(tmp_path: Path) -> None:
    c, _ = make(tmp_path, {f"{API}/pkg/pgf": (200, fx("pkg_pgf.json"))})
    p = c.package("pgf")
    assert p
    assert sorted(p.license) == ["fdl", "gpl2", "lppl1.3c"]
    assert p.aliases[0].id == "tikz"
    assert p.home is None
    assert p.descriptions[0].language is None


def test_package_install_object_and_unknown_fields() -> None:
    p = Package.model_validate({
        "id": "x",
        "install": {"path": "/a.zip"},
        "brandnew": 1,
        "topics": None,
    })
    assert p.install == "/a.zip"
    assert p.topics == []


def test_package_404_is_none(tmp_path: Path) -> None:
    c, _ = make(tmp_path, {})
    assert c.package("nonexistentpkg") is None
    assert c.author("nope") is None
    assert c.topic("nope") is None


def test_user_agent(tmp_path: Path) -> None:
    c, m = make(tmp_path, {f"{API}/pkg/binhex": (200, fx("pkg_binhex.json"))})
    c.package("binhex")
    assert m.requests[0].headers["user-agent"] == (
        "texi (+https://github.com/claudiosv/texi)"
    )


def test_authors_and_author(tmp_path: Path) -> None:
    c, _m = make(
        tmp_path,
        {
            f"{API}/authors?key=knuth": (200, fx("authors_knuth.json")),
            f"{API}/author/knuth?ref=true": (200, fx("author_knuth_ref.json")),
        },
    )
    [a] = c.authors("knuth")
    assert a.key == "knuth"
    assert a.familyname == "Knuth"
    assert a.died is False
    assert a.display_name == "Donald E. Knuth"
    k = c.author("knuth", ref=True)
    assert k
    assert k.packages
    assert len(k.packages) == 5


def test_topics_and_topic(tmp_path: Path) -> None:
    c, _ = make(
        tmp_path,
        {
            f"{API}/topics?key=abb": (200, fx("topics_abb.json")),
            f"{API}/topic/cv?ref=true": (200, fx("topic_cv_ref.json")),
        },
    )
    assert c.topics("abb")[0].key == "abbrev"
    t = c.topic("cv", ref=True)
    assert t
    assert t.details
    assert t.packages
    assert "cv" in t.packages


def test_topic_docs_references_alias() -> None:
    from texi.ctan import Topic

    assert Topic.model_validate({"key": "cv", "references": ["a"]}).packages == ["a"]


def test_licenses_and_version(tmp_path: Path) -> None:
    c, _ = make(
        tmp_path,
        {
            f"{API}/licenses?key=gpl": (200, fx("licenses_gpl.json")),
            "/json/2.0/version": (200, fx("version_2.0.json")),
        },
    )
    lic = c.licenses("gpl")
    assert lic[0].key == "gpl"
    assert lic[0].free is True
    v = c.version("2.0")
    assert v
    assert v.version == "2.0"
    assert c.version() is None  # 3.0 has no version endpoint (live behaviour)


def test_packages_key_and_aliases_param(tmp_path: Path) -> None:
    c, m = make(
        tmp_path,
        {
            f"{API}/packages?key=tikz&include-aliases=true": (
                200,
                fx("packages_tikz_aliases.json"),
            ),
            f"{API}/packages?key=tikz": (200, "[]"),
        },
    )
    assert c.packages("tikz") == []
    assert m.requests[-1].url.params.get("include-aliases") is None
    pk = c.packages("tikz", include_aliases=True)
    assert [p.key for p in pk] == ["tikz-cd"]
    assert m.requests[-1].url.params["include-aliases"] == "true"
    assert m.requests[-1].url.params["key"] == "tikz"
    [al] = c.package_aliases("tikz")
    assert al.alias == "tikz"
    assert al.pkg == "pgf"


def test_search(tmp_path: Path) -> None:
    c, m = make(tmp_path, {"/search/json": (200, fx("search_knuth.json"))})
    r = c.search("knuth", max=2, packages=True)
    assert r.number_of_hits == 112
    assert r.max == 2
    assert len(r.hits) == 2
    assert r.hits[0].path == "/pkg/knuth-lib"
    q = m.requests[0].url.params
    assert q["phrase"] == "knuth"
    assert q["ext"] == "true"
    assert q["PKG"] == "true"
    assert q["AUTHORS"] == "false"


def test_errors(tmp_path: Path) -> None:
    c, _ = make(
        tmp_path,
        {f"{API}/licenses": (503, "busy"), f"{API}/topics": (200, "<html>")},
    )
    with pytest.raises(CtanError):
        c.licenses()
    with pytest.raises(CtanError):
        c.topics()
    c2, _ = make(tmp_path / "b", {f"{API}/pkg/x": (200, '{"name": "no id"}')})
    with pytest.raises(CtanError):
        c2.package("x")


def test_offline_raises_and_find_texlive_none(tmp_path: Path) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        msg = "offline"
        raise httpx2.ConnectError(msg, request=request)

    c = CtanClient(transport=httpx2.MockTransport(boom), cache_dir=tmp_path)
    with pytest.raises(CtanError):
        c.package("binhex")
    assert c.find_texlive("binhex") is None


def test_cache_second_call_skips_transport(tmp_path: Path) -> None:
    routes = {f"{API}/pkg/binhex": (200, fx("pkg_binhex.json"))}
    c, m = make(tmp_path, routes)
    assert c.package("binhex")
    assert c.package("binhex")
    assert len(m.requests) == 1
    c.close()
    # persistent across clients
    c2, m2 = make(tmp_path, routes)
    assert c2.package("binhex")
    assert m2.requests == []
    # 404s are not cached
    assert c2.package("zzz") is None
    assert c2.package("zzz") is None
    assert len(m2.requests) == 2


def test_cache_disabled(tmp_path: Path) -> None:
    c, m = make(
        tmp_path, {f"{API}/pkg/binhex": (200, fx("pkg_binhex.json"))}, cache=False
    )
    c.package("binhex")
    c.package("binhex")
    assert len(m.requests) == 2


def test_context_manager_and_default_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from texi.ctan import default_cache_dir

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "texi" / "http"
    with CtanClient(transport=httpx2.MockTransport(Mock({}))) as c:
        assert c.package("a") is None
    assert (tmp_path / "texi" / "http" / "ctan.db").exists()


def test_find_texlive_cases(tmp_path: Path) -> None:
    c, _ = make(
        tmp_path,
        {
            f"{API}/packages?key=binhex&include-aliases=true": (
                200,
                json.dumps([
                    {"key": "binhex", "name": "binhex"},
                    {"key": "binhex-extra", "name": "x"},
                    {"alias": "binhex", "name": "B", "pkg": "other"},
                ]),
            ),
            f"{API}/pkg/binhex": (200, fx("pkg_binhex.json")),
            f"{API}/packages?key=foo&include-aliases=true": (200, '[{"key":"FOO"}]'),
            f"{API}/pkg/FOO": (200, '{"id":"FOO","texlive":null}'),
            f"{API}/packages?key=nope&include-aliases=true": (200, "[]"),
            f"{API}/packages?key=bad&include-aliases=true": (200, '{"x":1}'),
            f"{API}/packages?key=gone&include-aliases=true": (200, '[{"key":"gone"}]'),
        },
    )
    assert c.find_texlive("binhex") == ["kastrup"]  # key match only, not prefix/alias
    assert c.find_texlive("foo") == []  # case-insensitive, no texlive
    assert c.find_texlive("nope") == []
    assert c.find_texlive("bad") is None  # unexpected shape
    assert c.find_texlive("gone") == []  # pkg 404 skipped
    assert c.texlive_name("binhex") == "kastrup"
    assert c.texlive_name("zzz") is None


@pytest.mark.network
def test_live_binhex(tmp_path: Path) -> None:
    with CtanClient(cache_dir=tmp_path) as c:
        assert c.find_texlive("binhex") == ["kastrup"]
        p = c.package("binhex")
        assert p
        assert p.caption
        assert c.package("nonexistentpkg") is None


def test_find_texlive_follows_alias() -> None:
    """`tikz` is a CTAN alias of `pgf`, whose texlive name is `pgf`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/packages"):
            return httpx2.Response(
                200,
                json=[
                    {"key": "tikz-cd", "name": "TikZ-cd", "caption": "x"},
                    {"alias": "tikz", "name": "TikZ", "pkg": "pgf"},
                ],
            )
        if path.endswith("/pkg/pgf"):
            return httpx2.Response(
                200, json={"id": "pgf", "name": "PGF", "texlive": "pgf"}
            )
        return httpx2.Response(404, json={"errors": ["Not found"]})

    with CtanClient(transport=httpx2.MockTransport(handler), cache=False) as c:
        assert c.find_texlive("tikz") == ["pgf"]
