"""Property tests of the CTAN client against arbitrary responses (no network)."""

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

import httpx2
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from texi import ctan

Handler = Callable[[httpx2.Request], httpx2.Response]


def client(handler: Handler) -> ctan.CtanClient:
    return ctan.CtanClient(cache=False, transport=httpx2.MockTransport(handler))


FIELDS = [
    "key",
    "id",
    "name",
    "caption",
    "alias",
    "pkg",
    "texlive",
    "version",
    "install",
    "license",
    "authors",
    "aliases",
    "topics",
    "ctan",
    "path",
    "packages",
    "references",
    "hits",
    "numberOfHits",
    "offset",
    "max",
    "descriptions",
]
# Shallow on purpose: see the deeply nested JSON xfail.
json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(),
        st.text(max_size=8),
        st.sampled_from(["x", "", "true"]),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(FIELDS), inner, max_size=5),
    ),
    max_leaves=12,
)
bodies = st.one_of(
    json_values.map(lambda v: json.dumps(v).encode()), st.binary(max_size=60)
)
statuses = st.one_of(st.sampled_from([200, 404]), st.integers(100, 599))

# method name -> what a successful call returns
ENDPOINTS: dict[str, tuple[Callable[[ctan.CtanClient], Any], type, bool]] = {
    "authors": (lambda c: c.authors(), list, False),
    "author": (lambda c: c.author("x", ref=True), ctan.Author, True),
    "topics": (lambda c: c.topics("x"), list, False),
    "topic": (lambda c: c.topic("x"), ctan.Topic, True),
    "packages": (lambda c: c.packages(include_aliases=True), list, False),
    "package_aliases": (lambda c: c.package_aliases(), list, False),
    "package": (lambda c: c.package("x", author_name=True), ctan.Package, True),
    "licenses": (lambda c: c.licenses(), list, False),
    "version": (lambda c: c.version(), ctan.Version, True),
    "search": (lambda c: c.search("x", packages=True), ctan.SearchResult, False),
}


@settings(max_examples=250, deadline=None)
@given(endpoint=st.sampled_from(sorted(ENDPOINTS)), status=statuses, body=bodies)
@example(endpoint="package", status=200, body=b"null")
@example(endpoint="package", status=200, body=b'{"id":"x","license":null}')
@example(endpoint="packages", status=200, body=b'[1,{"alias":2}]')
def test_any_response_gives_a_model_none_or_ctan_error(
    endpoint: str, status: int, body: bytes
) -> None:
    call, model, optional = ENDPOINTS[endpoint]
    with client(lambda _: httpx2.Response(status, content=body)) as c:
        try:
            result = call(c)
        except ctan.CtanError:
            return
    if result is None:
        assert optional
        # None means "unknown" (404) or a literal JSON null.
        assert status == 404 or (status == 200 and json.loads(body) is None)
    else:
        assert status == 200
        assert isinstance(result, model)


# No control characters and no "{backslash}": see the InvalidURL xfail.
plain_text = st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=6)
listing = st.lists(
    st.one_of(
        st.fixed_dictionaries({"key": st.one_of(plain_text, st.just("want"))}),
        st.fixed_dictionaries({
            "alias": st.sampled_from(["want", "WANT", "other"]),
            "pkg": st.one_of(st.none(), st.sampled_from(["want", "p", "q"])),
        }),
    ),
    max_size=5,
)


@settings(max_examples=60, deadline=None)
@given(
    items=listing,
    names=st.lists(st.sampled_from(["t1", "t2", "", None]), min_size=1, max_size=3),
    failing=st.sets(st.integers(0, 5)),
)
def test_find_texlive_returns_distinct_names_or_none(
    items: list[dict[str, Any]], names: list[str | None], failing: set[int]
) -> None:
    calls = [0]

    def handler(request: httpx2.Request) -> httpx2.Response:
        n = calls[0]
        calls[0] += 1
        if n in failing:
            return httpx2.Response(500)
        if request.url.path.endswith("/packages"):
            return httpx2.Response(200, json=items)
        return httpx2.Response(200, json={"id": "p", "texlive": names[n % len(names)]})

    with client(handler) as c:
        found = c.find_texlive("want")
    if 0 in failing:
        assert found is None
        return
    assert found is not None
    assert len(found) == len(set(found))
    assert set(found) <= {n for n in names if n}


# Keys with URL syntax are excluded: see the xfails.
safe_keys = st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_.+ -]{0,12}", fullmatch=True)
unicode_keys = st.text(
    st.characters(exclude_categories=("Cs", "Cc"), exclude_characters="#?/%."),
    min_size=1,
    max_size=10,
)


@given(
    key=st.one_of(safe_keys, unicode_keys),
    kind=st.sampled_from(["pkg", "author", "topic"]),
)
def test_a_key_is_one_path_segment_of_the_request(key: str, kind: str) -> None:
    seen: list[httpx2.URL] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url)
        return httpx2.Response(404)

    with client(handler) as c:
        method = {"pkg": c.package, "author": c.author, "topic": c.topic}[kind]
        assert method(key) is None
    raw = seen[0].raw_path.decode("ascii")
    assert unquote(raw) == f"/json/3.0/{kind}/{key}"


# --- confirmed bugs -----------------------------------------------------------


def _requested(key: str) -> bytes:
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.raw_path)
        return httpx2.Response(404)

    with client(handler) as c:
        c.package(key)
    return seen[0]


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="'#' in a key is not percent-encoded"
)
def test_hash_in_a_key_is_encoded() -> None:
    assert _requested("a#b") == b"/json/3.0/pkg/a%23b"


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="'?' in a key starts a query string"
)
def test_question_mark_in_a_key_is_encoded() -> None:
    assert _requested("a?b") == b"/json/3.0/pkg/a%3Fb"


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="'..' in a key leaves the pkg/ endpoint"
)
def test_dot_segments_in_a_key_stay_under_pkg() -> None:
    assert _requested("../x").startswith(b"/json/3.0/pkg/")


@pytest.mark.xfail(
    strict=True,
    raises=httpx2.InvalidURL,
    reason="InvalidURL is not an HTTPError, so _get does not wrap it",
)
def test_a_control_character_in_a_key_gives_ctan_error_or_none() -> None:
    _requested("a\nb")


@pytest.mark.xfail(
    strict=True,
    raises=httpx2.InvalidURL,
    reason="find_texlive follows a bad key from CTAN and lets InvalidURL escape",
)
def test_find_texlive_survives_a_bad_key_in_the_listing() -> None:
    items = [{"alias": "q", "pkg": "a\nb"}]
    with client(lambda _: httpx2.Response(200, json=items)) as c:
        c.find_texlive("q")


@pytest.mark.xfail(
    strict=True,
    raises=httpx2.InvalidURL,
    reason="a bad redirect Location raises InvalidURL instead of CtanError",
)
def test_a_bad_redirect_gives_ctan_error() -> None:
    redirect = httpx2.Response(302, headers={"location": "mailto:a"})
    with client(lambda _: redirect) as c, pytest.raises(ctan.CtanError):
        c.package("x")


@pytest.mark.xfail(
    strict=True,
    raises=RecursionError,
    reason="resp.json() raises RecursionError, which is not caught",
)
def test_deeply_nested_json_gives_ctan_error() -> None:
    body = b"[" * 200_000 + b"]" * 200_000
    deep = httpx2.Response(200, content=body)
    with client(lambda _: deep) as c, pytest.raises(ctan.CtanError):
        c.package("x")
