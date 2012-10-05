#!/usr/bin/python

#
# Copyright 2012 Canonical Ltd.
#
# Authors:
#  Paul Collins <paul.collins@canonical.com>
#  James Page <james.page@ubuntu.com>
#

import glob
import os
import subprocess
import shutil
import socket
import sys

import ceph
import utils


def install_upstart_scripts():
    for x in glob.glob('files/upstart/*.conf'):
        shutil.copy(x, '/etc/init/')


def install():
    utils.juju_log('INFO', 'Begin install hook.')
    utils.configure_source()
    utils.install('ceph', 'gdisk')
    install_upstart_scripts()
    utils.juju_log('INFO', 'End install hook.')


def emit_cephconf():
    cephcontext = {
        'mon_hosts': ' '.join(get_mon_hosts()),
        'fsid': utils.config_get('fsid'),
        }

    with open('/etc/ceph/ceph.conf', 'w') as cephconf:
        cephconf.write(utils.render_template('ceph.conf', cephcontext))


def config_changed():
    utils.juju_log('INFO', 'Begin config-changed hook.')

    utils.juju_log('INFO', 'Monitor hosts are ' + repr(get_mon_hosts()))

    fsid = utils.config_get('fsid')
    if fsid == '':
        utils.juju_log('CRITICAL', 'No fsid supplied, cannot proceed.')
        sys.exit(1)

    monitor_secret = utils.config_get('monitor-secret')
    if monitor_secret == '':
        utils.juju_log('CRITICAL',
                       'No monitor-secret supplied, cannot proceed.')
        sys.exit(1)

    emit_cephconf()

    if ceph.is_quorum():
        for dev in utils.config_get('osd-devices').split(' '):
            osdize(dev)
        subprocess.call(['udevadm', 'trigger',
                         '--subsystem-match=block', '--action=add'])

    utils.juju_log('INFO', 'End config-changed hook.')


def get_mon_hosts():
    hosts = []
    hosts.append('{}:6789'.format(utils.get_host_ip()))

    for relid in utils.relation_ids('mon'):
        for unit in utils.relation_list(relid):
            hosts.append(
                '{}:6789'.format(utils.get_host_ip(
                                    utils.relation_get('private-address',
                                                       unit, relid)))
                )

    hosts.sort()
    return hosts


def bootstrap_monitor_cluster():
    hostname = utils.get_unit_hostname()
    done = '/var/lib/ceph/mon/ceph-{}/done'.format(hostname)
    secret = utils.config_get('monitor-secret')
    keyring = '/var/lib/ceph/tmp/{}.mon.keyring'.format(hostname)

    if os.path.exists(done):
        utils.juju_log('INFO',
                       'bootstrap_monitor_cluster: mon already initialized.')
    else:
        try:
            subprocess.check_call(['ceph-authtool', keyring,
                                   '--create-keyring', '--name=mon.',
                                   '--add-key={}'.format(secret),
                                   '--cap', 'mon', 'allow *'])

            subprocess.check_call(['ceph-mon', '--mkfs',
                                   '-i', hostname,
                                   '--keyring', keyring])

            with open(done, 'w'):
                pass

            subprocess.check_call(['start', 'ceph-mon-all-starter'])
        except:
            raise
        finally:
            os.unlink(keyring)


def osdize(dev):
    # XXX hack for instances
    subprocess.call(['umount', '/mnt'])

    if ceph.is_osd_disk(dev):
        utils.juju_log('INFO',
                       'Looks like {} is already an OSD, skipping.'.format(dev))
        return

    if subprocess.call(['grep', '-wqs', dev + '1', '/proc/mounts']) == 0:
        utils.juju_log('INFO',
                       'Looks like {} is in use, skipping.'.format(dev))
        return

    if os.path.exists(dev):
        subprocess.call(['ceph-disk-prepare', dev])


def mon_relation():
    utils.juju_log('INFO', 'Begin mon-relation hook.')
    emit_cephconf()

    moncount = int(utils.config_get('monitor-count'))
    if len(get_mon_hosts()) >= moncount:
        bootstrap_monitor_cluster()

        ceph.wait_for_quorum()
        for dev in utils.config_get('osd-devices').split(' '):
            osdize(dev)
        subprocess.call(['udevadm', 'trigger',
                         '--subsystem-match=block', '--action=add'])
    else:
        utils.juju_log('INFO',
                       'Not enough mons ({}), punting.'.format(
                            len(get_mon_hosts())))

    utils.juju_log('INFO', 'End mon-relation hook.')


def upgrade_charm():
    utils.juju_log('INFO', 'Begin upgrade-charm hook.')
    emit_cephconf()
    install_upstart_scripts()
    utils.juju_log('INFO', 'End upgrade-charm hook.')


def start():
    # In case we're being redeployed to the same machines, try
    # to make sure everything is running as soon as possible.
    subprocess.call(['start', 'ceph-mon-all-starter'])
    subprocess.call(['udevadm', 'trigger',
                     '--subsystem-match=block', '--action=add'])


hooks = {
    'config-changed': config_changed,
    'install': install,
    'mon-relation-departed': mon_relation,
    'mon-relation-joined': mon_relation,
    'start': start,
    'upgrade-charm': upgrade_charm,
}

hook = os.path.basename(sys.argv[0])

try:
    hooks[hook]()
except KeyError:
    utils.juju_log('INFO',
                   "This charm doesn't know how to handle '{}'.".format(hook))

sys.exit(0)
