# Copyright 2026 Canonical Ltd
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test the network binding functional-test oracle without Juju."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

path = Path(__file__).parents[1] / 'tests/network_binding.py'
spec = importlib.util.spec_from_file_location('network_functest', path)
functest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(functest)

HOST = '10.0.0.5'
LOCAL = '10.77.1.10'
MONS = ['10.0.0.9:3300', '10.0.0.9:6789']
NETWORKS = {'public': {'host-address': HOST},
            'cluster': {'host-address': HOST}}


def evidence(public, cluster, flag, ceph_args=None, outbound=()):
    """A ``network`` probe entry for an OSD listening on its addresses."""
    dropin = '[Service]\nCPUAffinity=4-7\n'
    if ceph_args:
        dropin += 'Environment="CEPH_ARGS={}"\n'.format(ceph_args)
    listening = ['{}:{}'.format(public, port) for port in (6800, 6801)] + [
        '{}:{}'.format(cluster, port) for port in (6802, 6803)]
    return {
        'pid': '123', 'dropin': dropin, 'ceph_args': ceph_args,
        'config': {'public_addr': 'v2:{}:0/0'.format(public),
                   'cluster_addr': 'v2:{}:0/0'.format(cluster),
                   'ms_bind_before_connect': flag},
        'listening': listening,
        # One accepted connection, which uses a listening endpoint.
        'established': [['{}:6800'.format(public), '10.0.0.9:51000']] + [
            list(pair) for pair in outbound],
    }


def advertised(public, cluster):
    return {'up': 1, 'public': ['{}:6800'.format(public),
                                '{}:6801'.format(public)],
            'cluster': ['{}:6802'.format(cluster), '{}:6803'.format(cluster)]}


class AddressParsingTest(unittest.TestCase):

    def test_endpoint_ip(self):
        self.assertEqual(functest.endpoint_ip('10.0.0.5:6800'), '10.0.0.5')
        self.assertEqual(functest.endpoint_ip('[fd00::5]:6800'), 'fd00::5')
        self.assertEqual(
            functest.endpoint_ip('[::ffff:10.0.0.5]:6800'), '10.0.0.5')
        self.assertEqual(
            functest.endpoint_ip('[fe80::1%enp5s0]:22'), 'fe80::1')
        for wildcard in ('*:6800', '0.0.0.0:6800', '[::]:6800'):
            self.assertIsNone(functest.endpoint_ip(wildcard))

    def test_entity_ip(self):
        self.assertEqual(functest.entity_ip('v2:10.0.0.5:0/0'), '10.0.0.5')
        self.assertEqual(functest.entity_ip('v2:[fd00::5]:0/0'), 'fd00::5')
        self.assertEqual(functest.entity_ip('10.0.0.5:0/0'), '10.0.0.5')
        self.assertIsNone(functest.entity_ip('-'))
        self.assertIsNone(functest.entity_ip(None))

    def test_dropin_ceph_args(self):
        self.assertEqual(functest.dropin_ceph_args(
            '[Service]\nEnvironment="CEPH_CONF=/x"\n'
            'Environment="CEPH_ARGS=--public_addr=10.0.1.5 '
            '--ms_bind_before_connect=true"\n'),
            '--public_addr=10.0.1.5 --ms_bind_before_connect=true')
        self.assertIsNone(functest.dropin_ceph_args('[Service]\n'))
        self.assertIsNone(functest.dropin_ceph_args(None))

    def test_address_network(self):
        interfaces = {
            'lo': {'addresses': ['127.0.0.1/8']},
            'enp5s0': {'addresses': ['10.0.0.5/24', 'fd00::5/64']},
            'numa1': {'addresses': ['10.77.1.10/24']}}
        self.assertEqual(str(functest.address_network(
            interfaces, '10.0.0.5')), '10.0.0.0/24')
        self.assertEqual(str(functest.address_network(
            interfaces, 'fd00::5')), 'fd00::/64')
        self.assertIsNone(functest.address_network(interfaces, '10.9.9.9'))


