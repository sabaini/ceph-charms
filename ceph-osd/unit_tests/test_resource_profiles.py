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

import unittest

import resource_profiles as rp


def osd(osd_id, tier=rp.TIER_NVME, data_node=0, aux_nodes=None,
        data_device=None):
    """Build OSD facts for the planner."""
    aux_nodes = aux_nodes or {}
    return rp.OsdFacts(
        osd_id=str(osd_id),
        data_device=data_device or '/dev/nvme{}n1'.format(osd_id),
        aux_devices=sorted(aux_nodes),
        tier=tier,
        data_node=data_node,
        aux_nodes=aux_nodes,
    )


def host(nodes=None, nic_nodes=(0,), interface='eth0', networks=()):
    """Build host facts for the planner."""
    if nodes is None:
        nodes = {0: list(range(0, 8)), 1: list(range(8, 16))}
    return rp.HostFacts(nodes=nodes, nic_interface=interface,
                        nic_nodes=set(nic_nodes), networks=tuple(networks))


def nic(interface, address, nodes, problems=()):
    return rp.Nic(interface=interface, address=address,
                  nodes=frozenset(nodes), problems=tuple(problems))


ETH0 = nic('eth0', '10.0.0.5', [0])
ETH1 = nic('eth1', '10.0.1.5', [1])


def networks(public=(ETH0, ETH1), cluster=None, default=ETH0,
             cluster_default=None):
    """Public and cluster network facts; cluster mirrors public."""
    if cluster is None:
        cluster = public
    return [rp.NetworkFacts(rp.ROLE_PUBLIC, default, tuple(public)),
            rp.NetworkFacts(rp.ROLE_CLUSTER, cluster_default or default,
                            tuple(cluster))]


SINGLE_NODE = {0: list(range(0, 16))}


class ProfileDefinitionTestCase(unittest.TestCase):

    def test_core_counts(self):
        self.assertEqual(
            rp.cores_for(rp.PROFILE_PERFORMANCE, rp.TIER_NVME), 12)
        self.assertEqual(
            rp.cores_for(rp.PROFILE_PERFORMANCE, rp.TIER_HDD), 4)
        self.assertEqual(rp.cores_for(rp.PROFILE_BALANCED, rp.TIER_NVME), 8)
        self.assertEqual(rp.cores_for(rp.PROFILE_BALANCED, rp.TIER_HDD), 2)
        self.assertEqual(rp.cores_for(rp.PROFILE_MINIMAL, rp.TIER_NVME), 2)
        self.assertEqual(rp.cores_for(rp.PROFILE_MINIMAL, rp.TIER_HDD), 2)

    def test_strictness(self):
        self.assertTrue(rp.is_strict(rp.PROFILE_PERFORMANCE))
        self.assertFalse(rp.is_strict(rp.PROFILE_BALANCED))
        self.assertFalse(rp.is_strict(rp.PROFILE_MINIMAL))

    def test_managed_profiles(self):
        self.assertFalse(rp.is_managed(rp.PROFILE_UNMANAGED))
        for profile in (rp.PROFILE_PERFORMANCE, rp.PROFILE_BALANCED,
                        rp.PROFILE_MINIMAL):
            self.assertTrue(rp.is_managed(profile))

    def test_tier_from_rotational_flag(self):
        self.assertEqual(rp.tier_for_rotational(True), rp.TIER_HDD)
        self.assertEqual(rp.tier_for_rotational(False), rp.TIER_NVME)
        # An unknown flag is treated as the frugal choice.
        self.assertEqual(rp.tier_for_rotational(None), rp.TIER_HDD)


class UnmanagedPlanTestCase(unittest.TestCase):

    def test_unmanaged_plans_nothing(self):
        plan = rp.build_plan(rp.PROFILE_UNMANAGED, [osd(0)], host())
        self.assertEqual(plan.requests, [])
        self.assertTrue(plan.satisfiable)


