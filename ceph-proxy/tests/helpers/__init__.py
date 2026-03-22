"""Shared helper functions for ceph-proxy integration tests."""

import contextlib
import functools
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

import jubilant

DEFAULT_TIMEOUT = 3600
CHARM_ROOT = Path(__file__).resolve().parents[2]
MONOREPO_ROOT = CHARM_ROOT.parent
WORKSPACE_ROOT = MONOREPO_ROOT.parent


class CharmDeployment(NamedTuple):
    """Charm reference and optional channel to deploy."""

    charm: str
    channel: str | None = None


@functools.lru_cache(maxsize=1)
def ensure_charmcraft() -> None:
    """Install charmcraft snap if it is not already available."""
    if shutil.which("charmcraft"):
        return
    subprocess.run(
        ["sudo", "snap", "install", "charmcraft", "--classic"],
        check=True,
    )


def _artifact_name_for_source(source_dir: Path) -> str:
    """Return the built charm artifact name for a source checkout."""
    for line in (source_dir / "metadata.yaml").read_text().splitlines():
        if line.startswith("name:"):
            charm_name = line.split(":", 1)[1].strip()
            return f"{charm_name}.charm"
    raise ValueError(
        "Unable to determine charm name from "
        f"{source_dir / 'metadata.yaml'}"
    )


def build_charm(
    charm_dir: Path,
    *,
    artifact_name: str,
    rebuild: bool = True,
) -> Path:
    """Build a charm at *charm_dir* and return the resulting artifact."""
    artifact = charm_dir / artifact_name
    if not rebuild and artifact.exists():
        return artifact.resolve()

    ensure_charmcraft()
    subprocess.run(["charmcraft", "-v", "pack"], check=True, cwd=charm_dir)
    built_charms = sorted(
        charm_dir.glob("*.charm"),
        key=lambda charm: charm.stat().st_mtime,
        reverse=True,
    )
    if not built_charms:
        raise FileNotFoundError(f"No charm artifacts produced in {charm_dir}")

    latest_artifact = built_charms[0]
    if latest_artifact != artifact:
        latest_artifact.rename(artifact)

    if artifact.exists():
        return artifact.resolve()
    raise FileNotFoundError(
        f"Expected charm artifact {artifact} was not produced"
    )


@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju) -> Iterator[None]:
    """Temporarily run update-status hooks every 10 seconds."""
    current_config = juju.model_config()
    previous_interval = current_config.get(
        "update-status-hook-interval",
        "5m",
    )
    juju.model_config({"update-status-hook-interval": "10s"})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": previous_interval})


