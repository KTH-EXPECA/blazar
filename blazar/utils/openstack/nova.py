# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import uuid as uuidgen

from novaclient import client as nova_client
from novaclient import exceptions as nova_exception
from novaclient.v2 import servers
from oslo_config import cfg
from oslo_log import log as logging

from blazar.manager import exceptions as manager_exceptions
from blazar.plugins import oshosts
from blazar.utils.openstack import base


nova_opts = [
    cfg.StrOpt('nova_client_version',
               default='2',
               deprecated_group='DEFAULT',
               help='Novaclient version'),
    cfg.StrOpt('compute_service',
               default='compute',
               deprecated_group='DEFAULT',
               help='Nova name in keystone'),
    cfg.StrOpt('image_prefix',
               default='reserved_',
               deprecated_group='DEFAULT',
               help='Prefix for VM images if you want to create snapshots'),
    cfg.StrOpt('aggregate_freepool_name',
               default='freepool',
               deprecated_group=oshosts.RESOURCE_TYPE,
               help='Name of the special aggregate where all hosts '
                    'are candidate for physical host reservation'),
    cfg.StrOpt('project_id_key',
               default='blazar:project',
               deprecated_group=oshosts.RESOURCE_TYPE,
               help='Aggregate metadata value for key matching project_id'),
    cfg.StrOpt('blazar_owner',
               default='blazar:owner',
               deprecated_group=oshosts.RESOURCE_TYPE,
               help='Aggregate metadata key for knowing owner project_id'),
    cfg.BoolOpt('az_aware',
                default=True,
                help='A flag to store original availability zone'),
    cfg.StrOpt('endpoint_override',
               help='Nova endpoint URL to use')
]


CONF = cfg.CONF
CONF.register_opts(nova_opts, group='nova')
CONF.import_opt('identity_service', 'blazar.utils.openstack.keystone')
LOG = logging.getLogger(__name__)


class BlazarNovaClient(object):
    def __init__(self, **kwargs):
        client_kwargs = base.client_kwargs(**kwargs)
        self.nova = nova_client.Client(
            CONF.nova.nova_client_version, **client_kwargs)
        self.nova.servers = ServerManager(self.nova)
        self.exceptions = nova_exception

    def __getattr__(self, name):
        return getattr(self.nova, name)


class ServerManager(servers.ServerManager):

    def create_image(self, server, image_name=None, metadata=None):
        """Snapshot a server."""
        if image_name is None:
            image_name = CONF.nova.image_prefix + server.name
        return super(ServerManager, self).create_image(server,
                                                       image_name=image_name,
                                                       metadata=metadata)


class NovaClientWrapper(object):
    @property
    def nova(self):
        nova = BlazarNovaClient(endpoint_override=CONF.nova.endpoint_override)
        return nova