class PerformancePlanTestCase(unittest.TestCase):

    def test_aligned_nvme_and_hdd(self):
        osds = [
            osd(0, tier=rp.TIER_NVME, data_node=0),
            osd(1, tier=rp.TIER_HDD, data_node=0, data_device='/dev/sdb'),
        ]
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, osds, host())
        self.assertTrue(plan.satisfiable)
        self.assertEqual([(r.osd_id, r.cores, r.mode, r.numa_node,
                           r.numa_policy) for r in plan.requests],
                         [('0', 12, rp.MODE_NUMA, 0, 'bind'),
                          ('1', 4, rp.MODE_NUMA, 0, 'bind')])
        self.assertEqual(plan.demand_by_node(), {0: 16})
        self.assertEqual(plan.demand_unbound(), 0)

    def test_nic_on_other_node_is_refused(self):
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE,
                             [osd(7, data_node=1)], host(nic_nodes=(0,)))
        self.assertFalse(plan.satisfiable)
        self.assertEqual(len(plan.errors), 1)
        self.assertIn('osd.7', plan.errors[0])
        self.assertIn('node 1', plan.errors[0])
        self.assertIn('eth0', plan.errors[0])
        self.assertEqual(plan.requests, [])

    def test_unknown_nic_node_is_a_deviation_not_an_error(self):
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE,
                             [osd(0, data_node=0)], host(nic_nodes=()))
        self.assertTrue(plan.satisfiable)
        self.assertEqual(len(plan.requests), 1)
        self.assertEqual(len(plan.requests[0].deviations), 1)
        self.assertIn('not verified', plan.requests[0].deviations[0])

    def test_unknown_device_node_is_refused(self):
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE,
                             [osd(0, data_node=None)], host())
        self.assertFalse(plan.satisfiable)
        self.assertIn('unknown', plan.errors[0])

    def test_db_device_on_other_node_is_refused(self):
        osds = [osd(0, data_node=0, aux_nodes={'/dev/nvme9n1': 1})]
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, osds, host())
        self.assertFalse(plan.satisfiable)
        self.assertIn('/dev/nvme9n1', plan.errors[0])

    def test_db_device_with_unknown_node_is_refused(self):
        osds = [osd(0, data_node=0, aux_nodes={'/dev/nvme9n1': None})]
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, osds, host())
        self.assertFalse(plan.satisfiable)
        self.assertIn('unknown node', plan.errors[0])

    def test_single_node_host_satisfies_alignment_trivially(self):
        osds = [osd(0, data_node=None, aux_nodes={'/dev/sdz': None})]
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, osds,
                             host(nodes=SINGLE_NODE, nic_nodes=()))
        self.assertTrue(plan.satisfiable)
        self.assertEqual(plan.requests[0].numa_node, 0)
        self.assertEqual(plan.requests[0].deviations, [])

    def test_partial_failure_refuses_only_offending_osd(self):
        osds = [osd(0, data_node=0), osd(1, data_node=1)]
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, osds,
                             host(nic_nodes=(0,)))
        # The whole plan is unsatisfiable, so the charm applies nothing.
        self.assertFalse(plan.satisfiable)
        self.assertEqual([r.osd_id for r in plan.requests], ['0'])


class BalancedPlanTestCase(unittest.TestCase):

    def test_aligns_data_device_only(self):
        osds = [osd(0, data_node=1, aux_nodes={'/dev/nvme9n1': 0})]
        plan = rp.build_plan(rp.PROFILE_BALANCED, osds, host(nic_nodes=(0,)))
        self.assertTrue(plan.satisfiable)
        request = plan.requests[0]
        self.assertEqual((request.cores, request.mode, request.numa_node,
                          request.numa_policy),
                         (8, rp.MODE_NUMA, 1, 'preferred'))
        # The db device on another node is reported, not refused.
        self.assertEqual(len(request.deviations), 1)
        self.assertIn('/dev/nvme9n1', request.deviations[0])

    def test_hdd_counts(self):
        osds = [osd(3, tier=rp.TIER_HDD, data_node=0,
                    data_device='/dev/sdb')]
        plan = rp.build_plan(rp.PROFILE_BALANCED, osds, host())
        self.assertEqual(plan.requests[0].cores, 2)

    def test_unknown_device_node_falls_back_without_numa(self):
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=None)],
                             host())
        self.assertTrue(plan.satisfiable)
        request = plan.requests[0]
        self.assertEqual(request.mode, rp.MODE_ANY)
        self.assertIsNone(request.numa_node)
        self.assertIsNone(request.numa_policy)
        self.assertIn('without NUMA locality', request.deviations[0])
        self.assertEqual(plan.demand_unbound(), 8)

    def test_device_reporting_unknown_node_id(self):
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=5)],
                             host())
        self.assertTrue(plan.satisfiable)
        self.assertEqual(plan.requests[0].mode, rp.MODE_ANY)

    def test_deviations_are_prefixed_with_the_osd(self):
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(4, data_node=None)],
                             host())
        self.assertTrue(plan.deviations()[0].startswith('osd.4: '))


