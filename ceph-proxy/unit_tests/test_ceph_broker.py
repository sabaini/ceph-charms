from unittest import mock
import unittest
import sys

# python-apt is not installed as part of test-requirements but is imported by
# some charmhelpers modules so create a fake import.
mock_apt = mock.MagicMock()
sys.modules['apt'] = mock_apt
mock_apt.apt_pkg = mock.MagicMock()

mock_apt_pkg = mock.MagicMock()
sys.modules['apt_pkg'] = mock_apt_pkg
mock_apt_pkg.upstream_version = mock.MagicMock()
mock_apt_pkg.upstream_version.return_value = '10.1.2-0ubuntu1'

import charms_ceph.broker as broker


class CephBrokerTest(unittest.TestCase):

    @mock.patch('charms_ceph.broker.config')
    def test_get_broker_service_strips_client_prefix(self, mock_config):
        mock_config.return_value = 'client.anbox'
        self.assertEqual('anbox', broker.get_broker_service())

    @mock.patch('charms_ceph.broker.config')
    def test_get_broker_service_defaults_to_admin(self, mock_config):
        mock_config.return_value = ''
        self.assertEqual('admin', broker.get_broker_service())

    @mock.patch('charms_ceph.broker.config')
    def test_get_broker_service_keeps_dotted_id(self, mock_config):
        mock_config.return_value = 'radosgw.gateway'
        self.assertEqual('radosgw.gateway', broker.get_broker_service())

    @mock.patch('charms_ceph.broker.handle_replicated_pool')
    @mock.patch('charms_ceph.broker.config')
    def test_process_requests_v1_uses_configured_admin_service(
            self,
            mock_config,
            mock_handle_replicated_pool):
        mock_config.return_value = 'client.anbox'
        req = {'op': 'create-pool', 'name': 'anbox-cloud-lxd', 'replicas': 3}

        broker.process_requests_v1([req])

        mock_handle_replicated_pool.assert_called_once_with(
            request=req, service='anbox')

    @mock.patch('charms_ceph.broker.monitor_key_get')
    @mock.patch('charms_ceph.broker.config')
    def test_get_group_uses_configured_admin_service(
            self,
            mock_config,
            mock_monitor_key_get):
        mock_config.return_value = 'client.anbox'
        mock_monitor_key_get.return_value = '{}'

        broker.get_group('test-group')

        mock_monitor_key_get.assert_called_once_with(
            service='anbox',
            key='cephx.groups.test-group',
        )

    @mock.patch('charms_ceph.broker.config')
    @mock.patch('charms_ceph.broker.check_call')
    def test_update_service_permissions_uses_configured_admin_service(
            self, mock_check, mock_config):
        service_obj = {
            'group_names': {'rwx': ['images']},
            'groups': {'images': {'pools': ['anbox-cloud-lxd']}},
        }
        mock_config.return_value = 'client.anbox'

        broker.update_service_permissions(
            service='lxd',
            service_obj=service_obj,
        )

        mock_check.assert_called_once_with([
            'ceph',
            '--id', 'anbox',
            'auth', 'caps',
            'client.lxd',
            'mon',
            'allow r, allow command "osd blacklist", '
            'allow command "osd blocklist"',
            'osd',
            'allow rwx pool=anbox-cloud-lxd',
        ])

    @mock.patch('charms_ceph.broker.update_service_permissions')
    @mock.patch('charms_ceph.broker._build_service_groups')
    @mock.patch('charms_ceph.broker.save_service')
    @mock.patch('charms_ceph.broker.save_group')
    @mock.patch('charms_ceph.broker.get_service_groups')
    @mock.patch('charms_ceph.broker.get_group')
    def test_handle_add_permissions_to_key_keeps_legacy_response(
            self,
            mock_get_group,
            mock_get_service_groups,
            mock_save_group,
            mock_save_service,
            mock_build_service_groups,
            mock_update_service_permissions):
        mock_get_group.return_value = {'pools': [], 'services': []}
        mock_get_service_groups.return_value = {
            'group_names': {},
            'groups': {},
        }
        mock_build_service_groups.return_value = {'images': {'pools': []}}

        resp = broker.handle_add_permissions_to_key(
            request={'name': 'lxd', 'group': 'images'},
            service='anbox',
        )

        self.assertEqual({'exit-code': 0}, resp)
        mock_update_service_permissions.assert_called_once_with(
            'lxd',
            {
                'group_names': {'rwx': ['images']},
                'groups': {'images': {'pools': []}},
            },
            None,
        )

        # Silence unused patch warnings by asserting expected interaction.
        mock_save_group.assert_called_once()
        mock_save_service.assert_called_once()
