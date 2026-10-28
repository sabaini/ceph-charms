# EPA resource-profile functional tests

This is a **disruptive, opt-in Zaza suite** for disposable models. It changes
profiles, restarts OSDs, resets systemd start-limit counters, and stops/masks one
OSD on the middle rollout host. Do not run it against production or a shared
model. It is deliberately separate from `tests/tests.yaml` and the default CI
bundles: the EPA snap is not yet published to the store.

## Coverage

The suite normalizes to a healthy `balanced` baseline, then verifies:

- `balanced -> minimal -> balanced` rollouts, with fresh generations and every
  host completing before its successor starts;
- real OSD process identity, systemd drop-ins, every observed thread's CPU
  affinity, and disjoint non-preemptive EPA claims;
- a second transition to `minimal`, with the first OSD on the **middle host**
  stopped and masked before the rollout;
- first-host completion, explicit middle-host failure, and no last-host start
  marker or change to its PIDs, claims, affinity or drop-ins;
- a queued `balanced` request that cannot supersede the unfinished generation;
- recovery after unmasking and a config-change retry, **without manually
  starting the OSD**, including a pending restart with an unchanged drop-in;
- completion of the old generation before the new generation starts, with
  host order preserved and no overlap in actual systemd restart windows;
- final Ceph health and allocation state.

Host order is discovered from `(hostname, unit name)`, matching the charm. No
OSD IDs, unit numbering order, IP addresses, generation IDs or LXD instance
names are hard-coded. The test uses Zaza/Juju to execute on units, not `lxc`.
The default bundle provides three separate 8-vCPU VMs, each with one MON and
two loop-backed OSD block devices. Allow roughly 36 GiB RAM and 96 GiB root
storage. Synchronized guest clocks are needed for timestamp comparisons.

This suite isolates the injected failure by resetting the OSD start counters;
it does not test exhaustion of Ceph's 3-starts/30-minute limit. It also does not
test VM power loss, leadership changes, `update-status` immutability, continuous
RADOS traffic, or physical multi-NUMA/SSD placement. Those remain separate
scenarios rather than implicit claims of this test.

## Build and deploy locally

The bundle uses `ch:ceph-mon` from `tentacle/edge`. Build the current ceph-osd
charm for Ubuntu 24.04 and the EPA snap from its local source tree using
Charmcraft/Snapcraft. For example:

```sh
(cd ceph-osd && charmcraft -v pack --use-lxd --platform ubuntu-24.04-amd64)
(cd ~/src/snap-epa-orchestrator && snapcraft -v pack --use-lxd)
```

Charmcraft packages the checked-in `ceph-osd/lib/charms_ceph` library directly.
After changing `charms.ceph/charms_ceph`, refresh that vendored copy before
building.

From the ceph-charms repository root, export **absolute paths** to those two
artifacts. The bundle uses the specified OSD charm and EPA snap directly,
without a store fallback or automatic selection of an old local artifact.

```sh
export TEST_EPA_OSD_CHARM="$PWD/ceph-osd/ceph-osd_ubuntu-24.04-amd64.charm"
export TEST_EPA_SNAP="$HOME/src/snap-epa-orchestrator/epa-orchestrator_2026.1-06648a34_amd64.snap"
# Adjust the snap filename to your actual build output.
export TEST_EPA_ARTIFACTS="$HOME/epa-functest-results"

tox -c ceph-osd -e func-resource-profiles
```

Zaza's strict Jinja rendering rejects missing artifact environment variables.
`TEST_*` variables are passed through by the charm's existing tox configuration.
Select a local LXD Juju controller before invoking the runner if testing locally;
Zaza creates a fresh model on the selected controller. `--keep-model` retains it
for inspection. The bundle's `loop` storage follows the existing Ceph functional
bundles and does not require formatting physical host disks.

The equivalent command from `ceph-osd/` in an environment with the existing
functional test requirements installed is:

```sh
functest-run-suite --keep-model --test-directory tests/resource-profiles
```

## Run against an existing disposable deployment

The model must have the current EPA-enabled charm, the local snap resource
attached, exactly three OSD units on distinct hosts, an OSD on each host, and a
healthy Ceph cluster. Allow enough CPU capacity to satisfy balanced/minimal
without warnings. Do not run other config changes, upgrades or failure tests
concurrently.

From `ceph-osd/`:

```sh
functest-test --model lxd-controller:MY-DISPOSABLE-MODEL \
  --test-directory tests/resource-profiles \
  --tests tests.resource_profiles.ResourceProfileRolloutTest
```

## Evidence and cleanup

Each run writes a unique subdirectory beneath `TEST_EPA_ARTIFACTS` (default
`/tmp/ceph-epa-functest`). It contains snapshots before/after each transition,
MON markers, allocation-action results, systemd journal events, restart windows
and the latest status. Keep the Zaza runner log as well.

Cleanup is registered before mutation. It captures evidence before repair,
unmasks the injected OSD even after assertions fail, resets start counters,
reconciles a healthy managed profile, then restores the original profile and
warning option. Cleanup errors fail the test; inspect the retained model if
repair cannot complete. As documented by the charm, restoring `unmanaged`
retains CPU allocations and drop-ins; it does not undo enforcement or uninstall
EPA. The model is not destroyed by the test.

The test oracle itself has fast unit tests (no Zaza dependency needed):

```sh
python3 -m unittest discover -s ceph-osd/unit_tests \
  -p test_resource_profile_functest.py -v
```

## NUMA-local network binding

`tests/network_binding.py` is a second disruptive, opt-in scenario. It needs a
host topology that a bundle cannot create, so it only runs against an existing
disposable model.

