"""Integration tests for the ceph-proxy charm."""

import json
import logging
import subprocess

import jubilant
import pytest

from tests import helpers

logger = logging.getLogger(__name__)

APP_NAME = "ceph-proxy"
CEPH_MON_APP = "ceph-mon"
CEPH_OSD_APP = "ceph-osd"
CEPHCLIENT_APP = "johnny"
JOHNNY_KEY = "AQCnjmtbuEACMxAA7joUmgLIGI4/3LKkPzUy8g=="


@pytest.fixture(scope="module")
def deployed_apps(
    juju_vm: jubilant.Juju,
    ceph_proxy_charm,
    cephclient_deployment: helpers.CharmDeployment,
) -> tuple[str, ...]:
    """Deploy ceph-proxy, a backing Ceph cluster, and the johnny client."""
    logger.info(
        "Deploying charms: %s, %s, %s, and %s",
        CEPH_MON_APP,
        CEPH_OSD_APP,
        APP_NAME,
        CEPHCLIENT_APP,
    )
    juju_vm.deploy(
        CEPH_OSD_APP,
        CEPH_OSD_APP,
        channel="latest/edge",
        num_units=3,
        storage={"osd-devices": "loop,10G"},
        to=("0", "1", "2"),
    )
    juju_vm.deploy(
        CEPH_MON_APP,
        CEPH_MON_APP,
        channel="latest/edge",
        num_units=3,
        config={"expected-osd-count": 3},
        to=("3", "4", "5"),
    )
    juju_vm.deploy(
        str(ceph_proxy_charm),
        APP_NAME,
        config={"source": "distro"},
        to="6",
    )
    juju_vm.deploy(
        cephclient_deployment.charm,
        CEPHCLIENT_APP,
        channel=cephclient_deployment.channel,
        to="7",
    )
    juju_vm.integrate(f"{CEPH_OSD_APP}:mon", f"{CEPH_MON_APP}:osd")

    with helpers.fast_forward(juju_vm):
        helpers.wait_for_apps(
            juju_vm,
            CEPH_MON_APP,
            CEPH_OSD_APP,
            CEPHCLIENT_APP,
            timeout=60 * 60,
        )
        cluster_config = helpers.wait_for_ceph_cluster_config(
            juju_vm,
            CEPH_MON_APP,
            timeout=30 * 60,
        )
        juju_vm.config(
            APP_NAME,
            {
                "auth-supported": "cephx",
                "admin-key": cluster_config["admin-key"],
                "fsid": cluster_config["fsid"],
                "monitor-hosts": cluster_config["monitor-hosts"],
                "user-keys": f"client.johnny:{JOHNNY_KEY}",
            },
        )
        helpers.wait_for_apps(juju_vm, APP_NAME, timeout=30 * 60)

    return (CEPH_MON_APP, CEPH_OSD_APP, APP_NAME, CEPHCLIENT_APP)


@pytest.fixture(scope="module")
def integrated_apps(
    juju_vm: jubilant.Juju,
    deployed_apps: tuple[str, ...],
) -> tuple[str, ...]:
    """Integrate ceph-proxy with johnny over the ceph relation."""
    juju_vm.integrate(f"{APP_NAME}:client", f"{CEPHCLIENT_APP}:ceph")
    with helpers.fast_forward(juju_vm):
        helpers.wait_for_apps(
            juju_vm,
            APP_NAME,
            CEPHCLIENT_APP,
            timeout=30 * 60,
        )
    return deployed_apps


@pytest.mark.abort_on_fail
@pytest.mark.smoke
def test_build_and_configure(
    juju_vm: jubilant.Juju,
    deployed_apps: tuple[str, ...],
) -> None:
    """Deploy the test model, configure ceph-proxy, and ensure it settles."""
    status = juju_vm.status()
    assert jubilant.all_active(status, *deployed_apps)


@pytest.mark.abort_on_fail
@pytest.mark.smoke
def test_user_keys_with_generic_ceph_client(
    juju_vm: jubilant.Juju,
    integrated_apps: tuple[str, ...],
) -> None:
    """Verify configured user-keys are shared with a ceph-client app."""
    status = juju_vm.status()
    assert jubilant.all_active(status, *integrated_apps)

    johnny_unit = helpers.first_unit_name(status, CEPHCLIENT_APP)
    ceph_proxy_unit = helpers.first_unit_name(status, APP_NAME)
    relation_data = helpers.wait_for_relation_data(
        johnny_unit,
        "ceph",
        ceph_proxy_unit,
        juju_vm.model,
        lambda data: (
            data.get("key") == JOHNNY_KEY
            and data.get("auth") == "cephx"
            and bool(data.get("ceph-public-address"))
        ),
        timeout=10 * 60,
        interval=10,
    )
    assert relation_data["key"] == JOHNNY_KEY
    assert relation_data["auth"] == "cephx"
    assert relation_data["ceph-public-address"]

    broker_data, broker_rsp_key = helpers.wait_for_broker_response(
        juju_vm,
        CEPHCLIENT_APP,
        APP_NAME,
        timeout=20 * 60,
    )
    broker_rsp_value = json.loads(broker_data[broker_rsp_key])
    assert broker_rsp_value.get("exit-code") == 0

    helpers.wait_for_pool(
        juju_vm,
        CEPH_MON_APP,
        "johnny-metadata",
        timeout=20 * 60,
    )

    mon_unit = helpers.first_unit_name(juju_vm.status(), CEPH_MON_APP)
    proc = subprocess.run(
        [
            "juju",
            "ssh",
            "-m",
            juju_vm.model,
            mon_unit,
            "sudo",
            "ceph",
            "auth",
            "get",
            "client.johnny",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "failed to find client.johnny" in f"{proc.stdout}\n{proc.stderr}"