class ReservationPool(NovaClientWrapper):
    def __init__(self):
        super(ReservationPool, self).__init__()
        self.config = CONF.nova
        self.freepool_name = self.config.aggregate_freepool_name

    def get_aggregate_from_name_or_id(self, aggregate_obj):
        """Return an aggregate by name or an id."""

        aggregate = None
        agg_id = None
        try:
            agg_id = int(aggregate_obj)
        except (ValueError, TypeError):
            if hasattr(aggregate_obj, 'id') and aggregate_obj.id:
                # pool is an aggregate
                agg_id = aggregate_obj.id

        if agg_id is not None:
            try:
                aggregate = self.nova.aggregates.get(agg_id)
            except nova_exception.NotFound:
                aggregate = None
        else:
            # FIXME(scroiset): can't get an aggregate by name
            # so iter over all aggregate and check for the good one
            all_aggregates = self.nova.aggregates.list()
            for agg in all_aggregates:
                if aggregate_obj == agg.name:
                    aggregate = agg
        if aggregate:
            return aggregate
        else:
            raise manager_exceptions.AggregateNotFound(pool=aggregate_obj)

    @staticmethod
    def _generate_aggregate_name():
        return str(uuidgen.uuid4())

    def create(self, name=None, project_id=None, az=None, metadata=None):
        """Create a Pool (an Aggregate) with or without Availability Zone.

        By default expose to user the aggregate with an Availability Zone.
        Return an aggregate or raise a nova exception.

        """

        name = name or self._generate_aggregate_name()
        if not project_id:
            raise manager_exceptions.ProjectIdNotFound()

        LOG.debug('Creating pool aggregate: %(name)s for %(project_id)s with '
                  'Availability Zone %(az)s', {
                      'name': name,
                      'project_id': project_id,
                      'az': az
                  })
        agg = self.nova.aggregates.create(name, az)

        if metadata:
            metadata[self.config.blazar_owner] = project_id
        else:
            metadata = {self.config.blazar_owner: project_id}
        self.nova.aggregates.set_metadata(agg, metadata)

        return agg

    def delete(self, pool, force=True):
        """Delete an aggregate.

        pool can be an aggregate name or an aggregate id.
        Remove all hosts before delete aggregate (default).
        If force is False, raise exception if at least one
        host is attached to.

        """

        try:
            agg = self.get_aggregate_from_name_or_id(pool)
        except manager_exceptions.AggregateNotFound:
            LOG.warn("Aggregate '%s' not found, skipping deletion", pool)
            return

        hosts = agg.hosts
        if len(hosts) > 0 and not force:
            raise manager_exceptions.AggregateHaveHost(name=agg.name,
                                                       hosts=agg.hosts)
        try:
            freepool_agg = self.get(self.freepool_name)
        except manager_exceptions.AggregateNotFound:
            raise manager_exceptions.NoFreePool()
        for host in hosts:
            LOG.debug("Removing host '%(host)s' from aggregate '%(id)s')",
                      {'host': host, 'id': agg.id})
            self.nova.aggregates.remove_host(agg.id, host)

            if freepool_agg.id != agg.id and host not in freepool_agg.hosts:
                self.nova.aggregates.add_host(freepool_agg.id, host)

        self.nova.aggregates.delete(agg.id)

    def get_all(self):
        """Return all aggregate."""

        return self.nova.aggregates.list()

    def get(self, pool):
        """return details for aggregate pool or raise AggregateNotFound."""

        return self.get_aggregate_from_name_or_id(pool)

    def get_computehosts(self, pool):
        """Return a list of compute host names for an aggregate."""

        try:
            agg = self.get_aggregate_from_name_or_id(pool)
            return agg.hosts
        except manager_exceptions.AggregateNotFound:
            return []

    def add_computehost(self, pool, hosts, stay_in=False):
        """Add compute host(s) to an aggregate.

        Each host must exist and be in the freepool, otherwise raise an error.

        :param pool: Name or UUID of the pool to rattach the host
        :param hosts: Names (not UUID) of hosts to associate
        :type host: str or list of str

        Return the related aggregate.
        Raise an aggregate exception if something wrong.
        """

        if not isinstance(hosts, list):
            hosts = [hosts]

        added_hosts = []
        removed_hosts = []
        agg = self.get_aggregate_from_name_or_id(pool)

        try:
            freepool_agg = self.get(self.freepool_name)
        except manager_exceptions.AggregateNotFound:
            raise manager_exceptions.NoFreePool()

        try:
            for host in hosts:
                if freepool_agg.id != agg.id and not stay_in:
                    if host not in freepool_agg.hosts:
                        raise manager_exceptions.HostNotInFreePool(
                            host=host, freepool_name=freepool_agg.name)
                    LOG.info("removing host '%(host)s' from freepool "
                             "aggregate %(name)s",
                             {'host': host, 'name': freepool_agg.name})
                    try:
                        self.remove_computehost(freepool_agg.id, host)
                        removed_hosts.append(host)
                    except nova_exception.NotFound:
                        raise manager_exceptions.HostNotFound(host=host)

                LOG.info("adding host '%(host)s' to aggregate %(id)s",
                         {'host': host, 'id': agg.id})
                try:
                    self.nova.aggregates.add_host(agg.id, host)
                    added_hosts.append(host)
                except nova_exception.NotFound:
                    raise manager_exceptions.HostNotFound(host=host)
                except nova_exception.Conflict as e:
                    raise manager_exceptions.AggregateAlreadyHasHost(
                        pool=pool, host=host, nova_exception=str(e))

                # remove preemptible instances
                for server in self.nova.servers.list(
                        search_opts={"node": host, "all_tenants": 1}):
                    try:
                        LOG.info('Terminating preemptible instance %s (%s)',
                                 server.name, server.id)
                        self.nova.servers.delete(server=server)
                    except nova_exception.NotFound:
                        LOG.info('Could not find server %s, may have been '
                                 'deleted concurrently.', server)
                    except Exception as e:
                        LOG.exception(
                            'Failed to delete %s: %s.', server, str(e))

        except Exception as e:
            if added_hosts:
                LOG.warn('Removing hosts added to aggregate %s: %s',
                         agg.id, added_hosts)
                for host in added_hosts:
                    self.nova.aggregates.remove_host(agg.id, host)
            if removed_hosts:
                LOG.warn('Adding hosts back to freepool: %s', removed_hosts)
                for host in removed_hosts:
                    self.nova.aggregates.add_host(freepool_agg.id, host)
            raise e

        return self.get_aggregate_from_name_or_id(pool)

    def remove_all_computehosts(self, pool):
        """Remove all compute hosts attached to an aggregate."""

        hosts = self.get_computehosts(pool)
        self.remove_computehost(pool, hosts)

    def remove_computehost(self, pool, hosts):
        """Remove compute host(s) from an aggregate."""

        if not isinstance(hosts, list):
            hosts = [hosts]

        agg = self.get_aggregate_from_name_or_id(pool)

        try:
            freepool_agg = self.get(self.freepool_name)
        except manager_exceptions.AggregateNotFound:
            raise manager_exceptions.NoFreePool()

        hosts_failing_to_remove = []
        hosts_failing_to_add = []
        hosts_not_in_freepool = []
        for host in hosts:
            if freepool_agg.id == agg.id:
                if host not in freepool_agg.hosts:
                    hosts_not_in_freepool.append(host)
                    continue
            try:
                self.nova.aggregates.remove_host(agg.id, host)
            except nova_exception.ClientException:
                hosts_failing_to_remove.append(host)
            if freepool_agg.id != agg.id and host not in freepool_agg.hosts:
                # NOTE(sbauza) : We don't want to put again the host in
                # freepool if the requested pool is the freepool...
                try:
                    self.nova.aggregates.add_host(freepool_agg.id, host)
                except nova_exception.ClientException:
                    hosts_failing_to_add.append(host)

        if hosts_failing_to_remove:
            raise manager_exceptions.CantRemoveHost(
                host=hosts_failing_to_remove, pool=agg)
        if hosts_failing_to_add:
            raise manager_exceptions.CantAddHost(host=hosts_failing_to_add,
                                                 pool=freepool_agg)
        if hosts_not_in_freepool:
            raise manager_exceptions.HostNotInFreePool(
                host=hosts_not_in_freepool, freepool_name=freepool_agg.name)

    def add_project(self, pool, project_id):
        """Add a project to an aggregate."""

        metadata = {project_id: self.config.project_id_key}

        agg = self.get_aggregate_from_name_or_id(pool)

        return self.nova.aggregates.set_metadata(agg.id, metadata)

    def remove_project(self, pool, project_id):
        """Remove a project from an aggregate."""

        agg = self.get_aggregate_from_name_or_id(pool)

        metadata = {project_id: None}
        return self.nova.aggregates.set_metadata(agg.id, metadata)


