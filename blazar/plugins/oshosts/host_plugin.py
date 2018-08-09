# -*- coding: utf-8 -*-
#
# Author: François Rossigneux <francois.rossigneux@inria.fr>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import datetime

from novaclient import exceptions as nova_exceptions
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import strutils

from blazar.db import api as db_api
from blazar.db import exceptions as db_ex
from blazar.db import utils as db_utils
from blazar.manager import exceptions as manager_ex
from blazar.plugins import base
from blazar.plugins import oshosts as plugin
from blazar import status
from blazar.utils.openstack import nova
from blazar.utils import plugins as plugins_utils
from blazar.utils import trusts

plugin_opts = [
    cfg.StrOpt('blazar_az_prefix',
               default='blazar_',
               help='Prefix for Availability Zones created by Blazar'),
    cfg.StrOpt('before_end',
               default='',
               help='Actions which we will be taken before the end of '
                    'the lease'),
    cfg.BoolOpt('enable_notification_monitor',
                default=False,
                help='Enable notification-based resource monitoring. '
                     'If it is enabled, the blazar-manager monitors states of '
                     'compute hosts by subscribing to notifications of Nova.'),
    cfg.ListOpt('notification_topics',
                default=['notifications', 'versioned_notifications'],
                help='Notification topics to subscribe to.'),
    cfg.BoolOpt('enable_polling_monitor',
                default=False,
                help='Enable polling-based resource monitoring. '
                     'If it is enabled, the blazar-manager monitors states '
                     'of compute hosts by polling the Nova API.'),
    cfg.IntOpt('polling_interval',
               default=60,
               help='Interval (seconds) of polling for health checking.'),
    cfg.IntOpt('healing_interval',
               default=60,
               help='Interval (minutes) of reservation healing. '
                    'If 0 is specified, the interval is infinite and all the '
                    'reservations in the future is healed at one time.'),
]

CONF = cfg.CONF
CONF.register_opts(plugin_opts, group=plugin.RESOURCE_TYPE)
LOG = logging.getLogger(__name__)

before_end_options = ['', 'snapshot', 'default']


