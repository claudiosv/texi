"""Typed client for the CTAN JSON API 3.0 with a persistent HTTP cache.

The API is documented at <https://ctan.org/help/json/3.0>. All models ignore
unknown fields and treat nearly every field as optional, so new or missing API
fields never break parsing.

Caching
-------
Requests go through `hishel` (`hishel.httpx2.CacheTransport`) backed by a SQLite
file. CTAN sends no `Cache-Control`, `ETag` or `Last-Modified` headers, so the
transport runs in its default (non RFC 9111) mode: every successful GET is
stored and served until a fixed TTL expires, regardless of response headers.
Package records and package lists live 24 hours, static lists (licenses,
topics, authors) 7 days, search results one hour. Failed responses (404, 5xx)
are never stored.

Expired entries are not served. When CTAN is unreachable and the cache entry
has expired, calls raise `CtanError` (and `CtanClient.find_texlive` returns
`None`). Serving stale data offline is not supported by hishel's default mode.
"""

from __future__ import annotations

import os
import weakref
from pathlib import Path
from typing import Any, Self

import httpx2
from hishel import SqliteStorage
from hishel.httpx2 import CacheTransport
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

DEFAULT_BASE_URL = "https://ctan.org"
API_VERSION = "3.0"
USER_AGENT = "texi (+https://github.com/claudiosv/texi)"

TTL_PACKAGE = 24 * 3600.0
"""Seconds a package record or package list stays fresh."""
TTL_STATIC = 7 * 24 * 3600.0
"""Seconds a static list (authors, topics, licenses) stays fresh."""
TTL_SEARCH = 3600.0
"""Seconds search results stay fresh."""
TTL_VERSION = 24 * 3600.0


class CtanError(Exception):
    """Network failure, unexpected HTTP status (e.g. 5xx) or unparsable response."""