class MinimalPlanTestCase(unittest.TestCase):

    def test_requests_no_numa_alignment(self):
        osds = [osd(0, data_node=0),
                osd(1, tier=rp.TIER_HDD, data_node=1)]
        plan = rp.build_plan(rp.PROFILE_MINIMAL, osds, host())
        self.assertTrue(plan.satisfiable)
        for request in plan.requests:
            self.assertEqual(request.cores, 2)
            self.assertEqual(request.mode, rp.MODE_ANY)
            self.assertIsNone(request.numa_node)
            self.assertIsNone(request.numa_policy)
            self.assertEqual(request.deviations, [])
        self.assertEqual(plan.demand_by_node(), {})
        self.assertEqual(plan.demand_unbound(), 4)


class CapacityTestCase(unittest.TestCase):

    def test_numa_demand_fits(self):
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=0)],
                             host())
        nodes = {0: list(range(0, 16)), 1: list(range(16, 32))}
        self.assertEqual(
            rp.check_capacity(plan, range(0, 16), nodes), [])

    def test_numa_shortfall_is_reported_per_node(self):
        osds = [osd(0, data_node=0), osd(1, data_node=0)]
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, osds, host())
        nodes = {0: list(range(0, 16)), 1: list(range(16, 32))}
        shortfalls = rp.check_capacity(plan, range(0, 16), nodes)
        self.assertEqual(len(shortfalls), 1)
        self.assertIn('NUMA node 0 needs 24 logical CPUs, 16 available',
                      shortfalls[0])

    def test_unbound_demand_uses_what_numa_left_over(self):
        plan = rp.build_plan(rp.PROFILE_MINIMAL,
                             [osd(0), osd(1), osd(2)], host())
        nodes = {0: list(range(0, 8))}
        self.assertEqual(
            rp.check_capacity(plan, [0, 1, 2, 3, 4, 5], nodes), [])
        shortfalls = rp.check_capacity(plan, [0, 1, 2], nodes)
        self.assertEqual(len(shortfalls), 1)
        self.assertIn('unaligned allocations need 6 logical CPUs, 3 '
                      'available', shortfalls[0])

    def test_mixed_demand(self):
        osds = [osd(0, data_node=0), osd(1, data_node=None)]
        plan = rp.build_plan(rp.PROFILE_BALANCED, osds, host())
        nodes = {0: list(range(0, 8)), 1: list(range(8, 16))}
        # Node 0 satisfies the aligned OSD, leaving node 1 for the other.
        self.assertEqual(rp.check_capacity(plan, range(0, 16), nodes), [])
        shortfalls = rp.check_capacity(plan, range(0, 12), nodes)
        self.assertEqual(len(shortfalls), 1)
        self.assertIn('unaligned allocations', shortfalls[0])


class SerialisationTestCase(unittest.TestCase):

    def test_plan_to_dict(self):
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             host())
        data = plan.to_dict()
        self.assertEqual(data['profile'], rp.PROFILE_BALANCED)
        self.assertEqual(data['osds'], [{
            'osd-id': '0',
            'tier': rp.TIER_NVME,
            'cores': 8,
            'mode': rp.MODE_NUMA,
            'numa-node': 1,
            'numa-policy': 'preferred',
            'addresses': {},
            'deviations': [],
        }])

    def test_request_repr(self):
        plan = rp.build_plan(rp.PROFILE_MINIMAL, [osd(2)], host())
        self.assertIn("osd_id='2'", repr(plan.requests[0]))