### Coverage

The expected addresses of each OSD come from `resource-allocation-status`
(`applied-*-addr`, or the host-wide address). They must match the desired
addresses, except for the documented `balanced` fallback: an OSD planned for a
NUMA node that was allocated on any node, or left unpinned, keeps the
host-wide addresses. On the hosts and in the cluster the test then checks:

- `CEPH_ARGS` in the drop-in and in the running daemon's environment;
- the effective `public_addr`, `cluster_addr` and `ms_bind_before_connect`,
  read through the admin socket;
- that the OSD listens only on its expected addresses, that the OSD map
  advertises them, and that every advertised endpoint accepts a TCP
  connection from the MON unit (public) and from another OSD unit, or the MON
  unit when there is none (cluster);
- for OSDs with a per-node address, that every connection the OSD opens
  leaves from one of its own addresses, and its monitor session from its
  public address.

The scenario:

1. Applies `balanced` (`TEST_NETWORK_PROFILE=performance` for the strict
   profile) and verifies the baseline. At least one OSD must use a per-node
   address.
2. Narrows `ceph-cluster-network` (or `ceph-public-network` when only the
   public address is per-node) to the subnet of the host-wide address, then
   restores it. Its other address stays per-node. The option applies to
   every OSD, so in each direction exactly the OSDs whose binding changes
   restart, and no OSD's CPUs change. The step is skipped with a warning
   when the per-node address is in the same subnet.
3. Moves to `minimal`, where every OSD uses the host-wide addresses, and back.
4. Adds `CEPH_ARGS=--public_addr=<host address>` to `/etc/default/ceph`,
   which systemd lets override the drop-in, and restarts that OSD:
   `update-status` must block the unit. Removing the line and restarting the
   OSD again clears the block.

Administratively down interfaces are covered by the discovery and
reconciliation unit tests instead: systemd-networkd removes the addresses of
an interface that is set down, so that case cannot be staged reliably here.

### Topology

The model needs an OSD host with at least two NUMA nodes, and an OSD whose
device is on a node with an interface holding an address of its own in
`ceph-public-network`/`ceph-cluster-network`, or in the `public`/`cluster`
bindings. That address needs source-based routing, as described under
*Network requirements* in `ceph-osd/RESOURCE-PROFILES.md`.

Without such hardware, `emulate-numa-network.sh` builds one from an LXD VM.
The LXD host needs two NUMA nodes. Pick CPUs for the VM from both nodes
(`/sys/devices/system/node/node*/cpulist`); here node 0 holds 0-11 and
node 1 holds 12-23:

```sh
juju add-model numa-net
juju model-config default-base=ubuntu@24.04
juju add-machine --constraints "virt-type=virtual-machine cores=16 mem=16G root-disk=40G"
# Use machine 0's LXD instance name, from `juju status`.
ceph-osd/tests/resource-profiles/emulate-numa-network.sh juju-0a1b2c-0 4-11,16-23
juju add-machine    # a container for the MON
juju deploy ceph-mon --channel tentacle/edge --to 1 --config source=distro \
    --config monitor-count=1 --config expected-osd-count=2
juju deploy "$TEST_EPA_OSD_CHARM" ceph-osd --to 0 \
    --resource epa-orchestrator="$TEST_EPA_SNAP" --config source=distro \
    --config performance-profile=balanced \
    --config osd-devices="/dev/disk/by-id/nvme-QEMU_NVMe_Ctrl_lxd_osd--n0 /dev/disk/by-id/nvme-QEMU_NVMe_Ctrl_lxd_osd--n1" \
    --config ceph-public-network="<LXD bridge subnet> 10.77.1.0/24"
juju integrate ceph-osd:mon ceph-mon:osd
```

The script gives the VM a second NIC on its own LXD bridge (`10.77.1.10`,
routed through `10.77.1.1` by source), an NVMe disk on each node, and a boot
service that reports the second NIC and the `osd-n1` disk on node 1. The
cluster network follows the `cluster` binding, so the OSD on the `osd-n1` disk
binds both of its addresses to `10.77.1.10`.

### Run

From `ceph-osd/`:

```sh
functest-test --model lxd-controller:numa-net \
  --test-directory tests/resource-profiles \
  --tests tests.network_binding.NetworkBindingTest
```

Evidence goes to a `network-<id>` directory beneath `TEST_EPA_ARTIFACTS`: the
allocation status and network probe of every step. Cleanup removes the
`/etc/default/ceph` line, restarts that OSD and restores every changed option.
The oracle has unit tests:

```sh
python3 -m unittest discover -s ceph-osd/unit_tests \
  -p test_network_binding_functest.py -v
```

## EPA source compatibility check

The socket contract check runs without Juju, a snap installation, or live OSDs.
It uses the charm client with the real EPA dispatcher and persistence code,
with temporary state and simulated CPU topology. From the repository root:

```sh
git clone https://github.com/canonical/snap-epa-orchestrator /tmp/epa-source
python3 -m venv /tmp/epa-contract-venv
/tmp/epa-contract-venv/bin/pip install 'pydantic>=2,<3'
PYTHONPATH=/tmp/epa-source /tmp/epa-contract-venv/bin/python \
    ceph-osd/tests/epa_contract.py
```

This covers general-pool selection, isolation from legacy owners, NUMA and
count allocations, restart persistence, release with missing online topology,
and failed persistence. It does not replace the installed-snap rollout suite
above. The charm requires both `cpu-pools` and `non-preemptive-allocations`;
an externally managed snap must have its general pool configured and restarted.