def wait_for_apps(
    juju: jubilant.Juju,
    *apps: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """Wait for *apps* to reach an active state."""
    juju.wait(
        lambda status: jubilant.all_active(status, *apps),
        error=lambda status: jubilant.any_error(status, *apps),
        timeout=timeout,
    )


def first_unit_name(status: jubilant.Status, app: str) -> str:
    """Return the first unit name for *app*."""
    units = status.apps[app].units
    if not units:
        raise AssertionError(f"{app} has no units")
    return next(iter(units))


def get_unit_info(unit: str, model: str) -> dict[str, Any]:
    """Return ``juju show-unit`` data for a specific unit."""
    proc = subprocess.run(
        ["juju", "show-unit", "-m", model, unit, "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout.strip() or "{}")
    if unit not in data:
        raise KeyError(f"No unit info available for {unit}: {data!r}")
    return data[unit]


def get_relation_data(
    unit: str,
    endpoint: str,
    related_unit: str,
    model: str,
) -> dict[str, str]:
    """Return relation data for a local endpoint and remote unit."""
    unit_data = get_unit_info(unit, model)
    for relation in unit_data.get("relation-info", []):
        related_units = relation.get("related-units", {})
        if (
            endpoint == relation.get("endpoint")
            and related_unit in related_units
        ):
            return related_units.get(related_unit, {}).get("data", {})
    return {}


def wait_for_relation_data(
    unit: str,
    endpoint: str,
    related_unit: str,
    model: str,
    predicate: Callable[[dict[str, str]], bool],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    interval: int = 10,
) -> dict[str, str]:
    """Poll relation data until *predicate* returns true."""
    deadline = time.time() + timeout
    last_data: dict[str, str] = {}

    while time.time() < deadline:
        try:
            last_data = get_relation_data(unit, endpoint, related_unit, model)
        except (subprocess.CalledProcessError, KeyError):
            last_data = {}
        if predicate(last_data):
            return last_data
        time.sleep(interval)

    raise AssertionError(
        "Timed out waiting for relation data on "
        f"{unit}:{endpoint} from {related_unit}; "
        f"last data: {last_data}"
    )


def wait_for_broker_response(
    juju: jubilant.Juju,
    requirer_app: str,
    provider_app: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    interval: int = 10,
) -> tuple[dict[str, str], str]:
    """Poll relation data until the requirer broker response is present."""
    status = juju.status()
    requirer_unit = first_unit_name(status, requirer_app)
    provider_unit = first_unit_name(status, provider_app)
    broker_rsp_key = f"broker-rsp-{requirer_unit.replace('/', '-')}"
    data = wait_for_relation_data(
        requirer_unit,
        "ceph",
        provider_unit,
        juju.model,
        lambda current: broker_rsp_key in current,
        timeout=timeout,
        interval=interval,
    )
    return data, broker_rsp_key


def _extract_mon_host(addr: str) -> str:
    """Extract the host portion from a Ceph monitor address string."""
    value = addr.strip()
    if value.startswith("v") and ":" in value:
        _, value = value.split(":", 1)
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.split(":", 1)[0]


def _mon_hosts_from_dump(mon_dump: dict[str, Any]) -> list[str]:
    """Return unique plain monitor IPs from ``ceph mon dump`` output."""
    hosts: list[str] = []
    for monitor in mon_dump.get("mons", []):
        addrvec = monitor.get("public_addrs", {}).get("addrvec", [])
        addresses = [
            entry.get("addr", "") for entry in addrvec if entry.get("addr")
        ]
        if not addresses:
            for key in ("public_addr", "addr"):
                if monitor.get(key):
                    addresses = [monitor[key]]
                    break
        for address in addresses:
            host = _extract_mon_host(address)
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def wait_for_ceph_cluster_config(
    juju: jubilant.Juju,
    app: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    interval: int = 15,
) -> dict[str, str]:
    """Wait until Ceph metadata needed by ceph-proxy is available."""
    deadline = time.time() + timeout
    last_error: str | None = None

    while time.time() < deadline:
        try:
            unit_name = first_unit_name(juju.status(), app)
            fsid = juju.ssh(unit_name, "sudo", "ceph", "fsid").strip()
            admin_key = juju.ssh(
                unit_name,
                "sudo",
                "ceph",
                "auth",
                "get-key",
                "client.admin",
            ).strip()
            mon_dump_raw = juju.ssh(
                unit_name,
                "sudo",
                "ceph",
                "mon",
                "dump",
                "-f",
                "json",
            )
            mon_dump = json.loads(mon_dump_raw)
            monitor_hosts = " ".join(_mon_hosts_from_dump(mon_dump))
            if fsid and admin_key and monitor_hosts:
                return {
                    "fsid": fsid,
                    "admin-key": admin_key,
                    "monitor-hosts": monitor_hosts,
                }
            last_error = (
                "Ceph metadata incomplete: "
                f"fsid={bool(fsid)} admin-key={bool(admin_key)} "
                f"monitor-hosts={monitor_hosts!r}"
            )
        except (
            json.JSONDecodeError,
            subprocess.CalledProcessError,
            AssertionError,
        ) as exc:
            last_error = str(exc)
        time.sleep(interval)

    raise AssertionError(
        "Timed out waiting for Ceph cluster config data; "
        f"last error: {last_error}"
    )


def wait_for_pool(
    juju: jubilant.Juju,
    app: str,
    pool_name: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    interval: int = 15,
) -> list[str]:
    """Wait until *pool_name* exists in the target Ceph cluster."""
    deadline = time.time() + timeout
    last_pools: list[str] = []

    while time.time() < deadline:
        try:
            unit_name = first_unit_name(juju.status(), app)
            raw_output = juju.ssh(
                unit_name,
                "sudo",
                "ceph",
                "osd",
                "pool",
                "ls",
                "--format",
                "json",
            )
            pools = json.loads(raw_output)
            if isinstance(pools, list):
                last_pools = [
                    pool.get("poolname", "")
                    if isinstance(pool, dict)
                    else str(pool)
                    for pool in pools
                ]
            if pool_name in last_pools:
                return last_pools
        except (
            json.JSONDecodeError,
            subprocess.CalledProcessError,
            AssertionError,
        ):
            last_pools = []
        time.sleep(interval)

    raise AssertionError(
        f"Timed out waiting for Ceph pool {pool_name!r}; "
        f"last pools: {last_pools}"
    )


def resolve_ceph_proxy_artifact() -> Path:
    """Return a built ceph-proxy artifact.

    Reuse CI-produced artifacts when they are already present.
    """
    for candidate in (
        MONOREPO_ROOT / "ceph-proxy.charm",
        CHARM_ROOT / "ceph-proxy.charm",
    ):
        if candidate.exists():
            return candidate.resolve()
    return build_charm(
        CHARM_ROOT,
        artifact_name="ceph-proxy.charm",
        rebuild=True,
    )


def resolve_cephclient_deployment() -> CharmDeployment:
    """Resolve how integration tests should deploy the johnny client charm."""
    artifact_override = os.environ.get("CLIENT_CHARM")
    if artifact_override:
        artifact = Path(artifact_override).expanduser().resolve()
        if not artifact.exists():
            raise FileNotFoundError(
                f"Ceph client charm override not found: {artifact}"
            )
        return CharmDeployment(str(artifact))

    source_override = os.environ.get("CLIENT_SOURCE")
    if source_override:
        source_dir = Path(source_override).expanduser().resolve()
        if not source_dir.exists():
            raise FileNotFoundError(
                f"Ceph client source override not found: {source_dir}"
            )
        artifact = build_charm(
            source_dir,
            artifact_name=_artifact_name_for_source(source_dir),
        )
        return CharmDeployment(str(artifact))

    sibling_source = WORKSPACE_ROOT / "johnny"
    if (sibling_source / "charmcraft.yaml").exists():
        artifact = build_charm(
            sibling_source,
            artifact_name=_artifact_name_for_source(sibling_source),
        )
        return CharmDeployment(str(artifact))

    return CharmDeployment(
        os.environ.get("CLIENT_NAME", "johnny"),
        os.environ.get("CLIENT_CHANNEL", "edge"),
    )