class NumaAddressingTestCase(unittest.TestCase):

    def addresses(self, plan):
        return dict((r.osd_id, r.addresses) for r in plan.requests)

    def test_each_osd_binds_its_node_address(self):
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE,
                             [osd(0, data_node=0), osd(1, data_node=1)],
                             host(networks=networks()))
        self.assertTrue(plan.satisfiable, plan.errors)
        self.assertEqual(self.addresses(plan), {
            '0': {'public': '10.0.0.5', 'cluster': '10.0.0.5'},
            '1': {'public': '10.0.1.5', 'cluster': '10.0.1.5'},
        })
        self.assertEqual(plan.deviations(), [])

    def test_single_interface_keeps_the_host_address(self):
        # Today's layout: the strict check applies to the one Ceph NIC.
        single = networks(public=(ETH0,))
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             host(networks=single))
        self.assertEqual(self.addresses(plan), {'0': {}})
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=1)],
                             host(networks=single))
        self.assertIn('Ceph network interface eth0 is on node(s) 0',
                      plan.errors[0])

    def test_single_node_host_never_binds_addresses(self):
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=0)],
                             host(nodes=SINGLE_NODE, networks=networks()))
        self.assertEqual(self.addresses(plan), {'0': {}})

    def test_minimal_and_unaligned_osds_keep_host_addresses(self):
        plan = rp.build_plan(rp.PROFILE_MINIMAL, [osd(0, data_node=1)],
                             host(networks=networks()))
        self.assertEqual(self.addresses(plan), {'0': {}})
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=None)],
                             host(networks=networks()))
        self.assertEqual(self.addresses(plan), {'0': {}})

    def test_missing_node_address(self):
        three_nodes = {0: [0, 1], 1: [2, 3], 2: [4, 5]}
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=2)],
                             host(nodes=three_nodes, networks=networks()))
        self.assertEqual(plan.requests, [])
        self.assertIn('no Ceph public network address on NUMA node 2',
                      plan.errors[0])
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=2)],
                             host(nodes=three_nodes, networks=networks()))
        self.assertEqual(self.addresses(plan), {
            '0': {'public': '10.0.0.5', 'cluster': '10.0.0.5'}})
        self.assertIn('using host address 10.0.0.5', plan.deviations()[0])

    def test_routing_problems(self):
        crossed = nic('eth1', '10.0.1.5', [1],
                      problems=['traffic is routed via eth0'])
        facts = host(networks=networks(public=(ETH0, crossed)))
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=1)],
                             facts)
        self.assertIn('osd.0: Ceph public network address 10.0.1.5 on '
                      'node 1: traffic is routed via eth0', plan.errors)
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             facts)
        self.assertEqual(self.addresses(plan)['0']['public'], '10.0.1.5')
        self.assertEqual(len(plan.deviations()), 2)

    def test_interfaces_spanning_nodes_serve_no_node(self):
        bond = nic('bond0', '10.0.0.5', [0, 1])
        facts = host(nic_nodes=(0, 1), interface='bond0',
                     networks=networks(public=(bond, ETH1), default=bond))
        self.assertEqual(rp.address_nodes(facts.networks[0]), {1})
        self.assertFalse(rp.numa_addressing(facts.networks[0], facts))
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=1)],
                             facts)
        self.assertTrue(plan.satisfiable)
        self.assertEqual(self.addresses(plan), {'0': {}})

    def test_inactive_role_is_checked_strictly(self):
        cluster0 = nic('eth2', '192.168.0.5', [0])
        facts = host(networks=networks(cluster=(cluster0,),
                                       cluster_default=cluster0))
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=1)],
                             facts)
        self.assertIn('Ceph cluster network interface eth2 is on node(s) 0',
                      plan.errors[0])
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             facts)
        self.assertEqual(self.addresses(plan),
                         {'0': {'public': '10.0.1.5'}})

    def test_prefers_the_host_address_on_its_node(self):
        other = nic('eth0', '10.0.0.4', [0])
        facts = host(networks=networks(public=(other, ETH0, ETH1)))
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=0)],
                             facts)
        self.assertEqual(self.addresses(plan)['0']['public'], '10.0.0.5')
        extra = nic('eth3', '10.0.1.4', [1])
        facts = host(networks=networks(public=(ETH0, ETH1, extra)))
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             facts)
        self.assertEqual(self.addresses(plan)['0']['public'], '10.0.1.5')

    def test_network_to_dict(self):
        facts = host(networks=networks())
        data = rp.network_to_dict(facts.networks[0], facts)
        self.assertEqual(data, {
            'role': 'public',
            'source': 'config',
            'default': {'interface': 'eth0', 'address': '10.0.0.5',
                        'numa-nodes': [0], 'problems': []},
            'candidates': [
                {'interface': 'eth0', 'address': '10.0.0.5',
                 'numa-nodes': [0], 'problems': []},
                {'interface': 'eth1', 'address': '10.0.1.5',
                 'numa-nodes': [1], 'problems': []},
            ],
            'numa-addressing': True,
        })
        self.assertIsNone(rp.nic_to_dict(None))


