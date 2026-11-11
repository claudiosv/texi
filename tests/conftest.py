import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--network", action="store_true", default=False, help="run live-network tests"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--network") or os.environ.get("TEXI_NETWORK") == "1":
        return
    skip = pytest.mark.skip(reason="needs --network or TEXI_NETWORK=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
