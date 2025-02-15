"""Ceph client library
"""

import json
import logging

from ops.framework import Object
from ops.framework import StoredState

from charmhelpers.contrib.storage.linux.ceph import (
    send_osd_settings,
)
import charms_ceph.utils as ceph


from utils import (
    get_public_addr,
    get_rbd_features,
)


logger = logging.getLogger(__name__)


class CephClientProvides(Object):
    """
    Encapsulate the Provides side of the Ceph Client relation.

    Hook events observed:
    - relation-joined
    - relation-changed
    """

    charm = None
    _stored = StoredState()

    def __init__(self, charm, relation_name='client'):
        super().__init__(charm, relation_name)

        self._stored.set_default(processed=[], processed_map={})
        self.charm = charm
        self.this_unit = self.model.unit
        self.relation_name = relation_name
        self.framework.observe(
            charm.on[self.relation_name].relation_joined,
            self._on_relation_changed
        )
        self.framework.observe(
            charm.on[self.relation_name].relation_changed,
            self._on_relation_changed
        )

        self._stored.processed_map = {}

    def notify_all(self):
        send_osd_settings()
        if not self.charm.ready_for_service():
            return
        for relation in self.model.relations[self.relation_name]:
            for unit in relation.units:
                self._handle_client_relation(relation, unit)

    def _on_relation_changed(self, event):
        """Prepare relation for data from requiring side."""
        send_osd_settings()
        if not self.charm.ready_for_service():
            return
        self._handle_client_relation(event.relation, event.unit)

    def _get_ceph_info_from_configs(self):
        """
        Create dictionary of ceph information required to set client relation.

        :returns: Dictionary of ceph configurations needed for client relation
        :rtype: dict
        """
        public_addr = get_public_addr()
        rbd_features = get_rbd_features()
        data = {
            'auth': 'cephx',
            'ceph-public-address': public_addr
        }
        if rbd_features:
            data['rbd-features'] = rbd_features
        return data

    def _get_custom_relation_init_data(self):
        """Information required for specialised relation.

        :returns: Ceph configurations needed for specialised relation
        :rtype: dict
        """
        return {}

    def _get_client_application_name(self, relation, unit):
        """Retrieve client application name from relation data."""
        return relation.data[unit].get(
            'application-name',
            relation.app.name)

    def _handle_client_relation(self, relation, unit):
        """Handle broker request and set the relation data

        :param relation: Operator relation
        :type relation: Relation
        :param unit: Unit to handle
        :type unit: Unit
        """

        # if is_unsupported_cmr(unit):
        #     return

        logger.debug(
            'mon cluster in quorum and osds bootstrapped '
            '- providing client with keys, processing broker requests')

        service_name = self._get_client_application_name(relation, unit)
        data = self._get_ceph_info_from_configs()
        data.update(self._get_custom_relation_init_data())
        data.update({'key': ceph.get_named_key(service_name)})

        data.update(
            self._handle_broker_request(
                relation, unit, add_legacy_response=True))
        for k, v in data.items():
            relation.data[self.this_unit][k] = str(v)

    def _handle_broker_request(
            self, relation, unit, add_legacy_response=False, force=False):
        """
        Retrieve broker request from relation, process, return response data.

        :param event: Operator event for the relation
        :type relid: Event
        :param add_legacy_response: (Optional) Adds the legacy ``broker_rsp``
                                    key to the response in addition to the
                                    new way.
        :type add_legacy_response: bool
        :returns: Dictionary of response data ready for use with relation_set.
        :param force: Whether to re-process broker requests.
        :type force: bool
        :rtype: dict
        """
        def _get_broker_req_id(request):
            try:
                if isinstance(request, str):
                    try:
                        req_key = json.loads(request)['request-id']
                    except (TypeError, json.decoder.JSONDecodeError):
                        logger.warning(
                            'Not able to decode request '
                            'id for broker request {}'.
                            format(request))
                        req_key = None
                else:
                    req_key = request['request-id']
            except KeyError:
                logger.warning(
                    'Not able to decode request id for broker request {}'.
                    format(request))
                req_key = None

            return req_key

        response = {}

        settings = relation.data[unit]
        if 'broker_req' in settings:
            broker_req_id = _get_broker_req_id(settings['broker_req'])
            if broker_req_id is None:
                return {}

            if not ceph.is_leader():
                logger.debug(
                    "Not leader - ignoring broker request {}".format(
                        broker_req_id))
                return {}

            unit_id = settings.get(
                'unit-name', unit.name).replace('/', '-')
            unit_response_key = 'broker-rsp-' + unit_id
            prev_result = self._stored.processed_map.get(broker_req_id)
            if prev_result is not None and not force:
                # The broker request has been processed already and we have
                # stored the result. Log it so that the users may know and
                # return the cached value, with the unit key.
                logger.debug(
                    "Ignoring already executed broker request {}".format(
                        broker_req_id))
                rsp = {unit_response_key: prev_result}
                if add_legacy_response:
                    rsp.update({'broker_rsp': prev_result})
                return rsp

            rsp = self.charm.process_broker_request(
                broker_req_id, settings['broker_req'])
            response.update({unit_response_key: rsp})
            if add_legacy_response:
                response.update({'broker_rsp': rsp})
            self._stored.processed_map[broker_req_id] = rsp
        else:
            logger.warn('broker_req not in settings: {}'.format(settings))
        return response
