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

"""Opt-in Zaza test of NUMA-local OSD network address binding.

Needs a disposable model whose OSD host has an address of its own on a
NUMA node holding an OSD; see resource-profiles/README.md. Never run it
against a production or shared model.
"""

import asyncio
import base64
import collections
import ipaddress
import json
import logging
import os
from pathlib import Path
import shlex
import time
import unittest
import uuid


LOG = logging.getLogger(__name__)
ROLES = ('public', 'cluster')
# Marks the operator CEPH_ARGS line the conflict step adds and removes.
CONFLICT_MARKER = '# ceph-osd network binding functest'


def generations(markers):
    """Group rollout markers by generation, then unit."""
    result = {}
    for key, value in markers.items():
        unit, generation, phase = key.removeprefix('osd-resource_').rsplit(
            '_', 2)
        result.setdefault(generation, {}).setdefault(unit, {})[phase] = value
    return result


async def _status_action(model, unit):
    import zaza.model
    return await asyncio.wait_for(zaza.model.async_run_action(
        unit, 'resource-allocation-status', model_name=model,
        action_params={'format': 'json'}, raise_on_failure=True), timeout=90)


def endpoint_ip(endpoint):
    """Return the IP of ``address:port`` (IPv6 in brackets), or None.

    None stands for a wildcard address. IPv4-mapped IPv6 addresses are
    returned as IPv4.
    """
    host = endpoint.rsplit(':', 1)[0].strip('[]').split('%')[0]
    if host == '*':
        return None
    address = ipaddress.ip_address(host)
    if address.is_unspecified:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def endpoint_key(endpoint):
    """Normalize ``address:port`` for comparison."""
    return endpoint_ip(endpoint), int(endpoint.rsplit(':', 1)[1])


def entity_ip(value):
    """Return the IP of an effective Ceph address such as ``v2:1.2.3.4:0/0``.

    :returns: the address, or None when Ceph has none (``-``)
    """
    if not value or value == '-':
        return None
    kind, _, rest = value.partition(':')
    if kind not in ('v1', 'v2', 'any'):
        rest = value
    return endpoint_ip(rest.rsplit('/', 1)[0])


def dropin_ceph_args(dropin):
    """Return the CEPH_ARGS a charm drop-in passes, or None."""
    prefix = 'Environment="CEPH_ARGS='
    for line in (dropin or '').splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].rstrip('"')
    return None


Expected = collections.namedtuple(
    'Expected', ['public', 'cluster', 'public_override', 'cluster_override'])


def expected_binding(osd, networks):
    """Derive an OSD's expected addresses from the status action.

    :param osd: one ``osds`` entry of ``resource-allocation-status``
    :param networks: its ``networks`` entry
    """
    values = {}
    for role in ROLES:
        override = osd.get('applied-{}-addr'.format(role))
        values[role] = override or networks.get(role, {}).get('host-address')
        values[role + '_override'] = override is not None
    return Expected(**values)


def allocation_problems(osd):
    """Check that an OSD's applied addresses are the desired ones.

    They may differ only in the documented fallback of the balanced
    profile: an OSD planned for a NUMA node that got CPUs on any node, or
    none and was left unpinned, keeps the host-wide addresses.

    :param osd: one ``osds`` entry of ``resource-allocation-status``
    :returns: problem descriptions, empty when consistent
    :rtype: list[str]
    """
    desired = dict((role, osd.get('desired-{}-addr'.format(role)))
                   for role in ROLES)
    applied = dict((role, osd.get('applied-{}-addr'.format(role)))
                   for role in ROLES)
    if desired == applied:
        return []
    if (osd.get('desired-mode') == 'numa' and
            osd.get('applied-mode') in ('any', None) and
            not any(applied.values())):
        LOG.info('osd.%s fell back from NUMA node %s to %s and keeps the '
                 'host-wide addresses', osd['osd-id'],
                 osd.get('desired-numa-node'),
                 'any node' if osd.get('applied-mode') else 'no CPU pinning')
        return []
    return ['osd.{}: applied addresses {} differ from the desired {} '
            '(desired mode {}, applied mode {})'.format(
                osd['osd-id'], applied, desired, osd.get('desired-mode'),
                osd.get('applied-mode'))]


def configured(entry):
    """Return an application config entry's value, or None when unset.

    Juju omits the value of an unset option without a default.
    """
    if 'value' not in entry or entry.get('source') in ('default', 'unset'):
        return None
    return entry['value']