class HelpersTest(unittest.TestCase):

    def test_configured(self):
        self.assertEqual(functest.configured(
            {'value': '10.0.0.0/24', 'source': 'user'}), '10.0.0.0/24')
        self.assertIsNone(functest.configured(
            {'value': 'unmanaged', 'source': 'default'}))
        self.assertIsNone(functest.configured({'source': 'unset'}))

    def test_generations(self):
        self.assertEqual(functest.generations({
            'osd-resource_ceph-osd/1_abc_start': '1',
            'osd-resource_ceph-osd/1_abc_done': '2',
            'osd-resource_ceph-osd/2_def_start': '3',
        }), {'abc': {'ceph-osd/1': {'start': '1', 'done': '2'}},
             'def': {'ceph-osd/2': {'start': '3'}}})


class ExpectationTest(unittest.TestCase):

    def test_override_and_host_addresses(self):
        expected = functest.expected_binding(
            {'applied-public-addr': LOCAL, 'applied-cluster-addr': None},
            NETWORKS)
        self.assertEqual(expected, functest.Expected(
            public=LOCAL, cluster=HOST, public_override=True,
            cluster_override=False))
        self.assertTrue(functest.overridden(expected))
        self.assertEqual(functest.expected_ceph_args(expected), {
            '--public_addr=' + LOCAL, '--ms_bind_before_connect=true'})

    def test_host_wide_addresses(self):
        expected = functest.expected_binding(
            {'applied-public-addr': None, 'applied-cluster-addr': None},
            NETWORKS)
        self.assertFalse(functest.overridden(expected))
        self.assertEqual(functest.expected_ceph_args(expected), set())


def status_osd(desired, applied, desired_mode='numa', applied_mode='numa'):
    """A ``resource-allocation-status`` OSD entry for osd.1."""
    return {'osd-id': '1', 'desired-mode': desired_mode,
            'desired-numa-node': 1, 'applied-mode': applied_mode,
            'applied-cores': '4-7',
            'desired-public-addr': desired, 'desired-cluster-addr': desired,
            'applied-public-addr': applied, 'applied-cluster-addr': applied}


class AllocationProblemsTest(unittest.TestCase):

    def test_applied_desired_addresses_pass(self):
        self.assertEqual(functest.allocation_problems(
            status_osd(LOCAL, LOCAL)), [])
        self.assertEqual(functest.allocation_problems(
            status_osd(None, None, 'any', 'any')), [])

    def test_balanced_fallback_keeps_host_addresses(self):
        # CPUs on any node, or none at all (no applied record).
        for applied_mode in ('any', None):
            self.assertEqual(functest.allocation_problems(
                status_osd(LOCAL, None, applied_mode=applied_mode)), [])

    def test_other_differences_are_reported(self):
        for osd in (status_osd(LOCAL, None),
                    status_osd(LOCAL, '10.77.2.10', applied_mode='any'),
                    status_osd(None, LOCAL)):
            problems = functest.allocation_problems(osd)
            self.assertEqual(len(problems), 1, osd)
            self.assertIn('osd.1: applied addresses', problems[0])


