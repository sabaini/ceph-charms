# Copyright 2026 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OSD resource profiles and the (pure) allocation planner.

This module owns the profile definitions and turns host facts into a
desired allocation plan.  It performs no I/O: discovery lives in
:mod:`host_topology`, claiming CPUs lives in :mod:`epa_client`, and
applying the plan lives in :mod:`resource_apply`.

Only CPU resources are planned in this iteration.  Memory is handled
solely as a NUMA *locality* hint (systemd ``NUMAPolicy``); no memory
sizing, hugepages or Ceph settings are computed here.  On hosts with a
Ceph network address per NUMA node, each OSD is also bound to the
address on its data device's node.
"""

import collections

PROFILE_UNMANAGED = 'unmanaged'
PROFILE_PERFORMANCE = 'performance'
PROFILE_BALANCED = 'balanced'
PROFILE_MINIMAL = 'minimal'

PROFILES = (
    PROFILE_UNMANAGED,
    PROFILE_PERFORMANCE,
    PROFILE_BALANCED,
    PROFILE_MINIMAL,
)

TIER_NVME = 'nvme'
TIER_HDD = 'hdd'

# Logical CPUs per OSD, by profile and device tier.
PROFILE_CORES = {
    PROFILE_PERFORMANCE: {TIER_NVME: 12, TIER_HDD: 4},
    PROFILE_BALANCED: {TIER_NVME: 8, TIER_HDD: 2},
    PROFILE_MINIMAL: {TIER_NVME: 2, TIER_HDD: 2},
}

# systemd NUMAPolicy applied alongside the CPU affinity.  'bind' is a
# strict memory binding, 'preferred' is a hint, None leaves the default.
PROFILE_NUMA_POLICY = {
    PROFILE_PERFORMANCE: 'bind',
    PROFILE_BALANCED: 'preferred',
    PROFILE_MINIMAL: None,
}

# Profiles that refuse to apply anything they cannot satisfy exactly.
STRICT_PROFILES = frozenset([PROFILE_PERFORMANCE])

# Allocation modes for a single OSD.
MODE_NUMA = 'numa'
MODE_ANY = 'any'

OsdFacts = collections.namedtuple(
    'OsdFacts',
    ['osd_id', 'data_device', 'aux_devices', 'tier', 'data_node',
     'aux_nodes'])

# ``nic_interface``/``nic_nodes`` describe the host-wide Ceph interface.
# ``networks`` holds a NetworkFacts per Ceph network role; it is empty when
# per-NUMA-node addresses were not discovered.
HostFacts = collections.namedtuple(
    'HostFacts', ['nodes', 'nic_interface', 'nic_nodes', 'networks'])
HostFacts.__new__.__defaults__ = ((),)

ROLE_PUBLIC = 'public'
ROLE_CLUSTER = 'cluster'
NETWORK_ROLES = (ROLE_PUBLIC, ROLE_CLUSTER)

# One local address able to carry a Ceph network role.  ``nodes`` are the
# NUMA nodes of the interface's physical members.  ``problems`` explain why
# traffic for the address could still cross to another NUMA node's
# interface (routing or ARP); empty when nothing was found.
Nic = collections.namedtuple(
    'Nic', ['interface', 'address', 'nodes', 'problems'])

# Candidate addresses for one Ceph network role.  ``default`` is the
# host-wide address the charm renders into ceph.conf.  ``source`` says
# where the role's networks came from: the charm option or the Juju binding.
NetworkFacts = collections.namedtuple(
    'NetworkFacts', ['role', 'default', 'candidates', 'source'])
SOURCE_CONFIG = 'config'
SOURCE_BINDING = 'binding'
NetworkFacts.__new__.__defaults__ = (SOURCE_CONFIG,)


def nic_to_dict(nic):
    """Render a Nic as plain data (for state and actions)."""
    if nic is None:
        return None
    return {
        'interface': nic.interface,
        'address': nic.address,
        'numa-nodes': sorted(nic.nodes),
        'problems': list(nic.problems),
    }


def address_nodes(network):
    """NUMA nodes served by a candidate address on a single node.

    Interfaces spanning nodes (for example a bond across sockets) and
    interfaces without NUMA information serve no particular node.

    :rtype: set[int]
    """
    return set(
        next(iter(nic.nodes)) for nic in network.candidates
        if len(nic.nodes) == 1)


def _node_problems(network):
    """Problems of the candidates that would serve a NUMA node."""
    return [
        '{}: {}'.format(nic.address, problem)
        for nic in network.candidates if len(nic.nodes) == 1
        for problem in nic.problems]


def untrusted_binding(network, host):
    """Whether per-node addresses from a Juju binding are refused.

    A binding is always present: without an explicit one it is the
    default space, which can hold every subnet on the machine, such as a
    management network on one socket and the storage network on the
    other.  Per-node addresses from a binding are therefore only used
    when they pass the routing check, which a deliberately built
    per-node Ceph network does and such an accidental mix does not.

    :rtype: bool
    """
    return (network.source == SOURCE_BINDING and
            len(host.nodes) > 1 and len(address_nodes(network)) > 1 and
            bool(_node_problems(network)))


def numa_addressing(network, host):
    """Whether OSDs bind a network role to their NUMA node's address.

    This only applies when candidate addresses cover more than one NUMA
    node.  A single Ceph interface keeps the host-wide address, and so do
    per-node addresses from a Juju binding that fail the routing check.

    :rtype: bool
    """
    return (len(host.nodes) > 1 and len(address_nodes(network)) > 1 and
            not untrusted_binding(network, host))


def network_to_dict(network, host):
    """Render a NetworkFacts as plain data (for state and actions)."""
    return {
        'role': network.role,
        'source': network.source,
        'default': nic_to_dict(network.default),
        'candidates': [nic_to_dict(nic) for nic in network.candidates],
        'numa-addressing': numa_addressing(network, host),
    }


def is_managed(profile):
    """Report whether a profile results in charm-managed allocations."""
    return profile in PROFILE_CORES


def is_strict(profile):
    """Report whether a profile refuses partial satisfaction."""
    return profile in STRICT_PROFILES


def tier_for_rotational(rotational):
    """Classify a device tier from its rotational flag.

    Non-rotational devices (NVMe and SATA/SAS SSDs alike) get the NVMe
    core counts; rotational devices get the HDD counts.  An unknown
    flag is treated as rotational, which is the frugal choice.

    :param rotational: rotational flag, None when unknown
    :type rotational: bool or None
    :rtype: str
    """
    if rotational is None:
        return TIER_HDD
    return TIER_HDD if rotational else TIER_NVME


def cores_for(profile, tier):
    """Return the logical CPU count for a profile and device tier.

    :rtype: int
    """
    return PROFILE_CORES[profile][tier]


class OsdRequest(object):
    """The desired allocation for a single OSD."""

    def __init__(self, osd_id, tier, cores, mode, numa_node=None,
                 numa_policy=None, deviations=None, addresses=None):
        self.osd_id = str(osd_id)
        self.tier = tier
        self.cores = cores
        self.mode = mode
        self.numa_node = numa_node
        self.numa_policy = numa_policy
        self.deviations = list(deviations or [])
        # Network role to the address this OSD binds instead of the
        # host-wide ceph.conf one, for roles with per-node addresses.
        self.addresses = dict(addresses or {})

    def __repr__(self):
        return (
            'OsdRequest(osd_id={0.osd_id!r}, tier={0.tier!r}, '
            'cores={0.cores!r}, mode={0.mode!r}, '
            'numa_node={0.numa_node!r})'.format(self)
        )

    def to_dict(self):
        """Render the request as plain data (for state and actions)."""
        return {
            'osd-id': self.osd_id,
            'tier': self.tier,
            'cores': self.cores,
            'mode': self.mode,
            'numa-node': self.numa_node,
            'numa-policy': self.numa_policy,
            'addresses': dict(self.addresses),
            'deviations': list(self.deviations),
        }


class Plan(object):
    """A profile's desired allocation for every local OSD."""

    def __init__(self, profile, requests=None, errors=None, warnings=None):
        self.profile = profile
        self.requests = list(requests or [])
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])

    @property
    def satisfiable(self):
        """True when nothing prevents the plan from being applied."""
        return not self.errors

    def deviations(self):
        """Return every per-OSD deviation, prefixed with the OSD ID."""
        result = []
        for request in self.requests:
            for deviation in request.deviations:
                result.append('osd.{}: {}'.format(request.osd_id, deviation))
        return result

    def demand_by_node(self):
        """Total NUMA-bound logical CPU demand, per NUMA node.

        :rtype: dict[int, int]
        """
        demand = collections.defaultdict(int)
        for request in self.requests:
            if request.mode == MODE_NUMA:
                demand[request.numa_node] += request.cores
        return dict(demand)

    def demand_unbound(self):
        """Total logical CPU demand that is not bound to a NUMA node.

        :rtype: int
        """
        return sum(r.cores for r in self.requests if r.mode == MODE_ANY)

    def to_dict(self):
        """Render the plan as plain data (for state and actions)."""
        return {
            'profile': self.profile,
            'osds': [r.to_dict() for r in self.requests],
            'errors': list(self.errors),
            'warnings': list(self.warnings),
        }


