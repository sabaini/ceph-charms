#!/usr/bin/env python3
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

"""Read-only guest probes for the opt-in resource-profile functional test.

Uses only the guest's standard library and system commands, not charm modules.
"""

import glob
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


def command(*args):
    """Run a bounded command, failing rather than hiding missing evidence."""
    return subprocess.check_output(args, text=True, timeout=30)


def allocations():
    """Read EPA's protected claims directly from its Unix socket."""
    path = '/var/snap/epa-orchestrator/current/data/epa.sock'
    if not Path(path).exists():
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(15)
        sock.connect(path)
        sock.sendall(json.dumps({
            'version': '1.0', 'service_name': 'ceph-profile-functest',
            'action': 'list_allocations',
            'pool': 'general',
        }).encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    result = json.loads(b''.join(chunks))
    if result.get('pool') != 'general':
        raise RuntimeError('EPA did not confirm the general pool: {}'.format(
            result))
    return sorted(result['allocations'], key=lambda a: a['service_name'])


def snapshot():
    """Capture process identity, claims, drop-ins and thread affinity."""
    result = {'hostname': socket.gethostname(), 'time': time.time(),
              'boot-id': Path('/proc/sys/kernel/random/boot_id').read_text(),
              'claims': allocations(), 'osds': {}}
    for directory in sorted(glob.glob('/var/lib/ceph/osd/ceph-*')):
        osd = directory.rsplit('-', 1)[1]
        service = 'ceph-osd@{}.service'.format(osd)
        properties = dict(line.split('=', 1) for line in command(
            'systemctl', 'show', service,
            '--property=MainPID,ActiveState,LoadState,NUMAPolicy,NUMAMask,'
            'ExecMainStartTimestampMonotonic').splitlines())
        dropin = Path('/etc/systemd/system/{}.d/'
                      '50-charm-resource-profile.conf'.format(service))
        pid = int(properties['MainPID'])
        affinities = set()
        for task in glob.glob('/proc/{}/task/*'.format(pid)) if pid else []:
            try:
                affinities.add(tuple(sorted(os.sched_getaffinity(
                    int(task.rsplit('/', 1)[1])))))
            except ProcessLookupError:
                pass  # A thread may exit while its siblings are inspected.
        result['osds'][osd] = {
            'systemd': properties,
            'dropin': dropin.read_text() if dropin.exists() else None,
            'affinities': sorted(affinities),
        }
    return result


def tcp_sockets():
    """Map process IDs to their listening and established TCP endpoints."""
    result = {}
    for line in command('ss', '-Htanp').splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        state, local, peer = fields[0], fields[3], fields[4]
        for pid in re.findall(r'pid=(\d+),', line):
            entry = result.setdefault(
                pid, {'listening': [], 'established': []})
            if state == 'LISTEN':
                entry['listening'].append(local)
            elif state == 'ESTAB':
                entry['established'].append([local, peer])
    return result


def daemon_config(osd, option):
    """Read one effective option through the OSD's admin socket."""
    try:
        return json.loads(command('ceph', 'daemon', 'osd.' + osd, 'config',
                                  'get', option))[option]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return None


def network():
    """Capture each OSD's configured, effective and observed addresses."""
    sockets = tcp_sockets()
    result = {'hostname': socket.gethostname(), 'interfaces': {
        link['ifname']: {'flags': link.get('flags', []), 'addresses': [
            '{}/{}'.format(a['local'], a['prefixlen'])
            for a in link.get('addr_info', [])]}
        for link in json.loads(command('ip', '-json', 'addr', 'show'))},
        'osds': {}}
    for directory in sorted(glob.glob('/var/lib/ceph/osd/ceph-*')):
        osd = directory.rsplit('-', 1)[1]
        service = 'ceph-osd@{}.service'.format(osd)
        pid = command('systemctl', 'show', service, '--property=MainPID',
                      '--value').strip()
        dropin = Path('/etc/systemd/system/{}.d/'
                      '50-charm-resource-profile.conf'.format(service))
        environment = Path('/proc/{}/environ'.format(pid))
        ceph_args = None
        if pid != '0' and environment.exists():
            for item in environment.read_bytes().decode().split('\0'):
                if item.startswith('CEPH_ARGS='):
                    ceph_args = item.split('=', 1)[1]
        result['osds'][osd] = dict(
            sockets.get(pid, {'listening': [], 'established': []}),
            pid=pid, dropin=dropin.read_text() if dropin.exists() else None,
            ceph_args=ceph_args, config={
                option: daemon_config(osd, option) for option in (
                    'public_addr', 'cluster_addr',
                    'ms_bind_before_connect')})
    return result


def advertised():
    """Read the addresses the cluster map advertises, from a MON unit."""
    def addresses(vector):
        return [entry['addr'] for entry in vector.get('addrvec', [])]
    osds = json.loads(command('ceph', 'osd', 'dump', '--format=json'))
    mons = json.loads(command('ceph', 'mon', 'dump', '--format=json'))
    return {
        'osds': {str(osd['osd']): {
            'up': osd['up'], 'public': addresses(osd['public_addrs']),
            'cluster': addresses(osd['cluster_addrs'])}
            for osd in osds['osds']},
        'mons': [address for mon in mons['mons']
                 for address in addresses(mon['public_addrs'])],
    }


def connect(*endpoints):
    """Open a TCP connection to each ``address:port``; map to any error."""
    result = {}
    for endpoint in endpoints:
        host, _, port = endpoint.rpartition(':')
        try:
            with socket.create_connection((host.strip('[]'), int(port)),
                                          timeout=5):
                result[endpoint] = None
        except OSError as exc:
            result[endpoint] = str(exc)
    return result


def markers():
    """Read the rollout handoff keys from a MON unit."""
    # One bounded command, independent of how many old generations exist.
    keys = json.loads(command('ceph', 'config-key', 'dump'))
    return {key: value for key, value in keys.items()
            if key.startswith('osd-resource_')}


def journal(since):
    """Keep systemd lifecycle messages (not the daemon's verbose log)."""
    text = command('journalctl', '-u', 'ceph-osd@*', '--since', '@' + since,
                   '--no-pager', '-o', 'json')
    return [entry for entry in map(json.loads, text.splitlines())
            if entry.get('SYSLOG_IDENTIFIER') == 'systemd']


if __name__ == '__main__':
    if sys.argv[1] == 'snapshot':
        output = snapshot()
    elif sys.argv[1] == 'markers':
        output = markers()
    elif sys.argv[1] == 'journal':
        output = journal(sys.argv[2])
    elif sys.argv[1] == 'network':
        output = network()
    elif sys.argv[1] == 'advertised':
        output = advertised()
    elif sys.argv[1] == 'connect':
        output = connect(*sys.argv[2:])
    else:
        raise ValueError('Unknown probe: ' + sys.argv[1])
    print(json.dumps(output, sort_keys=True))