class BindingProblemsTest(unittest.TestCase):

    def setUp(self):
        self.expected = functest.Expected(LOCAL, LOCAL, True, True)
        self.args = ('--public_addr={0} --cluster_addr={0} '
                     '--ms_bind_before_connect=true').format(LOCAL)
        self.evidence = evidence(
            LOCAL, LOCAL, 'true', self.args,
            outbound=[(LOCAL + ':40000', '10.0.0.9:3300'),
                      (LOCAL + ':40001', '10.0.0.9:6800')])
        self.advertised = advertised(LOCAL, LOCAL)

    def problems(self, expected=None):
        return functest.binding_problems(
            '1', expected or self.expected, self.evidence, self.advertised,
            MONS)

    def test_bound_osd_passes(self):
        self.assertEqual(self.problems(), [])

    def test_host_wide_osd_passes(self):
        expected = functest.Expected(HOST, HOST, False, False)
        self.evidence = evidence(HOST, HOST, 'false')
        self.advertised = advertised(HOST, HOST)
        self.assertEqual(self.problems(expected), [])

    def test_separate_roles_pass(self):
        # Only the public address is local; the flag binds both messengers.
        expected = functest.Expected(LOCAL, HOST, True, False)
        args = '--public_addr={} --ms_bind_before_connect=true'.format(LOCAL)
        self.evidence = evidence(
            LOCAL, HOST, 'true', args,
            outbound=[(LOCAL + ':40000', '10.0.0.9:3300'),
                      (HOST + ':40001', '10.0.0.6:6802')])
        self.advertised = advertised(LOCAL, HOST)
        self.assertEqual(self.problems(expected), [])

    def test_conflicting_environment_is_reported(self):
        # /etc/default/ceph replaced the drop-in's CEPH_ARGS.
        self.evidence['ceph_args'] = '--public_addr=' + HOST
        self.evidence['config']['public_addr'] = 'v2:{}:0/0'.format(HOST)
        self.evidence['config']['ms_bind_before_connect'] = 'false'
        problems = self.problems()
        self.assertTrue(any('daemon environment CEPH_ARGS' in p
                            for p in problems), problems)
        self.assertTrue(any('runs with public_addr' in p
                            for p in problems), problems)
        self.assertTrue(any('ms_bind_before_connect false' in p
                            for p in problems), problems)

    def test_missing_dropin_args_are_reported(self):
        self.evidence['dropin'] = '[Service]\n'
        self.assertIn('drop-in CEPH_ARGS', self.problems()[0])

    def test_wildcard_or_wrong_listener_is_reported(self):
        self.evidence['listening'] = ['0.0.0.0:6800', HOST + ':6801']
        problems = self.problems()
        self.assertIn('osd.1: listens on a wildcard address', problems)
        self.assertTrue(any('does not listen on' in p for p in problems))
        self.assertTrue(any('also listens on' in p for p in problems))

    def test_advertised_address_is_checked(self):
        self.advertised = advertised(HOST, LOCAL)
        self.assertTrue(any('advertises public' in p
                            for p in self.problems()))
        self.advertised['up'] = 0
        self.assertIn('osd.1: is not up in the cluster map', self.problems())

    def test_outbound_source_is_checked(self):
        self.evidence['established'].append([HOST + ':40002',
                                             '10.0.0.6:6802'])
        self.assertTrue(any('opens connections from' in p
                            for p in self.problems()))

    def test_monitor_connection_source_is_checked(self):
        expected = functest.Expected(LOCAL, HOST, True, False)
        self.evidence = evidence(
            LOCAL, HOST, 'true',
            '--public_addr={} --ms_bind_before_connect=true'.format(LOCAL),
            outbound=[(HOST + ':40000', '10.0.0.9:3300')])
        self.advertised = advertised(LOCAL, HOST)
        self.assertIn('connects to monitors from', ' '.join(
            self.problems(expected)))

    def test_missing_monitor_connection_is_reported(self):
        self.evidence['established'] = [
            pair for pair in self.evidence['established']
            if not pair[1].endswith(':3300')]
        self.assertIn('osd.1: has no monitor connection to check',
                      self.problems())

    def test_host_wide_osd_ignores_outbound_sources(self):
        expected = functest.Expected(HOST, HOST, False, False)
        self.evidence = evidence(
            HOST, HOST, 'false', outbound=[(LOCAL + ':40000',
                                            '10.0.0.9:3300')])
        self.advertised = advertised(HOST, HOST)
        self.assertEqual(self.problems(expected), [])