def _target_node(profile, osd, host, deviations, errors):
    """Resolve the NUMA node an OSD should be pinned to.

    :returns: the NUMA node, or None to allocate without NUMA locality
    :rtype: int or None
    """
    single_node = len(host.nodes) <= 1
    if single_node:
        # A single-node host trivially satisfies every alignment
        # requirement, including the NIC and memory constraints.
        return sorted(host.nodes)[0] if host.nodes else 0

    node = osd.data_node
    if node is None:
        message = (
            'NUMA node of data device {} is unknown'.format(osd.data_device))
        if is_strict(profile):
            errors.append(message)
            return None
        deviations.append('{}; allocating without NUMA locality'.format(
            message))
        return None

    if node not in host.nodes:
        message = 'data device {} reports unknown NUMA node {}'.format(
            osd.data_device, node)
        if is_strict(profile):
            errors.append(message)
            return None
        deviations.append('{}; allocating without NUMA locality'.format(
            message))
        return None

    # Auxiliary (block.db / block.wal) devices.
    for device, aux_node in sorted(osd.aux_nodes.items()):
        if aux_node == node:
            continue
        described = 'node {}'.format(aux_node) if aux_node is not None \
            else 'an unknown node'
        message = (
            'device {} is on {} but data device {} is on node {}'.format(
                device, described, osd.data_device, node))
        if is_strict(profile):
            errors.append(message)
            return None
        deviations.append(message)
    return node