class NovaInventory(NovaClientWrapper):

    def get_host_details(self, host):
        """Get Nova capabilities of a single host

        :param host: UUID, ID or name of nova-compute host
        :return: Dict of capabilities or raise HostNotFound
        """
        try:
            # NOTE(tetsuro): Only id (microversion < 2.53) or uuid
            # (microversion >= 2.53) is acceptable for the
            # `novaclient.hypervisors.get` argument. The invalid arguments
            # result in NotFound exception for microversion < 2.53 and
            # BadRequest exception for microversion >= 2.53
            hypervisor = self.nova.hypervisors.get(host)
        except (nova_exception.NotFound, nova_exception.BadRequest):
            # Name (not id or uuid) is given for the `host` parameter.
            try:
                hypervisors_list = self.nova.hypervisors.search(host)
            except nova_exception.NotFound:
                raise manager_exceptions.HostNotFound(host=host)
            if len(hypervisors_list) > 1:
                raise manager_exceptions.MultipleHostsFound(host=host)
            else:
                hypervisor_id = hypervisors_list[0].id
                # NOTE(sbauza): No need to catch the exception as we're sure
                #  that the hypervisor exists
                hypervisor = self.nova.hypervisors.get(hypervisor_id)

        az_name = ''
        if CONF.nova.az_aware:
            host_name = hypervisor.service['host']
            for zone in self.nova.availability_zones.list(detailed=True):
                if (zone.hosts and host_name in zone.hosts
                   and 'nova-compute' in zone.hosts[host_name]):
                    az_name = zone.zoneName

        try:
            # NOTE(tetsuro): compute API microversion 2.28 changes cpu_info
            # from string to object
            cpu_info = str(hypervisor.cpu_info)
            return {'id': hypervisor.id,
                    'availability_zone': az_name,
                    'hypervisor_hostname': hypervisor.hypervisor_hostname,
                    'service_name': hypervisor.service['host'],
                    'vcpus': hypervisor.vcpus,
                    'cpu_info': cpu_info,
                    'hypervisor_type': hypervisor.hypervisor_type,
                    'hypervisor_version': hypervisor.hypervisor_version,
                    'memory_mb': hypervisor.memory_mb,
                    'local_gb': hypervisor.local_gb}
        except AttributeError:
            raise manager_exceptions.InvalidHost(host=host)

    def get_servers_per_host(self, host):
        """List all servers of a nova-compute host

        :param host: Name (not UUID) of nova-compute host
        :return: Dict of servers or None
        """
        try:
            hypervisors_list = self.nova.hypervisors.search(host, servers=True)
        except nova_exception.NotFound:
            raise manager_exceptions.HostNotFound(host=host)
        if len(hypervisors_list) > 1:
            raise manager_exceptions.MultipleHostsFound(host=host)
        else:
            try:
                return hypervisors_list[0].servers
            except AttributeError:
                # NOTE(sbauza): nova.hypervisors.search(servers=True) returns
                #  a list of hosts without 'servers' attribute if no servers
                #  are running on that host
                return None
