"""Pytest + jubilant fixtures for testing."""

from pathlib import Path
from typing import Iterator

import jubilant
import pytest

from tests import helpers


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add options."""
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help=(
            "Do not destroy the temporary Juju models created "
            "for integration tests."
        ),
    )


def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo,
) -> None:
    """Abort the test session if an abort_on_fail test fails."""
    if (
        call.when == "call"
        and call.excinfo
        and item.get_closest_marker("abort_on_fail")
    ):
        item.session.shouldstop = "abort_on_fail marker triggered"


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> Iterator[jubilant.Juju]:
    """Provide a temporary Juju model managed by Jubilant."""
    keep_models = bool(request.config.getoption("--keep-models"))
    with jubilant.temp_model(keep=keep_models) as juju:
        juju.wait_timeout = 20 * 60
        yield juju
        if request.session.testsfailed:
            log = juju.debug_log(limit=1000)
            if log:
                print(log, end="")


@pytest.fixture(scope="session")
def ceph_proxy_charm() -> Path:
    """Return the built ceph-proxy charm artifact."""
    return helpers.resolve_ceph_proxy_artifact()