def bindings(pids, local=(), cores='4-7'):
    """Verified OSD states; those in ``local`` use the per-node address."""
    return {osd: {
        'unit': 'ceph-osd/1', 'cores': cores, 'pid': pid,
        'expected': functest.Expected(
            LOCAL, LOCAL, True, True) if osd in local else
        functest.Expected(HOST, HOST, False, False)}
        for osd, pid in pids.items()}


class RestartProblemsTest(unittest.TestCase):

    def test_every_osd_whose_binding_changed_restarts(self):
        # osd.1 and osd.2 share the per-node address; osd.0 does not.
        before = bindings({'0': '10', '1': '11', '2': '12'}, local={'1', '2'})
        after = bindings({'0': '10', '1': '21', '2': '22'})
        self.assertEqual(functest.restart_problems(before, after), [])
        unrestarted = bindings({'0': '10', '1': '11', '2': '12'})
        self.assertEqual(functest.restart_problems(before, unrestarted), [
            'osd.1 binding changed but it was not restarted',
            'osd.2 binding changed but it was not restarted'])

    def test_restart_without_a_binding_change_is_reported(self):
        before = bindings({'0': '10', '1': '11'}, local={'1'})
        after = bindings({'0': '20', '1': '21'})
        self.assertEqual(functest.restart_problems(before, after), [
            'osd.0 was restarted without a binding change'])

    def test_changed_cores_and_missing_osds_are_reported(self):
        before = bindings({'1': '11', '2': '12'})
        after = bindings({'1': '11', '3': '13'}, cores='8-11')
        self.assertEqual(functest.restart_problems(before, after), [
            'osd.1 cores changed from 4-7 to 8-11',
            'osd.2 is only present before the change',
            'osd.3 is only present after the change'])


