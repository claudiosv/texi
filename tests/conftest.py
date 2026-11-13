import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--network", action="store_true", default=False, help="run live-network tests"
    )


def pytest_configure(config: pytest.Config) -> None:
    """Pin the LaTeX grammar cache before tests point $XDG_CACHE_HOME at tmp dirs.

    The pack is configured once per process; left to the first test that parses,
    it would cache (and re-download) the grammar in that test's tmp dir every run.
    """
    from texi import texscan

    texscan._pack()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--network") or os.environ.get("TEXI_NETWORK") == "1":
        return
    skip = pytest.mark.skip(reason="needs --network or TEXI_NETWORK=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _reset_tlmgr_guard():
    """The tlmgr ban is per command; don't let one test's setting leak into the next."""
    from texi import tlpdb

    yield
    tlpdb.reset_system_cache()


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the number of download retries but not the waiting."""
    from texi import archive

    monkeypatch.setattr(archive.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _no_tex_fmt_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests must not fetch tex-fmt from CTAN."""
    from texi import cli

    monkeypatch.setattr(cli, "_install_tex_fmt", lambda env_dir, **kw: None)