def default_cache_dir() -> Path:
    """Default HTTP cache dir: `$XDG_CACHE_HOME/texi/http` (or `~/.cache/texi/http`)."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "texi" / "http"


# --------------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Author(_Model):
    """An author record (`authors`, `author/<key>`).

    Attributes
    ----------
    key : str
        CTAN author key.
    packages : list[str] or None
        Package keys mentioning the author; only set with `ref=True`.
    """

    key: str
    title: str | None = None
    givenname: str | None = None
    von: str | None = None
    familyname: str | None = None
    junior: str | None = None
    pseudonym: str | None = None
    female: bool | None = None
    died: bool | None = None
    packages: list[str] | None = None

    @property
    def display_name(self) -> str:
        """Printable name; the pseudonym wins when present."""
        if self.pseudonym:
            return self.pseudonym
        parts = [self.title, self.givenname, self.von, self.familyname, self.junior]
        return " ".join(p for p in parts if p)


class Topic(_Model):
    """A topic record (`topics`, `topic/<key>`).

    Attributes
    ----------
    packages : list[str] or None
        Package keys using the topic; only set with `ref=True`. (The live API
        names this field `packages`; the docs say `references`, which is
        accepted too.)
    """

    key: str
    details: str | None = None
    packages: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("packages", "references")
    )


class PackageSummary(_Model):
    """List item of `packages`: key, display name and caption."""

    key: str
    name: str | None = None
    caption: str | None = None


class PackageAlias(_Model):
    """Alias entry of `packages?include-aliases=true`."""

    alias: str
    name: str | None = None
    pkg: str | None = None


class License(_Model):
    """A license (`licenses`)."""

    key: str
    name: str | None = None
    free: bool | None = None


class Version(_Model):
    """The `version` endpoint: the interface version as a string."""

    version: str | None = None


class PackageVersion(_Model):
    """Version of a package; `number` and/or `date` may be missing or empty."""

    number: str | None = None
    date: str | None = None


class AliasRef(_Model):
    """Alias listed on a package record."""

    id: str
    name: str | None = None


class PackageAuthor(_Model):
    """Author reference on a package record.

    Only `id` and `active` are sent unless `author_name=True` was requested.
    """

    id: str
    active: bool | None = None
    title: str | None = None
    givenname: str | None = None
    von: str | None = None
    familyname: str | None = None
    junior: str | None = None
    pseudonym: str | None = None
    female: bool | None = None
    died: bool | None = None


class Copyright(_Model):
    """A copyright holder and year(s)."""

    owner: str | None = None
    year: str | None = None


class Description(_Model):
    """Long description; `text` may contain HTML (including `<ref refid=...>`)."""

    text: str | None = None
    language: str | None = None


class Documentation(_Model):
    """Documentation link. `href` starting with `ctan:` is relative to the CTAN tree."""

    details: str | None = None
    language: str | None = None
    href: str | None = None


class CtanLocation(_Model):
    """Location of the package in the CTAN tree."""

    path: str
    file: bool = False


class Package(_Model):
    """Full package record (`pkg/<key>`).

    Attributes
    ----------
    id : str
        CTAN package key (the list endpoint calls this `key`).
    license : list[str]
        License keys; a single string from the API is wrapped in a list.
    ctan : CtanLocation or None
        Location in the CTAN tree.
    install : str or None
        Path of the TDS zip relative to the CTAN `install` directory.
    miktex, texlive : str or None
        Package name in MiKTeX / TeX Live. TeX Live may be a bundle name
        different from `id` (e.g. `binhex` is in `kastrup`).
    topics : list[str]
        Topic keys.
    also : list[str]
        Related package keys.
    index : list[str]
        Extra search terms.
    """

    id: str
    name: str | None = None
    caption: str | None = None
    aliases: list[AliasRef] = []
    authors: list[PackageAuthor] = []
    copyright: list[Copyright] = []
    license: list[str] = []
    version: PackageVersion | None = None
    descriptions: list[Description] = []
    documentation: list[Documentation] = []
    home: str | None = None
    support: str | None = None
    announce: str | None = None
    bugs: str | None = None
    repository: str | None = None
    development: str | None = None
    ctan: CtanLocation | None = None
    install: str | None = None
    miktex: str | None = None
    texlive: str | None = None
    topics: list[str] = []
    also: list[str] = []
    index: list[str] = []

    @field_validator("license", mode="before")
    @classmethod
    def _license_list(cls, v: Any) -> Any:
        if v is None:
            return []
        return [v] if isinstance(v, str) else v

    @field_validator(
        "aliases",
        "authors",
        "copyright",
        "descriptions",
        "documentation",
        "topics",
        "also",
        "index",
        mode="before",
    )
    @classmethod
    def _null_list(cls, v: Any) -> Any:
        return [] if v is None else v

    @field_validator("install", mode="before")
    @classmethod
    def _install_path(cls, v: Any) -> Any:
        # Docs describe an object with `path`; the live API sends a plain string.
        if isinstance(v, dict):
            return v.get("path")
        return v

    @field_validator("version", mode="before")
    @classmethod
    def _version_obj(cls, v: Any) -> Any:
        return None if v == "" else v


class SearchHit(_Model):
    """One search result; `path` is absolute on ctan.org, e.g. `/pkg/amsmath`."""

    title: str | None = None
    path: str | None = None
    text: str | None = None


class SearchResult(_Model):
    """Search response (`/search/json`)."""

    number_of_hits: int = Field(
        default=0, validation_alias=AliasChoices("numberOfHits", "number_of_hits")
    )
    offset: int = 0
    max: int = 0
    phrase: str | None = None
    hits: list[SearchHit] = []


# --------------------------------------------------------------------------- client


def _model_list[T: BaseModel](model: type[T], data: Any) -> list[T]:
    return TypeAdapter(list[model]).validate_python(data)  # ty: ignore[invalid-type-form]


class CtanClient:
    """Synchronous client for the CTAN JSON API 3.0.

    Use as a context manager, or call `close`.

    Parameters
    ----------
    base_url : str
        Site root. The API lives under `<base_url>/json/3.0`, search under
        `<base_url>/search/json`.
    timeout : float
        Request timeout in seconds.
    cache_dir : pathlib.Path or None
        Directory for the SQLite cache. Defaults to `default_cache_dir()`.
    cache : bool
        Set False to disable HTTP caching.
    transport : httpx2.BaseTransport or None
        Underlying transport (for tests, e.g. `httpx2.MockTransport`). It is
        still wrapped in the cache unless `cache=False`.
    client : httpx2.Client or None
        Pre-built client, used as is (no caching added; `transport`,
        `timeout` and `cache*` are ignored).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 10.0,
        cache_dir: Path | None = None,
        cache: bool = True,
        transport: httpx2.BaseTransport | None = None,
        client: httpx2.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return
        next_transport = transport or httpx2.HTTPTransport()
        if cache:
            directory = cache_dir or default_cache_dir()
            next_transport = CacheTransport(
                next_transport,
                storage=SqliteStorage(
                    database_path=directory / "ctan.db", default_ttl=TTL_PACKAGE
                ),
            )
        self._client = httpx2.Client(
            transport=next_transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        # Dropping the CtanClient closes the client and its SQLite cache too.
        self._close = weakref.finalize(self, self._client.close)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client (and cache storage) if this object created it."""
        if self._owns_client:
            self._close()

    # -- plumbing

    def _get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        ttl: float,
        allow_404: bool = False,
    ) -> Any | None:
        """GET and decode JSON. None on 404 when `allow_404`; CtanError otherwise."""
        try:
            resp = self._client.get(
                url, params=params, extensions={"caching": {"ttl": ttl}}
            )
        except httpx2.HTTPError as e:
            msg = f"CTAN request failed: {url}: {e}"
            raise CtanError(msg) from e
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code != 200:
            msg = f"CTAN returned HTTP {resp.status_code} for {url}"
            raise CtanError(msg)
        try:
            return resp.json()
        except ValueError as e:
            msg = f"CTAN returned invalid JSON for {url}"
            raise CtanError(msg) from e

    def _api(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        ttl: float,
        allow_404: bool = False,
    ) -> Any | None:
        return self._get(
            f"{self.base_url}/json/{API_VERSION}/{path}",
            params,
            ttl=ttl,
            allow_404=allow_404,
        )

    @staticmethod
    def _parse[T](parse: Any, data: Any, what: str) -> T:
        try:
            return parse(data)
        except ValidationError as e:
            msg = f"Unexpected CTAN response for {what}: {e}"
            raise CtanError(msg) from e

    @staticmethod
    def _key_params(key: str | None) -> dict[str, str]:
        return {} if key is None else {"key": key}

    # -- endpoints

    def authors(self, key: str | None = None) -> list[Author]:
        """List authors, optionally only those whose key starts with `key`."""
        data = self._api("authors", self._key_params(key), ttl=TTL_STATIC)
        return self._parse(lambda d: _model_list(Author, d), data, "authors")

    def author(self, key: str, *, ref: bool = False) -> Author | None:
        """Fetch one author; None if unknown.

        Parameters
        ----------
        key : str
            Author key.
        ref : bool
            Also return the list of packages mentioning the author.
        """
        params = {"ref": "true"} if ref else None
        data = self._api(f"author/{key}", params, ttl=TTL_STATIC, allow_404=True)
        if data is None:
            return None
        return self._parse(Author.model_validate, data, f"author/{key}")

    def topics(self, key: str | None = None) -> list[Topic]:
        """List topics, optionally only those whose key starts with `key`."""
        data = self._api("topics", self._key_params(key), ttl=TTL_STATIC)
        return self._parse(lambda d: _model_list(Topic, d), data, "topics")

    def topic(self, key: str, *, ref: bool = False) -> Topic | None:
        """Fetch one topic; None if unknown. `ref=True` adds its package keys."""
        params = {"ref": "true"} if ref else None
        data = self._api(f"topic/{key}", params, ttl=TTL_STATIC, allow_404=True)
        if data is None:
            return None
        return self._parse(Topic.model_validate, data, f"topic/{key}")

    def packages(
        self, key: str | None = None, include_aliases: bool = False
    ) -> list[PackageSummary]:
        """List packages whose key starts with `key` (all if None).

        Alias entries (only returned with `include_aliases=True`) are dropped
        here; use `package_aliases` to get them.
        """
        return self._packages_raw(key, include_aliases)[0]

    def package_aliases(self, key: str | None = None) -> list[PackageAlias]:
        """List aliases whose key starts with `key` (all if None)."""
        return self._packages_raw(key, True)[1]

    def _packages_raw(
        self, key: str | None, include_aliases: bool
    ) -> tuple[list[PackageSummary], list[PackageAlias]]:
        params = self._key_params(key)
        if include_aliases:
            params["include-aliases"] = "true"
        data = self._api("packages", params, ttl=TTL_PACKAGE)
        if not isinstance(data, list):
            msg = "Unexpected CTAN response for packages: not a list"
            raise CtanError(msg)
        pkgs = [d for d in data if not (isinstance(d, dict) and "alias" in d)]
        aliases = [d for d in data if isinstance(d, dict) and "alias" in d]
        return (
            self._parse(lambda d: _model_list(PackageSummary, d), pkgs, "packages"),
            self._parse(lambda d: _model_list(PackageAlias, d), aliases, "packages"),
        )

    def package(self, key: str, *, author_name: bool = False) -> Package | None:
        """Fetch a full package record; None if unknown.

        CTAN redirects aliases to the underlying package.

        Parameters
        ----------
        key : str
            Package key or alias.
        author_name : bool
            Include given and family names on the authors.
        """
        params = {"author-name": "true"} if author_name else None
        data = self._api(f"pkg/{key}", params, ttl=TTL_PACKAGE, allow_404=True)
        if data is None:
            return None
        return self._parse(Package.model_validate, data, f"pkg/{key}")

    def licenses(self, key: str | None = None) -> list[License]:
        """List licenses, optionally only those whose key starts with `key`."""
        data = self._api("licenses", self._key_params(key), ttl=TTL_STATIC)
        return self._parse(lambda d: _model_list(License, d), data, "licenses")

    def version(self, api_version: str = API_VERSION) -> Version | None:
        """Interface version reported by `json/<api_version>/version`.

        Returns None when that version has no such endpoint. As of writing the
        live 3.0 API answers 404 here although the docs describe it (2.0 works).
        """
        data = self._get(
            f"{self.base_url}/json/{api_version}/version",
            ttl=TTL_VERSION,
            allow_404=True,
        )
        if data is None:
            return None
        return self._parse(Version.model_validate, data, "version")

    def search(
        self,
        phrase: str,
        *,
        offset: int = 0,
        max: int = 16,  # ruff: ignore[builtin-argument-shadowing]
        packages: bool | None = None,
        authors: bool | None = None,
        topics: bool | None = None,
        portal: bool | None = None,
    ) -> SearchResult:
        """Full-text search (`/search/json`).

        Parameters
        ----------
        phrase : str
            Search phrase.
        offset : int
            First hit to return (paging).
        max : int
            Page size, 1 to 256 (CTAN clamps).
        packages, authors, topics, portal : bool or None
            Restrict to these sections. If any is given, `ext=true` is sent and
            the unspecified sections are sent as false.
        """
        params: dict[str, str] = {
            "phrase": phrase,
            "offset": str(offset),
            "max": str(max),
        }
        sections = {
            "PKG": packages,
            "AUTHORS": authors,
            "TOPICS": topics,
            "PORTAL": portal,
        }
        if any(v is not None for v in sections.values()):
            params["ext"] = "true"
            for name, v in sections.items():
                params[name] = "true" if v else "false"
        data = self._get(f"{self.base_url}/search/json", params, ttl=TTL_SEARCH)
        return self._parse(SearchResult.model_validate, data, "search")

    # -- conveniences

    def texlive_name(self, key: str) -> str | None:
        """TeX Live package name of CTAN package `key`, or None.

        None if the package is unknown or not in TeX Live. Raises `CtanError`
        on network failure.
        """
        pkg = self.package(key)
        return pkg.texlive if pkg and pkg.texlive else None

    def find_texlive(self, name: str) -> list[str] | None:
        """Ask CTAN which TeX Live packages provide `name`, e.g. binhex -> kastrup.

        Looks `name` up as a CTAN key or alias (exact, case-insensitive); an alias
        is followed to the package it points to (`tikz` -> `pgf`). Then reads the
        `texlive` field of each matching package record.

        Returns
        -------
        list[str] or None
            Distinct TeX Live names (possibly empty if CTAN has no match or no
            TeX Live name). None if CTAN could not be reached; callers should
            not cache that.
        """
        try:
            hits, aliases = self._packages_raw(name, True)
        except CtanError:
            return None
        wanted = name.lower()
        keys = [h.key for h in hits if h.key.lower() == wanted]
        keys += [
            a.pkg
            for a in aliases
            if a.pkg and a.alias.lower() == wanted and a.pkg not in keys
        ]
        found: list[str] = []
        for key in keys:
            try:
                tl = self.texlive_name(key)
            except CtanError:
                continue
            if tl and tl not in found:
                found.append(tl)
        return found