class PhysicalHostPlugin(base.BasePlugin, nova.NovaClientWrapper):
    """Plugin for physical host resource."""
    resource_type = plugin.RESOURCE_TYPE
    title = 'Physical Host Plugin'
    description = 'This plugin starts and shutdowns the hosts.'
    freepool_name = CONF.nova.aggregate_freepool_name
    pool = None

    def __init__(self):
        super(PhysicalHostPlugin, self).__init__(
            username=CONF.os_admin_username,
            password=CONF.os_admin_password,
            user_domain_name=CONF.os_admin_user_domain_name,
            project_name=CONF.os_admin_project_name,
            project_domain_name=CONF.os_admin_project_domain_name)
        self.monitor = PhysicalHostMonitorPlugin()
        self.monitor.register_healing_handler(self.heal_reservations)

    def reserve_resource(self, reservation_id, values):
        """Create reservation."""
        self._check_params(values)

        host_ids = self._matching_hosts(
            values['hypervisor_properties'],
            values['resource_properties'],
            values['count_range'],
            values['start_date'],
            values['end_date'],
        )
        if not host_ids:
            raise manager_ex.NotEnoughHostsAvailable()
        pool = nova.ReservationPool()
        pool_name = reservation_id
        az_name = "%s%s" % (CONF[self.resource_type].blazar_az_prefix,
                            pool_name)
        pool_instance = pool.create(name=pool_name, az=az_name)
        host_rsrv_values = {
            'reservation_id': reservation_id,
            'aggregate_id': pool_instance.id,
            'resource_properties': values['resource_properties'],
            'hypervisor_properties': values['hypervisor_properties'],
            'count_range': values['count_range'],
            'status': 'pending',
            'before_end': values['before_end']
        }
        host_reservation = db_api.host_reservation_create(host_rsrv_values)
        for host_id in host_ids:
            db_api.host_allocation_create({'compute_host_id': host_id,
                                          'reservation_id': reservation_id})
        return host_reservation['id']

    def update_reservation(self, reservation_id, values):
        """Update reservation."""
        reservation = db_api.reservation_get(reservation_id)
        lease = db_api.lease_get(reservation['lease_id'])

        if (not [x for x in values.keys() if x in ['min', 'max',
                                                   'hypervisor_properties',
                                                   'resource_properties']]
                and values['start_date'] >= lease['start_date']
                and values['end_date'] <= lease['end_date']):
            # Nothing to update
            return

        dates_before = {'start_date': lease['start_date'],
                        'end_date': lease['end_date']}
        dates_after = {'start_date': values['start_date'],
                       'end_date': values['end_date']}
        host_reservation = db_api.host_reservation_get(
            reservation['resource_id'])
        self._update_allocations(dates_before, dates_after, reservation_id,
                                 reservation['status'], host_reservation,
                                 values)

        updates = {}
        if 'min' in values or 'max' in values:
            count_range = str(values.get(
                'min', host_reservation['count_range'].split('-')[0])
            ) + '-' + str(values.get(
                'max', host_reservation['count_range'].split('-')[1])
            )
            updates['count_range'] = count_range
        if 'hypervisor_properties' in values:
            updates['hypervisor_properties'] = values.get(
                'hypervisor_properties')
        if 'resource_properties' in values:
            updates['resource_properties'] = values.get(
                'resource_properties')
        if updates:
            db_api.host_reservation_update(host_reservation['id'], updates)

    def on_start(self, resource_id):
        """Add the hosts in the pool."""
        host_reservation = db_api.host_reservation_get(resource_id)
        pool = nova.ReservationPool()
        for allocation in db_api.host_allocation_get_all_by_values(
                reservation_id=host_reservation['reservation_id']):
            host = db_api.host_get(allocation['compute_host_id'])
            pool.add_computehost(host_reservation['aggregate_id'],
                                 host['service_name'])

    def before_end(self, resource_id):
        """Take an action before the end of a lease."""
        host_reservation = db_api.host_reservation_get(resource_id)
        action = host_reservation['before_end']
        if action == 'default':
            action = CONF[plugin.RESOURCE_TYPE].before_end
        if action == 'snapshot':
            pool = nova.ReservationPool()
            client = nova.BlazarNovaClient()
            for host in pool.get_computehosts(
                    host_reservation['aggregate_id']):
                for server in client.servers.list(
                        search_opts={"host": host, "all_tenants": 1}):
                        client.servers.create_image(server=server)

    def on_end(self, resource_id):
        """Remove the hosts from the pool."""
        host_reservation = db_api.host_reservation_get(resource_id)
        db_api.host_reservation_update(host_reservation['id'],
                                       {'status': 'completed'})
        allocations = db_api.host_allocation_get_all_by_values(
            reservation_id=host_reservation['reservation_id'])
        for allocation in allocations:
            db_api.host_allocation_destroy(allocation['id'])
        pool = nova.ReservationPool()
        for host in pool.get_computehosts(host_reservation['aggregate_id']):
            for server in self.nova.servers.list(
                    search_opts={"host": host, "all_tenants": 1}):
                try:
                    self.nova.servers.delete(server=server)
                except nova_exceptions.NotFound:
                    LOG.info('Could not find server %s, may have been deleted '
                             'concurrently.', server)
                except Exception as e:
                    LOG.exception('Failed to delete %s: %s.', server, str(e))
        try:
            pool.delete(host_reservation['aggregate_id'])
        except manager_ex.AggregateNotFound:
            pass

    def heal_reservations(self, failed_resources, interval_begin,
                          interval_end):
        """Heal reservations which suffer from resource failures.

        :param: failed_resources: a list of failed hosts.
        :param: interval_begin: start date of the period to heal.
        :param: interval_end: end date of the period to heal.
        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                       {'missing_resources': True}}
        """
        reservation_flags = {}

        host_ids = [h['id'] for h in failed_resources]
        reservations = db_utils.get_reservations_by_host_ids(host_ids,
                                                             interval_begin,
                                                             interval_end)

        for reservation in reservations:
            if reservation['resource_type'] != plugin.RESOURCE_TYPE:
                continue

            for allocation in [alloc for alloc
                               in reservation['computehost_allocations']
                               if alloc['compute_host_id'] in host_ids]:
                if self._reallocate(allocation):
                    if reservation['status'] == status.reservation.ACTIVE:
                        if reservation['id'] not in reservation_flags:
                            reservation_flags[reservation['id']] = {}
                        reservation_flags[reservation['id']].update(
                            {'resources_changed': True})
                else:
                    if reservation['id'] not in reservation_flags:
                        reservation_flags[reservation['id']] = {}
                    reservation_flags[reservation['id']].update(
                        {'missing_resources': True})

        return reservation_flags

    def _reallocate(self, allocation):
        """Allocate an alternative host.

        :param: allocation: allocation to change.
        :return: True if an alternative host was successfully allocated.
        """
        reservation = db_api.reservation_get(allocation['reservation_id'])
        h_reservation = db_api.host_reservation_get(
            reservation['resource_id'])
        lease = db_api.lease_get(reservation['lease_id'])
        pool = nova.ReservationPool()

        # Remove the old host from the aggregate.
        if reservation['status'] == status.reservation.ACTIVE:
            host = db_api.host_get(allocation['compute_host_id'])
            pool.remove_computehost(h_reservation['aggregate_id'],
                                    host['service_name'])

        # Allocate an alternative host.
        start_date = max(datetime.datetime.utcnow(), lease['start_date'])
        new_hostids = self._matching_hosts(
            reservation['hypervisor_properties'],
            reservation['resource_properties'],
            '1-1', start_date, lease['end_date']
        )
        if not new_hostids:
            db_api.host_allocation_destroy(allocation['id'])
            LOG.warn('Could not find alternative host for reservation %s '
                     '(lease: %s).', reservation['id'], lease['name'])
            return False
        else:
            new_hostid = new_hostids.pop()
            db_api.host_allocation_update(allocation['id'],
                                          {'compute_host_id': new_hostid})
            LOG.warn('Resource changed for reservation %s (lease: %s).',
                     reservation['id'], lease['name'])
            if reservation['status'] == status.reservation.ACTIVE:
                # Add the alternative host into the aggregate.
                new_host = db_api.host_get(new_hostid)
                pool.add_computehost(h_reservation['aggregate_id'],
                                     new_host['service_name'])

            return True

    def _get_extra_capabilities(self, host_id):
        extra_capabilities = {}
        raw_extra_capabilities = (
            db_api.host_extra_capability_get_all_per_host(host_id))
        for capability in raw_extra_capabilities:
            key = capability['capability_name']
            extra_capabilities[key] = capability['capability_value']
        return extra_capabilities

    def get_computehost(self, host_id):
        host = db_api.host_get(host_id)
        extra_capabilities = self._get_extra_capabilities(host_id)
        if host is not None and extra_capabilities:
            res = host.copy()
            res.update(extra_capabilities)
            return res
        else:
            return host

    def list_computehosts(self):
        raw_host_list = db_api.host_list()
        host_list = []
        for host in raw_host_list:
            host_list.append(self.get_computehost(host['id']))
        return host_list

    def create_computehost(self, host_values):
        # TODO(sbauza):
        #  - Exception handling for HostNotFound
        host_id = host_values.pop('id', None)
        host_name = host_values.pop('name', None)
        try:
            trust_id = host_values.pop('trust_id')
        except KeyError:
            raise manager_ex.MissingTrustId()

        host_ref = host_id or host_name
        if host_ref is None:
            raise manager_ex.InvalidHost(host=host_values)

        with trusts.create_ctx_from_trust(trust_id):
            inventory = nova.NovaInventory()
            servers = inventory.get_servers_per_host(host_ref)
            if servers:
                raise manager_ex.HostHavingServers(host=host_ref,
                                                   servers=servers)
            host_details = inventory.get_host_details(host_ref)
            # NOTE(sbauza): Only last duplicate name for same extra capability
            # will be stored
            to_store = set(host_values.keys()) - set(host_details.keys())
            extra_capabilities_keys = to_store
            extra_capabilities = dict(
                (key, host_values[key]) for key in extra_capabilities_keys
            )

            if any([len(key) > 64 for key in extra_capabilities_keys]):
                raise manager_ex.ExtraCapabilityTooLong()

            pool = nova.ReservationPool()
            pool.add_computehost(self.freepool_name,
                                 host_details['service_name'])

            host = None
            cantaddextracapability = []
            try:
                if trust_id:
                    host_details.update({'trust_id': trust_id})
                host = db_api.host_create(host_details)
            except db_ex.BlazarDBException:
                # We need to rollback
                # TODO(sbauza): Investigate use of Taskflow for atomic
                # transactions
                pool.remove_computehost(self.freepool_name,
                                        host_details['service_name'])
            if host:
                for key in extra_capabilities:
                    values = {'computehost_id': host['id'],
                              'capability_name': key,
                              'capability_value': extra_capabilities[key],
                              }
                    try:
                        db_api.host_extra_capability_create(values)
                    except db_ex.BlazarDBException:
                        cantaddextracapability.append(key)
            if cantaddextracapability:
                raise manager_ex.CantAddExtraCapability(
                    keys=cantaddextracapability,
                    host=host['id'])
            if host:
                return self.get_computehost(host['id'])
            else:
                return None

    def is_updatable_extra_capability(self, capability):
        reservations = db_utils.get_reservations_by_host_id(
            capability['computehost_id'], datetime.datetime.utcnow(),
            datetime.date.max)

        for r in reservations:
            plugin_reservation = db_utils.get_plugin_reservation(
                r['resource_type'], r['resource_id'])

            requirements_queries = plugins_utils.convert_requirements(
                plugin_reservation['resource_properties'])

            # TODO(masahito): If all the reservations using the
            # extra_capability can be re-allocated it's okay to update
            # the extra_capability.
            for requirement in requirements_queries:
                # A requirement is of the form "key op value" as string
                if requirement.split(" ")[0] == capability['capability_name']:
                    return False
        return True

    def update_computehost(self, host_id, values):
        # nothing to update
        if not values:
            return self.get_computehost(host_id)

        cant_update_extra_capability = []
        previous_capabilities = self._get_extra_capabilities(host_id)
        updated_keys = set(values.keys()) & set(previous_capabilities.keys())
        new_keys = set(values.keys()) - set(previous_capabilities.keys())

        for key in updated_keys:
            raw_capability = next(iter(
                db_api.host_extra_capability_get_all_per_name(host_id, key)))
            capability = {
                'capability_name': key,
                'capability_value': values[key],
            }
            if self.is_updatable_extra_capability(raw_capability):
                try:
                    db_api.host_extra_capability_update(
                        raw_capability['id'], capability)
                except (db_ex.BlazarDBException, RuntimeError):
                    cant_update_extra_capability.append(
                        raw_capability['capability_name'])
            else:
                LOG.info("Capability %s can't be updated because "
                         "existing reservations require it.",
                         raw_capability['capability_name'])
                cant_update_extra_capability.append(
                    raw_capability['capability_name'])

        for key in new_keys:
            new_capability = {
                'computehost_id': host_id,
                'capability_name': key,
                'capability_value': values[key],
            }
            try:
                db_api.host_extra_capability_create(new_capability)
            except (db_ex.BlazarDBException, RuntimeError):
                cant_update_extra_capability.append(
                    new_capability['capability_name'])

        if cant_update_extra_capability:
            raise manager_ex.CantAddExtraCapability(
                host=host_id, keys=cant_update_extra_capability)

        LOG.info('Extra capabilities on compute host %s updated with %s',
                 host_id, values)
        return self.get_computehost(host_id)

    def delete_computehost(self, host_id):
        host = db_api.host_get(host_id)
        if not host:
            raise manager_ex.HostNotFound(host=host_id)

        with trusts.create_ctx_from_trust(host['trust_id']):
            if db_api.host_allocation_get_all_by_values(
                    compute_host_id=host_id):
                raise manager_ex.CantDeleteHost(
                    host=host_id,
                    msg='The host is reserved.'
                )

            inventory = nova.NovaInventory()
            servers = inventory.get_servers_per_host(
                host['hypervisor_hostname'])
            if servers:
                raise manager_ex.HostHavingServers(
                    host=host['hypervisor_hostname'], servers=servers)

            try:
                pool = nova.ReservationPool()
                pool.remove_computehost(self.freepool_name,
                                        host['service_name'])
                # NOTE(sbauza): Extracapabilities will be destroyed thanks to
                #  the DB FK.
                db_api.host_destroy(host_id)
            except db_ex.BlazarDBException as e:
                # Nothing so bad, but we need to alert admins
                # they have to rerun
                raise manager_ex.CantDeleteHost(host=host_id, msg=str(e))

    def _matching_hosts(self, hypervisor_properties, resource_properties,
                        count_range, start_date, end_date):
        """Return the matching hosts (preferably not allocated)

        """
        count_range = count_range.split('-')
        min_host = count_range[0]
        max_host = count_range[1]
        allocated_host_ids = []
        not_allocated_host_ids = []
        filter_array = []
        start_date_with_margin = start_date - datetime.timedelta(
            minutes=CONF.cleaning_time)
        end_date_with_margin = end_date + datetime.timedelta(
            minutes=CONF.cleaning_time)

        # TODO(frossigneux) support "or" operator
        if hypervisor_properties:
            filter_array = plugins_utils.convert_requirements(
                hypervisor_properties)
        if resource_properties:
            filter_array += plugins_utils.convert_requirements(
                resource_properties)
        for host in db_api.reservable_host_get_all_by_queries(filter_array):
            if not db_api.host_allocation_get_all_by_values(
                    compute_host_id=host['id']):
                not_allocated_host_ids.append(host['id'])
            elif db_utils.get_free_periods(
                host['id'],
                start_date_with_margin,
                end_date_with_margin,
                end_date_with_margin - start_date_with_margin
            ) == [
                (start_date_with_margin, end_date_with_margin),
            ]:
                allocated_host_ids.append(host['id'])
        if len(not_allocated_host_ids) >= int(min_host):
            return not_allocated_host_ids[:int(max_host)]
        all_host_ids = allocated_host_ids + not_allocated_host_ids
        if len(all_host_ids) >= int(min_host):
            return all_host_ids[:int(max_host)]
        else:
            return []

    def _convert_int_param(self, param, name):
        """Checks that the parameter is present and can be converted to int."""
        if param is None:
            raise manager_ex.MissingParameter(param=name)
        if strutils.is_int_like(param):
            param = int(param)
        else:
            raise manager_ex.MalformedParameter(param=name)
        return param

    def _check_params(self, values):
        min_hosts = self._convert_int_param(values.get('min'), 'min')
        max_hosts = self._convert_int_param(values.get('max'), 'max')

        if 0 <= min_hosts and min_hosts <= max_hosts:
            values['count_range'] = str(min_hosts) + '-' + str(max_hosts)
        else:
            raise manager_ex.InvalidRange()

        if 'hypervisor_properties' not in values:
            raise manager_ex.MissingParameter(param='hypervisor_properties')
        if 'resource_properties' not in values:
            raise manager_ex.MissingParameter(param='resource_properties')

        if 'before_end' not in values:
            values['before_end'] = 'default'
        if values['before_end'] not in before_end_options:
            raise manager_ex.MalformedParameter(param='before_end')

    def _update_allocations(self, dates_before, dates_after, reservation_id,
                            reservation_status, host_reservation, values):
        min_hosts = self._convert_int_param(values.get(
            'min', host_reservation['count_range'].split('-')[0]), 'min')
        max_hosts = self._convert_int_param(values.get(
            'max', host_reservation['count_range'].split('-')[1]), 'max')
        if min_hosts < 0 or max_hosts < min_hosts:
            raise manager_ex.InvalidRange()
        hypervisor_properties = values.get(
            'hypervisor_properties',
            host_reservation['hypervisor_properties'])
        resource_properties = values.get(
            'resource_properties',
            host_reservation['resource_properties'])
        allocs = db_api.host_allocation_get_all_by_values(
            reservation_id=reservation_id)

        allocs_to_remove = self._allocations_to_remove(
            dates_before, dates_after, max_hosts, hypervisor_properties,
            resource_properties, allocs)

        if (allocs_to_remove and
                reservation_status == status.reservation.ACTIVE):
            raise manager_ex.NotEnoughHostsAvailable()

        kept_hosts = len(allocs) - len(allocs_to_remove)
        if kept_hosts < max_hosts:
            min_hosts = min_hosts - kept_hosts \
                if (min_hosts - kept_hosts) > 0 else 0
            max_hosts = max_hosts - kept_hosts
            host_ids = self._matching_hosts(
                hypervisor_properties, resource_properties,
                str(min_hosts) + '-' + str(max_hosts),
                dates_after['start_date'], dates_after['end_date'])
            if len(host_ids) >= min_hosts:
                for host_id in host_ids:
                    db_api.host_allocation_create(
                        {'compute_host_id': host_id,
                         'reservation_id': reservation_id})
            else:
                raise manager_ex.NotEnoughHostsAvailable()

        for allocation in allocs_to_remove:
            db_api.host_allocation_destroy(allocation['id'])

    def _allocations_to_remove(self, dates_before, dates_after, max_hosts,
                               hypervisor_properties, resource_properties,
                               allocs):
        allocs_to_remove = []
        requested_host_ids = [host['id'] for host in
                              self._filter_hosts_by_properties(
                                  hypervisor_properties, resource_properties)]

        for alloc in allocs:
            if alloc['compute_host_id'] not in requested_host_ids:
                allocs_to_remove.append(alloc)
                continue
            if (dates_before['start_date'] > dates_after['start_date'] or
                    dates_before['end_date'] < dates_after['end_date']):
                reserved_periods = db_utils.get_reserved_periods(
                    alloc['compute_host_id'],
                    dates_after['start_date'],
                    dates_after['end_date'],
                    datetime.timedelta(seconds=1))

                max_start = max(dates_before['start_date'],
                                dates_after['start_date'])
                min_end = min(dates_before['end_date'],
                              dates_after['end_date'])

                if not (len(reserved_periods) == 0 or
                        (len(reserved_periods) == 1 and
                         reserved_periods[0][0] == max_start and
                         reserved_periods[0][1] == min_end)):
                    allocs_to_remove.append(alloc)
                    continue

        kept_hosts = len(allocs) - len(allocs_to_remove)
        if kept_hosts > max_hosts:
            allocs_to_remove.extend(
                [allocation for allocation in allocs
                 if allocation not in allocs_to_remove
                 ][:(kept_hosts - max_hosts)]
            )

        return allocs_to_remove

    def _filter_hosts_by_properties(self, hypervisor_properties,
                                    resource_properties):
        filter = []
        if hypervisor_properties:
            filter += plugins_utils.convert_requirements(hypervisor_properties)
        if resource_properties:
            filter += plugins_utils.convert_requirements(resource_properties)
        if filter:
            return db_api.host_get_all_by_queries(filter)
        else:
            return db_api.host_list()