class BindingTrustTestCase(unittest.TestCase):
    """Per-node addresses from a Juju binding must pass the routing check."""

    CROSSED = nic('eth1', '10.0.1.5', [1],
                  problems=['traffic is routed via eth0'])

    def binding(self, public=(ETH0, ETH1)):
        return [rp.NetworkFacts(role, ETH0, tuple(public),
                                rp.SOURCE_BINDING)
                for role in rp.NETWORK_ROLES]

    def addresses(self, plan):
        return dict((r.osd_id, r.addresses) for r in plan.requests)

    def test_clean_binding_binds_node_addresses(self):
        facts = host(networks=self.binding())
        self.assertFalse(rp.untrusted_binding(facts.networks[0], facts))
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=1)],
                             facts)
        self.assertTrue(plan.satisfiable, plan.errors)
        self.assertEqual(self.addresses(plan), {
            '0': {'public': '10.0.1.5', 'cluster': '10.0.1.5'}})
        self.assertEqual(plan.warnings, [])

    def test_misrouted_binding_keeps_the_host_address(self):
        facts = host(networks=self.binding(public=(ETH0, self.CROSSED)))
        self.assertTrue(rp.untrusted_binding(facts.networks[0], facts))
        self.assertFalse(rp.numa_addressing(facts.networks[0], facts))
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             facts)
        self.assertEqual(self.addresses(plan), {'0': {}})
        self.assertEqual(plan.deviations(), [])
        self.assertEqual(len(plan.warnings), 2)
        self.assertEqual(
            plan.warnings[0],
            'not binding OSDs to per-node addresses from the Juju public '
            'binding, which fail the routing check (10.0.1.5: traffic is '
            'routed via eth0); OSDs keep host address 10.0.0.5')
        self.assertEqual(
            rp.network_to_dict(facts.networks[0], facts)['numa-addressing'],
            False)

    def test_misrouted_binding_is_checked_strictly(self):
        facts = host(networks=self.binding(public=(ETH0, self.CROSSED)))
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=1)],
                             facts)
        self.assertIn('Ceph network interface eth0 is on node(s) 0',
                      plan.errors[0])
        plan = rp.build_plan(rp.PROFILE_PERFORMANCE, [osd(0, data_node=0)],
                             facts)
        self.assertTrue(plan.satisfiable, plan.errors)
        self.assertEqual(self.addresses(plan), {'0': {}})

    def test_configured_networks_keep_per_node_addresses(self):
        configured = [rp.NetworkFacts(n.role, n.default, n.candidates)
                      for n in self.binding(public=(ETH0, self.CROSSED))]
        facts = host(networks=configured)
        self.assertFalse(rp.untrusted_binding(configured[0], facts))
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=1)],
                             facts)
        self.assertEqual(self.addresses(plan)['0']['public'], '10.0.1.5')
        self.assertEqual(plan.warnings, [])

    def test_unaligned_candidates_do_not_affect_trust(self):
        virtual = nic('br0', '10.0.2.5', [], problems=['routed via eth0'])
        facts = host(networks=self.binding(public=(ETH0, ETH1, virtual)))
        self.assertFalse(rp.untrusted_binding(facts.networks[0], facts))

    def test_single_node_binding_needs_no_trust(self):
        facts = host(networks=self.binding(public=(ETH0, nic(
            'eth2', '10.0.2.5', [0], problems=['routed via eth1']))))
        self.assertFalse(rp.untrusted_binding(facts.networks[0], facts))
        plan = rp.build_plan(rp.PROFILE_BALANCED, [osd(0, data_node=0)],
                             facts)
        self.assertEqual(plan.warnings, [])

    def test_minimal_does_not_warn(self):
        facts = host(networks=self.binding(public=(ETH0, self.CROSSED)))
        plan = rp.build_plan(rp.PROFILE_MINIMAL, [osd(0, data_node=1)],
                             facts)
        self.assertEqual(plan.warnings, [])