def overridden(expected):
    return expected.public_override or expected.cluster_override


def expected_ceph_args(expected):
    """The CEPH_ARGS tokens the charm should pass, as a set."""
    args = set()
    for role in ROLES:
        if getattr(expected, role + '_override'):
            args.add('--{}_addr={}'.format(role, getattr(expected, role)))
    if args:
        args.add('--ms_bind_before_connect=true')
    return args


def binding_problems(osd, expected, evidence, advertised, mons):
    """Compare one OSD's observed binding with what the charm applied.

    :param osd: OSD ID
    :param expected: :class:`Expected` addresses
    :param evidence: the OSD's entry of the ``network`` probe
    :param advertised: the OSD's entry of the ``advertised`` probe
    :param mons: monitor ``address:port`` endpoints
    :returns: human readable problems; empty when the binding holds
    :rtype: list[str]
    """
    problems = []

    def problem(text, *args):
        problems.append('osd.{}: {}'.format(osd, text.format(*args)))

    wanted_args = expected_ceph_args(expected)
    for source, value in (
            ('drop-in', dropin_ceph_args(evidence['dropin'])),
            ('daemon environment', evidence['ceph_args'])):
        if set((value or '').split()) != wanted_args:
            problem('{} CEPH_ARGS is {!r}, expected {}', source, value,
                    sorted(wanted_args) or None)

    wanted = set()
    for role in ROLES:
        address = getattr(expected, role)
        if address is None:
            continue
        wanted.add(address)
        actual = evidence['config'].get(role + '_addr')
        if entity_ip(actual) != address:
            problem('runs with {}_addr {}, expected {}', role, actual, address)
        ips = set(endpoint_ip(a) for a in advertised.get(role, []))
        if ips != {address}:
            problem('cluster map advertises {} address(es) {}, expected {}',
                    role, sorted(ips, key=str), address)
    flag = evidence['config'].get('ms_bind_before_connect')
    if flag != ('true' if overridden(expected) else 'false'):
        problem('runs with ms_bind_before_connect {}', flag)
    if not advertised.get('up'):
        problem('is not up in the cluster map')

    listening = set(endpoint_ip(e) for e in evidence['listening'])
    if None in listening:
        problem('listens on a wildcard address')
    if not wanted <= listening:
        problem('does not listen on {}', sorted(wanted - listening))
    if None not in (expected.public, expected.cluster) and \
            listening - wanted - {None}:
        problem('also listens on {}', sorted(listening - wanted - {None}))

    if overridden(expected):
        # Accepted connections use a listening endpoint; the rest are opened
        # by the OSD and must leave from one of its own addresses.
        local = set(endpoint_key(e) for e in evidence['listening'])
        opened = [(source, peer) for source, peer in evidence['established']
                  if endpoint_key(source) not in local]
        sources = set(endpoint_ip(source) for source, _ in opened)
        if sources - wanted:
            problem('opens connections from {}, expected only {}',
                    sorted(sources - wanted, key=str), sorted(wanted))
        mon_keys = set(endpoint_key(m) for m in mons)
        to_mons = set(endpoint_ip(source) for source, peer in opened
                      if endpoint_key(peer) in mon_keys)
        if not to_mons:
            problem('has no monitor connection to check')
        elif expected.public and to_mons != {expected.public}:
            problem('connects to monitors from {}, expected {}',
                    sorted(to_mons, key=str), expected.public)
    return problems


def address_network(interfaces, address):
    """Return the subnet of the interface holding ``address``, or None.

    :param interfaces: the ``interfaces`` entry of the ``network`` probe
    """
    for interface in interfaces.values():
        for cidr in interface['addresses']:
            network = ipaddress.ip_interface(cidr)
            if str(network.ip) == address:
                return network.network
    return None


def restart_problems(before, after):
    """Check that exactly the OSDs whose binding changed were restarted.

    A network option applies to every OSD of the application, so every OSD
    whose expected addresses changed must restart, and no other may. CPU
    allocations stay unchanged.

    :param before: per OSD, its ``expected`` addresses, ``cores`` and
        daemon ``pid`` as verified before a change
    :param after: the same, verified after the change
    :returns: problem descriptions, empty when the restarts are right
    :rtype: list[str]
    """
    problems = []
    for osd in sorted(set(before) | set(after), key=int):
        if osd not in before or osd not in after:
            problems.append('osd.{} is only present {} the change'.format(
                osd, 'after' if osd in after else 'before'))
            continue
        old, new = before[osd], after[osd]
        if old['cores'] != new['cores']:
            problems.append('osd.{} cores changed from {} to {}'.format(
                osd, old['cores'], new['cores']))
        changed = old['expected'] != new['expected']
        restarted = old['pid'] != new['pid']
        if changed and not restarted:
            problems.append('osd.{} binding changed but it was not '
                            'restarted'.format(osd))
        elif restarted and not changed:
            problems.append('osd.{} was restarted without a binding '
                            'change'.format(osd))
    return problems