class PhysicalHostMonitorPlugin(base.BaseMonitorPlugin,
                                nova.NovaClientWrapper):
    """Monitor plugin for physical host resource."""

    # Singleton design pattern
    _instance = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super(PhysicalHostMonitorPlugin, cls).__new__(cls)
            cls._instance.healing_handlers = []
            super(PhysicalHostMonitorPlugin, cls._instance).__init__(
                username=CONF.os_admin_username,
                password=CONF.os_admin_password,
                user_domain_name=CONF.os_admin_user_domain_name,
                project_name=CONF.os_admin_project_name,
                project_domain_name=CONF.os_admin_project_domain_name)
        return cls._instance

    def __init__(self):
        """Do nothing.

        This class uses the Singleton design pattern and an instance of this
        class is generated and initialized in __new__().
        """
        pass

    def register_healing_handler(self, handler):
        self.healing_handlers.append(handler)

    def is_notification_enabled(self):
        """Check if the notification monitor is enabled."""
        return CONF[plugin.RESOURCE_TYPE].enable_notification_monitor

    def get_notification_event_types(self):
        """Get event types of notification messages to handle."""
        return ['service.update']

    def get_notification_topics(self):
        """Get topics of notification to subscribe to."""
        return CONF[plugin.RESOURCE_TYPE].notification_topics

    def notification_callback(self, event_type, payload):
        """Handle a notification message.

        It is used as a callback of a notification-based resource monitor.

        :param event_type: an event type of a notification.
        :param payload: a payload of a notification.
        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                       {'missing_resources': True}}
        """
        LOG.trace('Handling a notification...')
        reservation_flags = {}

        data = payload.get('nova_object.data', None)
        if data:
            if data['disabled'] or data['forced_down']:
                failed_hosts = db_api.reservable_host_get_all_by_queries(
                    ['hypervisor_hostname == ' + data['host']])
                if failed_hosts:
                    LOG.warn('%s failed.',
                             failed_hosts[0]['hypervisor_hostname'])
                    reservation_flags = self._handle_failures(failed_hosts)
            else:
                recovered_hosts = db_api.host_get_all_by_queries(
                    ['reservable == 0',
                     'hypervisor_hostname == ' + data['host']])
                if recovered_hosts:
                    db_api.host_update(recovered_hosts[0]['id'],
                                       {'reservable': True})
                    LOG.warn('%s recovered.',
                             recovered_hosts[0]['hypervisor_hostname'])

        return reservation_flags

    def is_polling_enabled(self):
        """Check if the polling monitor is enabled."""
        return CONF[plugin.RESOURCE_TYPE].enable_polling_monitor

    def get_polling_interval(self):
        """Get interval of polling."""
        return CONF[plugin.RESOURCE_TYPE].polling_interval

    def poll(self):
        """Detect and handle resource failures.

        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                 {'missing_resources': True}}
        """
        LOG.trace('Poll...')
        reservation_flags = {}

        failed_hosts, recovered_hosts = self._poll_resource_failures()
        if failed_hosts:
            for host in failed_hosts:
                LOG.warn('%s failed.', host['hypervisor_hostname'])
            reservation_flags = self._handle_failures(failed_hosts)
        if recovered_hosts:
            for host in recovered_hosts:
                db_api.host_update(host['id'], {'reservable': True})
                LOG.warn('%s recovered.', host['hypervisor_hostname'])

        return reservation_flags

    def _poll_resource_failures(self):
        """Check health of hosts by calling Nova Hypervisors API.

        :return: a list of failed hosts, a list of recovered hosts.
        """
        hosts = db_api.host_get_all_by_filters({})
        reservable_hosts = [h for h in hosts if h['reservable'] is True]
        unreservable_hosts = [h for h in hosts if h['reservable'] is False]

        try:
            hvs = self.nova.hypervisors.list()

            failed_hv_ids = [str(hv.id) for hv in hvs
                             if hv.state == 'down' or hv.status == 'disabled']
            failed_hosts = [host for host in reservable_hosts
                            if host['id'] in failed_hv_ids]

            active_hv_ids = [str(hv.id) for hv in hvs
                             if hv.state == 'up' and hv.status == 'enabled']
            recovered_hosts = [host for host in unreservable_hosts
                               if host['id'] in active_hv_ids]
        except Exception as e:
            LOG.exception('Skipping health check. %s', str(e))

        return failed_hosts, recovered_hosts

    def _handle_failures(self, failed_hosts):
        """Handle resource failures.

        :param: failed_hosts: a list of failed hosts.
        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                 {'missing_resources': True}}
        """

        # Update the computehosts table
        for host in failed_hosts:
            try:
                db_api.host_update(host['id'], {'reservable': False})
            except Exception as e:
                LOG.exception('Failed to update %s. %s',
                              host['hypervisor_hostname'], str(e))

        # Heal related reservations
        return self.heal()

    def get_healing_interval(self):
        """Get interval of reservation healing in minutes."""
        return CONF[plugin.RESOURCE_TYPE].healing_interval

    def heal(self):
        """Heal suffering reservations in the next healing interval.

        :return: a dictionary of {reservation id: flags to update}
        """
        reservation_flags = {}
        hosts = db_api.unreservable_host_get_all_by_queries([])

        interval_begin = datetime.datetime.utcnow()
        interval = self.get_healing_interval()
        if interval == 0:
            interval_end = datetime.date.max
        else:
            interval_end = interval_begin + datetime.timedelta(
                minutes=interval)

        for handler in self.healing_handlers:
            reservation_flags.update(handler(hosts,
                                             interval_begin,
                                             interval_end))

        return reservation_flags