def _nodes_text(nodes):
    return ','.join(str(n) for n in sorted(nodes)) or 'none'


def _check_interface(osd, node, interface, nodes, label, deviations,
                     errors):
    """Strictly check that a host-wide Ceph interface is on ``node``."""
    if not nodes:
        deviations.append(
            'NUMA node of {} {} is unknown; NIC alignment not '
            'verified'.format(label, interface or 'unknown'))
        return
    if node not in nodes:
        errors.append(
            'data device {} is on node {} but {} {} is on node(s) '
            '{}'.format(osd.data_device, node, label, interface,
                        _nodes_text(nodes)))


def _local_nic(network, node):
    """Pick the candidate address on ``node``: the host-wide one if local."""
    local = [nic for nic in network.candidates if set(nic.nodes) == {node}]
    for nic in local:
        if network.default and nic.address == network.default.address:
            return nic
    if not local:
        return None
    return sorted(local, key=lambda n: (n.interface or '', n.address))[0]


def _select_addresses(profile, osd, node, host, deviations, errors):
    """Choose the Ceph network addresses an OSD binds on its NUMA node.

    A network role whose candidate addresses cover more than one NUMA
    node binds each OSD to its node's address.  Otherwise the host-wide
    address applies, and only the strict profile checks its locality.

    :returns: mapping of network role to address; empty when every role
        uses the host-wide address
    :rtype: dict[str, str]
    """
    if len(host.nodes) <= 1:
        return {}
    strict = is_strict(profile)
    active = [n for n in host.networks if numa_addressing(n, host)]
    if not active:
        if strict:
            _check_interface(osd, node, host.nic_interface, host.nic_nodes,
                             'Ceph network interface', deviations, errors)
        return {}

    addresses = {}
    for network in host.networks:
        label = 'Ceph {} network'.format(network.role)
        if network not in active:
            if strict:
                default = network.default
                _check_interface(
                    osd, node, default.interface if default else None,
                    default.nodes if default else (),
                    '{} interface'.format(label), deviations, errors)
            continue
        nic = _local_nic(network, node)
        if nic is None:
            message = ('no {} address on NUMA node {} of data device {} '
                       '(addresses serve node(s) {})'.format(
                           label, node, osd.data_device,
                           _nodes_text(address_nodes(network))))
            if strict:
                errors.append(message)
                continue
            if network.default is None:
                deviations.append(message)
                continue
            deviations.append('{}; using host address {}'.format(
                message, network.default.address))
            nic = network.default
        else:
            for problem in nic.problems:
                message = '{} address {} on node {}: {}'.format(
                    label, nic.address, node, problem)
                if strict:
                    errors.append(message)
                else:
                    deviations.append(message)
        addresses[network.role] = nic.address
    return addresses