class ScenarioTest(unittest.TestCase):

    def setUp(self):
        self.case = functest.NetworkBindingTest('test_numa_local_addresses')
        self.case.model = 'test-model'
        self.case.units = ['ceph-osd/1']
        self.case.profile = 'balanced'
        self.case.changed = set()
        self.case.zaza = Mock()
        self.case.zaza.get_application_config.return_value = {
            'performance-profile': {'value': 'balanced', 'source': 'user'},
            'ceph-cluster-network': {'value': '10.0.0.5/32',
                                     'source': 'user'},
            'ceph-public-network': {'value': '', 'source': 'default'},
            # Juju omits the value of an unset option without a default.
            'osd-devices': {'source': 'unset'},
        }
        for name in ('_wait', '_markers', '_reset_counters'):
            setattr(self.case, name, Mock(return_value={}))
        self.case._active = Mock(return_value=True)

    def test_change_skips_values_already_in_place(self):
        self.case._change({'performance-profile': 'balanced',
                           'ceph-public-network': None,
                           'osd-devices': None})
        self.case.zaza.set_application_config.assert_not_called()
        self.case.zaza.reset_application_config.assert_not_called()
        self.assertEqual(self.case.changed, set())
        self.assertEqual(self.case._wait.call_args[0][0], 'active units')

    def test_change_resets_and_sets_then_waits_for_a_new_rollout(self):
        self.case._change({'performance-profile': 'minimal',
                           'ceph-cluster-network': None})
        self.case.zaza.reset_application_config.assert_called_once_with(
            'ceph-osd', ['ceph-cluster-network'], model_name='test-model')
        self.case.zaza.set_application_config.assert_called_once_with(
            'ceph-osd', {'performance-profile': 'minimal'},
            model_name='test-model')
        self.assertEqual(self.case.profile, 'minimal')
        self.assertEqual(self.case.changed,
                         {'performance-profile', 'ceph-cluster-network'})
        completed = self.case._wait.call_args[0][1]
        # A stale, already finished generation is not the new rollout.
        self.assertFalse(completed())
        self.case._markers.return_value = {
            'g1': {'ceph-osd/1': {'start': '1', 'done': '2'}}}
        self.assertTrue(completed())

    def test_verify_checks_a_fallback_osd_against_the_applied_state(self):
        self.case.mon = 'ceph-mon/0'
        self.case.artifacts = '/tmp/evidence'
        self.case._save = Mock()
        self.case._unreachable = Mock(return_value=[])
        self.case._allocations = Mock(return_value={'ceph-osd/1': {
            'networks': NETWORKS,
            'osds': [status_osd(LOCAL, None, applied_mode='any')]}})
        network = evidence(HOST, HOST, 'false')
        self.case._probe = Mock(side_effect=lambda unit, mode: {
            'network': {'osds': {'1': network}},
            'advertised': {'osds': {'1': advertised(HOST, HOST)},
                           'mons': MONS}}[mode])
        verified = self.case._verify('fallback')
        self.assertEqual(verified['1']['expected'],
                         functest.Expected(HOST, HOST, False, False))

        # The daemon must still run with the applied host addresses.
        network.update(evidence(LOCAL, LOCAL, 'true'))
        with patch.object(functest.time, 'sleep'), \
                patch.object(functest.time, 'monotonic',
                             side_effect=[0, 0, 200]):
            self.assertRaises(AssertionError, self.case._verify, 'bound')

    def check_address_change(self, narrowed_pids, restored_pids):
        """Run the address change step on two OSDs sharing an address."""
        baseline = bindings({'0': '10', '1': '11', '2': '12'},
                            local={'1', '2'})
        states = {
            'address-only': bindings(narrowed_pids),
            'address-restored': bindings(restored_pids, local={'1', '2'}),
        }
        for state in states.values():
            for osd in ('1', '2'):
                # Only the cluster address moves to the host.
                state[osd]['expected'] = functest.Expected(
                    LOCAL, HOST, True, False)
        states['address-restored'] = dict(
            (osd, dict(state, expected=baseline[osd]['expected']))
            for osd, state in states['address-restored'].items())
        self.case.artifacts = '/tmp/evidence'
        self.case._allocations = Mock(
            return_value={'ceph-osd/1': {'networks': NETWORKS}})
        self.case._probe = Mock(return_value={'interfaces': {
            'enp5s0': {'addresses': [HOST + '/24']},
            'numa1': {'addresses': [LOCAL + '/24']}}})
        self.case._original = Mock(return_value='10.0.0.0/23')
        self.case._change = Mock()
        self.case._verify = Mock(side_effect=lambda name: states[name])
        self.case._check_address_change('1', baseline)
        self.assertEqual(self.case._change.call_args_list[0][0][0],
                         {'ceph-cluster-network': '10.0.0.0/24'})

    def test_address_change_restarts_every_osd_on_the_address(self):
        self.check_address_change({'0': '10', '1': '21', '2': '22'},
                                  {'0': '10', '1': '31', '2': '32'})

    def test_address_change_requires_every_affected_osd_to_restart(self):
        with self.assertRaises(AssertionError) as raised:
            self.check_address_change({'0': '10', '1': '21', '2': '12'},
                                      {'0': '10', '1': '31', '2': '32'})
        self.assertIn('osd.2 binding changed but it was not restarted',
                      str(raised.exception))

    def test_restore_removes_the_conflict_before_restoring_config(self):
        calls = Mock()
        self.case.original = deepcopy(
            self.case.zaza.get_application_config.return_value)
        self.case.original['ceph-cluster-network'] = {'source': 'unset'}
        self.case.changed = {'ceph-cluster-network'}
        for name in ('_remove_conflict', '_change'):
            method = Mock()
            setattr(self.case, name, method)
            calls.attach_mock(method, name)
        self.case._restore()
        self.assertEqual([c[0] for c in calls.mock_calls],
                         ['_remove_conflict', '_change'])
        self.case._change.assert_called_once_with(
            {'ceph-cluster-network': None})
