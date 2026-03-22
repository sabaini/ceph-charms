"""Pytest + jubilant fixtures for ceph-proxy integration testing."""

from typing import Iterator

import jubilant
import pytest

from tests import helpers


@pytest.fixture(scope="session")
def cephclient_deployment() -> helpers.CharmDeployment:
    """Return how integration tests should deploy the Ceph client charm."""
    return helpers.resolve_cephclient_deployment()


@pytest.fixture(scope="module")
def juju_vm_constraints() -> tuple[str, ...]:
    """Default VM constraints for integration tests that need VM machines."""
    return ("virt-type=virtual-machine", "cores=2", "mem=4G", "root-disk=25G")


@pytest.fixture(scope="module")
def juju_vm(
    request: pytest.FixtureRequest,
    juju_vm_constraints: tuple[str, ...],
) -> Iterator[jubilant.Juju]:
    """Provide a temporary Juju model configured for VM-based tests."""
    keep_models = bool(request.config.getoption("--keep-models"))
    with jubilant.temp_model(keep=keep_models) as juju:
        juju.wait_timeout = 60 * 60
        juju.cli("set-model-constraints", *juju_vm_constraints)
        yield juju
        if request.session.testsfailed:
            log = juju.debug_log(limit=1000)
            if log:
                print(log, end="")