class NetworkBindingTest(unittest.TestCase):
    """One dependent scenario on a host with per-node addresses."""

    timeout = 600

    def setUp(self):
        import zaza.model
        self.zaza = zaza.model
        self.model = self.zaza.get_juju_model()
        self.bound_profile = os.environ.get(
            'TEST_NETWORK_PROFILE', 'balanced')
        self.conflict = None
        self.changed = set()
        self.artifacts = Path(os.environ.get(
            'TEST_EPA_ARTIFACTS', '/tmp/ceph-epa-functest')) / (
                'network-' + uuid.uuid4().hex)
        self.artifacts.mkdir(parents=True)
        LOG.info('Network binding evidence: %s', self.artifacts)
        status = self.zaza.get_status(model_name=self.model)
        self.units = sorted(status.applications['ceph-osd'].units)
        self.mon = sorted(status.applications['ceph-mon'].units)[0]
        self.original = self.zaza.get_application_config(
            'ceph-osd', model_name=self.model)
        self.profile = configured(
            self.original['performance-profile']) or 'unmanaged'
        self.addCleanup(self._restore)
        self.addCleanup(self._final_evidence)
        self._change({'performance-profile': self.bound_profile})

    def _save(self, name, value):
        (self.artifacts / (name + '.json')).write_text(
            json.dumps(value, indent=2, sort_keys=True))

    def _run(self, unit, command):
        result = self.zaza.run_on_unit(
            unit, command, model_name=self.model, timeout=120)
        self.assertEqual(str(result['Code']), '0', (unit, command, result))
        return result['Stdout']

    def _probe(self, unit, mode, *args):
        source = Path(__file__).with_name('resource_profile_probe.py')
        code = 'import base64; exec(base64.b64decode({!r}))'.format(
            base64.b64encode(source.read_bytes()).decode())
        return json.loads(self._run(unit, 'python3 -c {} {}'.format(
            shlex.quote(code), shlex.join([mode, *args]))))

    def _rows(self):
        status = self.zaza.get_status(model_name=self.model)
        rows = {unit: {'workload': u.workload_status.status,
                       'message': u.workload_status.info,
                       'agent': u.agent_status.status}
                for unit, u in status.applications['ceph-osd'].units.items()}
        self._save('last-status', rows)
        self.assertFalse([u for u, r in rows.items() if 'error' in (
            r['workload'], r['agent'])], rows)
        return rows

    def _active(self):
        """All units idle and active with the current profile applied."""
        rows = self._rows()
        return all(r['workload'] == 'active' and r['agent'] == 'idle' and (
            self.profile == 'unmanaged' or
            'profile {} applied'.format(self.profile) in r['message'])
            for r in rows.values())

    def _wait(self, description, predicate):
        LOG.info('Waiting for %s', description)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(5)
        self.fail('Timed out waiting for {}; evidence: {}'.format(
            description, self.artifacts))

    def _markers(self):
        return generations(self._probe(self.mon, 'markers'))

    def _reset_counters(self):
        # Each step restarts OSDs; stay clear of systemd's start limit.
        for unit in self.units:
            self._run(unit, 'for d in /var/lib/ceph/osd/ceph-*; do '
                      'systemctl reset-failed "ceph-osd@${d##*-}.service" '
                      '|| exit; done')

    def _change(self, values):
        """Apply config (None resets an option) and wait for its rollout."""
        current = self.zaza.get_application_config(
            'ceph-osd', model_name=self.model)
        values = {k: v for k, v in values.items()
                  if configured(current[k]) != v}
        self.changed.update(values)
        self.profile = values.get('performance-profile', self.profile)
        if not values:
            self._wait('active units', self._active)
            return
        before = self._markers()
        self._reset_counters()
        reset = sorted(k for k, v in values.items() if v is None)
        if reset:
            self.zaza.reset_application_config(
                'ceph-osd', reset, model_name=self.model)
        update = {k: v for k, v in values.items() if v is not None}
        if update:
            self.zaza.set_application_config(
                'ceph-osd', update, model_name=self.model)
        if self.profile == 'unmanaged':
            # Unmanaged starts no rollout generation; observe local state.
            self._wait('unmanaged profile', lambda: self._active() and all(
                status['requested-profile'] == 'unmanaged'
                for status in self._allocations(strict=False).values()))
            return

        def completed():
            markers = self._markers()
            new = set(markers) - set(before)
            return bool(new) and all(
                set(markers[g]) == set(self.units) and
                all('done' in m for m in markers[g].values())
                for g in new) and self._active()
        self._wait('rollout after {}'.format(sorted(values)), completed)

    def _allocations(self, strict=True):
        result = {}
        for unit in self.units:
            action = self.zaza.sync_wrapper(_status_action)(self.model, unit)
            status = json.loads(action.data['results']['message'])
            if strict:
                self.assertFalse(status['errors'], status)
                self.assertFalse(status['drift'], status)
                self.assertEqual(status['rollout']['status'], 'complete')
            result[unit] = status
        return result

    def _verify(self, name):
        """Check every OSD's binding end to end, retrying briefly.

        :returns: per OSD, the owning unit, the :class:`Expected`
            addresses, the applied cores and the daemon PID
        """
        allocations = self._allocations()
        self._save(name + '-allocations', allocations)
        expected = {}
        problems = []
        for unit, status in allocations.items():
            for osd in status['osds']:
                problems.extend(allocation_problems(osd))
                # The daemon must run with what was applied.
                expected[osd['osd-id']] = (unit, expected_binding(
                    osd, status['networks']), osd['applied-cores'])
        self.assertEqual(problems, [], 'evidence: {}'.format(self.artifacts))
        evidence = {}

        def holds():
            evidence.update({u: self._probe(u, 'network')
                             for u in self.units})
            evidence['advertised'] = self._probe(self.mon, 'advertised')
            del problems[:]
            for osd, (unit, wanted, _) in sorted(expected.items()):
                problems.extend(binding_problems(
                    osd, wanted, evidence[unit]['osds'][osd],
                    evidence['advertised']['osds'][osd],
                    evidence['advertised']['mons']))
            problems.extend(self._unreachable(expected, evidence))
            self._save(name + '-network', evidence)
            if problems:
                LOG.info('Binding not (yet) as expected: %s', problems)
            return not problems
        deadline = time.monotonic() + 120
        while not holds() and time.monotonic() < deadline:
            time.sleep(10)
        self.assertEqual(problems, [], 'evidence: {}'.format(self.artifacts))
        return {osd: dict(unit=unit, expected=wanted, cores=cores,
                          pid=evidence[unit]['osds'][osd]['pid'])
                for osd, (unit, wanted, cores) in expected.items()}

    def _unreachable(self, expected, evidence):
        """Connect to every advertised endpoint from another host.

        Public endpoints are tried from the MON unit; cluster endpoints
        from another OSD unit, or the MON unit when there is none.
        """
        problems = []
        for osd, (unit, _, _) in sorted(expected.items()):
            advertised = evidence['advertised']['osds'][osd]
            peers = [u for u in self.units if u != unit]
            for role, source in (('public', self.mon),
                                 ('cluster', peers[0] if peers else self.mon)):
                for endpoint, error in self._probe(
                        source, 'connect', *advertised[role]).items():
                    if error:
                        problems.append(
                            'osd.{}: {} endpoint {} unreachable from {}: '
                            '{}'.format(osd, role, endpoint, source, error))
        return problems

    def test_numa_local_addresses(self):
        """Bind, change one address, fall back to and return from host-wide
        addresses, and detect an operator CEPH_ARGS conflict."""
        baseline = self._verify('baseline')
        bound = {osd: b for osd, b in baseline.items()
                 if overridden(b['expected'])}
        self.assertTrue(bound, 'No OSD uses a NUMA-local address. This test '
                        'needs a host with an address of its own on the '
                        'NUMA node of an OSD; see the README.')
        # Prefer an OSD whose cluster address is local, so that its public
        # address stays local while the cluster address changes.
        osd = min(bound, key=lambda o: (
            not bound[o]['expected'].cluster_override, int(o)))
        self._check_address_change(osd, baseline)

        # Host-wide addressing, and back.
        self._change({'performance-profile': 'minimal'})
        hostwide = self._verify('minimal')
        self.assertEqual([o for o, b in hostwide.items()
                          if overridden(b['expected'])], [])
        self._change({'performance-profile': self.bound_profile})
        rebound = self._verify('rebound')
        self.assertEqual({o: b['expected'] for o, b in rebound.items()},
                         {o: b['expected'] for o, b in baseline.items()})

        self._check_conflict(osd, rebound[osd])

    def _check_address_change(self, osd, baseline):
        """Narrow one network to the host address's subnet and back.

        The OSD loses only that address. The option applies to every OSD,
        so each OSD whose binding changes restarts, and no other.
        """
        binding = baseline[osd]
        role = 'cluster' if binding['expected'].cluster_override \
            else 'public'
        other = 'public' if role == 'cluster' else 'cluster'
        option = 'ceph-{}-network'.format(role)
        host_address = self._allocations()[binding['unit']]['networks'][
            role]['host-address']
        subnet = address_network(self._probe(
            binding['unit'], 'network')['interfaces'], host_address)
        local = getattr(binding['expected'], role)
        if subnet is None or ipaddress.ip_address(local) in subnet:
            LOG.warning('Skipping the address change: %s shares a subnet '
                        'with the host %s address %s', local, role,
                        host_address)
            return
        self._change({option: str(subnet)})
        narrowed = self._verify('address-only')
        self.assertEqual(narrowed[osd]['expected']._replace(**{
            role: host_address, role + '_override': False}),
            narrowed[osd]['expected'])
        self.assertEqual(getattr(narrowed[osd]['expected'], other),
                         getattr(binding['expected'], other))
        self._assert_restarted(baseline, narrowed)

        self._change({option: self._original(option)})
        restored = self._verify('address-restored')
        self.assertEqual(restored[osd]['expected'], binding['expected'])
        self._assert_restarted(narrowed, restored)

    def _original(self, option):
        return configured(self.original[option])

    def _assert_restarted(self, before, after):
        """Only OSDs whose binding changed restart; cores stay unchanged."""
        self.assertEqual(restart_problems(before, after), [],
                         'evidence: {}'.format(self.artifacts))

    def _restart_osd(self, unit, osd):
        self._reset_counters()
        self._run(unit, 'systemctl restart ceph-osd@{}.service'.format(osd))
        self._wait('osd.{} active'.format(osd), lambda: '"active"' in (
            self.zaza.run_on_unit(
                unit, 'ceph daemon osd.{} status'.format(osd),
                model_name=self.model).get('Stdout') or ''))

    def _check_conflict(self, osd, binding):
        """An operator CEPH_ARGS in /etc/default/ceph replaces the drop-in's.

        systemd gives ``EnvironmentFile=`` precedence over ``Environment=``,
        so verification on update-status must block the unit.
        """
        unit = binding['unit']
        host_public = self._allocations()[unit]['networks']['public'][
            'host-address']
        self.conflict = (unit, osd)
        self._run(unit, 'printf "%s\\nCEPH_ARGS=--public_addr=%s\\n" {} {} '
                  '>> /etc/default/ceph'.format(
                      shlex.quote(CONFLICT_MARKER), shlex.quote(host_public)))
        self._restart_osd(unit, osd)
        self._run(unit, 'hooks/update-status')
        self._wait('conflict detected', lambda: (
            lambda row: row['workload'] == 'blocked' and
            'osd.{} runs with'.format(osd) in row['message'])(
                self._rows()[unit]))
        self._remove_conflict()
        self._wait('conflict resolved', self._active)
        self._verify('conflict-resolved')

    def _remove_conflict(self):
        if not self.conflict:
            return
        unit, osd = self.conflict
        self._run(unit, "sed -i '/^{}$/,+1d' /etc/default/ceph".format(
            CONFLICT_MARKER))
        self._restart_osd(unit, osd)
        self._run(unit, 'hooks/update-status')
        self.conflict = None

    def _final_evidence(self):
        # unittest cleanups run LIFO: capture failures BEFORE repairing them.
        try:
            self._save('before-cleanup', {
                u: self._probe(u, 'network') for u in self.units})
        except Exception:
            LOG.exception('Could not capture final evidence at %s',
                          self.artifacts)

    def _restore(self):
        """Undo the injected conflict and every changed option."""
        self._remove_conflict()
        self._change({k: self._original(k) for k in self.changed})