def build_plan(profile, osds, host):
    """Compute the desired allocation for a profile.

    :param profile: profile name
    :type profile: str
    :param osds: local OSD facts
    :type osds: list[OsdFacts]
    :param host: host topology facts
    :type host: HostFacts
    :rtype: Plan
    """
    if not is_managed(profile):
        return Plan(profile)

    plan = Plan(profile)
    policy = PROFILE_NUMA_POLICY[profile]
    if profile != PROFILE_MINIMAL and osds:
        for network in host.networks:
            if untrusted_binding(network, host):
                plan.warnings.append(
                    'not binding OSDs to per-node addresses from the Juju {} '
                    'binding, which fail the routing check ({}); OSDs keep '
                    'host address {}'.format(
                        network.role, '; '.join(_node_problems(network)),
                        network.default.address if network.default
                        else 'unknown'))
    for osd in sorted(osds, key=lambda o: int(o.osd_id)):
        deviations = []
        errors = []
        cores = cores_for(profile, osd.tier)
        if profile == PROFILE_MINIMAL:
            # The minimal profile deliberately requests no NUMA
            # alignment at all.
            node = None
        else:
            node = _target_node(profile, osd, host, deviations, errors)
        addresses = {}
        if node is not None and not errors:
            addresses = _select_addresses(
                profile, osd, node, host, deviations, errors)
        if errors:
            plan.errors.extend(
                'osd.{}: {}'.format(osd.osd_id, error) for error in errors)
            continue
        mode = MODE_NUMA if node is not None else MODE_ANY
        plan.requests.append(OsdRequest(
            osd_id=osd.osd_id,
            tier=osd.tier,
            cores=cores,
            mode=mode,
            numa_node=node,
            numa_policy=policy if mode == MODE_NUMA else None,
            deviations=deviations,
            addresses=addresses,
        ))
    return plan


def check_capacity(plan, free_cpus, nodes):
    """Verify a plan fits the CPUs the allocator can currently grant.

    This is a preflight check: for strict profiles it lets the charm
    refuse a plan before making any partial claim.

    :param plan: the desired plan
    :type plan: Plan
    :param free_cpus: logical CPUs the charm may claim
    :type free_cpus: iterable[int]
    :param nodes: NUMA node to logical CPU mapping
    :type nodes: dict[int, list[int]]
    :returns: shortfall messages, empty when the plan fits
    :rtype: list[str]
    """
    free = set(free_cpus)
    shortfalls = []
    remaining = len(free)
    for node, demand in sorted(plan.demand_by_node().items()):
        available = len(free & set(nodes.get(node, [])))
        if demand > available:
            shortfalls.append(
                'NUMA node {} needs {} logical CPUs, {} available'.format(
                    node, demand, available))
        remaining -= min(demand, available)
    unbound = plan.demand_unbound()
    if unbound > remaining:
        shortfalls.append(
            'unaligned allocations need {} logical CPUs, {} available'.format(
                unbound, max(remaining, 0)))
    return shortfalls
