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

"""Implementation of SQLAlchemy backend."""

import sys

from oslo_config import cfg

from blazar.db import exceptions as db_exc
from blazar.db.sqlalchemy import facade_wrapper
from blazar.db.sqlalchemy import models
from oslo_db import exception as common_db_exc
from oslo_db.sqlalchemy import session as db_session
from oslo_log import log as logging
import sqlalchemy as sa
from sqlalchemy.sql.expression import asc
from sqlalchemy.sql.expression import desc


EXTRA_CAPABILITY_MODELS = {
    'physical:host': models.ComputeHostExtraCapability,
    'network': models.NetworkSegmentExtraCapability,
    'device': models.DeviceExtraCapability,
}
FORBIDDEN_EXTRA_CAPABILITY_NAMES = ["id", "reservable"]

LOG = logging.getLogger(__name__)

get_engine = facade_wrapper.get_engine
get_session = facade_wrapper.get_session


def get_backend():
    """The backend is this module itself."""
    return sys.modules[__name__]


def _read_deleted_filter(query, db_model, deleted):
    if 'deleted' not in db_model.__table__.columns:
        return query

    default_deleted_value = None
    if not deleted:
        query = query.filter(db_model.deleted == default_deleted_value)
    return query


def model_query(model, session=None, deleted=False):
    """Query helper.

    :param model: base model to query
    """
    session = session or get_session()

    return _read_deleted_filter(session.query(model), model, deleted)


def setup_db():
    try:
        engine = db_session.EngineFacade(cfg.CONF.database.connection,
                                         sqlite_fk=True).get_engine()
        models.Lease.metadata.create_all(engine)
    except sa.exc.OperationalError as e:
        LOG.error("Database registration exception: %s", e)
        return False
    return True


def drop_db():
    try:
        engine = db_session.EngineFacade(cfg.CONF.database.connection,
                                         sqlite_fk=True).get_engine()
        models.Lease.metadata.drop_all(engine)
    except Exception as e:
        LOG.error("Database shutdown exception: %s", e)
        return False
    return True


# Helpers for building constraints / equality checks


def constraint(**conditions):
    return Constraint(conditions)


def equal_any(*values):
    return EqualityCondition(values)


def not_equal(*values):
    return InequalityCondition(values)


class Constraint(object):
    def __init__(self, conditions):
        self.conditions = conditions

    def apply(self, model, query):
        for key, condition in self.conditions.items():
            for clause in condition.clauses(getattr(model, key)):
                query = query.filter(clause)
        return query


class EqualityCondition(object):
    def __init__(self, values):
        self.values = values

    def clauses(self, field):
        return sa.or_([field == value for value in self.values])


class InequalityCondition(object):
    def __init__(self, values):
        self.values = values

    def clauses(self, field):
        return [field != value for value in self.values]


# Reservation
def _reservation_get(session, reservation_id):
    query = model_query(models.Reservation, session)
    return query.filter_by(id=reservation_id).first()


def reservation_get(reservation_id):
    return _reservation_get(get_session(), reservation_id)


def reservation_get_all():
    query = model_query(models.Reservation, get_session())
    return query.all()


def reservation_get_all_by_lease_id(lease_id):
    reservations = (model_query(models.Reservation,
                                get_session()).filter_by(lease_id=lease_id))
    return reservations.all()


def reservation_get_all_by_values(**kwargs):
    """Returns all entries filtered by col=value."""

    reservation_query = model_query(models.Reservation, get_session())
    for name, value in kwargs.items():
        column = getattr(models.Reservation, name, None)
        if column:
            reservation_query = reservation_query.filter(column == value)
    return reservation_query.all()


def reservation_create(values):
    values = values.copy()
    reservation = models.Reservation()
    reservation.update(values)

    session = get_session()
    with session.begin():
        try:
            reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=reservation.__class__.__name__, columns=e.columns)

    return reservation_get(reservation.id)


def reservation_update(reservation_id, values):
    session = get_session()

    with session.begin():
        reservation = _reservation_get(session, reservation_id)
        reservation.update(values)
        reservation.save(session=session)

    return reservation_get(reservation_id)


def _reservation_destroy(session, reservation):
    if reservation.instance_reservation:
        reservation.instance_reservation.soft_delete(session=session)

    if reservation.computehost_reservation:
        reservation.computehost_reservation.soft_delete(session=session)

    if reservation.network_reservation:
        reservation.network_reservation.soft_delete(session=session)

    if reservation.floatingip_reservation:
        reservation.floatingip_reservation.soft_delete(session=session)

    if reservation.computehost_allocations:
        for computehost_allocation in reservation.computehost_allocations:
            computehost_allocation.soft_delete(session=session)

    if reservation.network_allocations:
        for network_allocation in reservation.network_allocations:
            network_allocation.soft_delete(session=session)

    if reservation.floatingip_allocations:
        for floatingip_allocation in reservation.floatingip_allocations:
            floatingip_allocation.soft_delete(session=session)

    reservation.soft_delete(session=session)


def reservation_destroy(reservation_id):
    session = get_session()
    with session.begin():
        reservation = _reservation_get(session, reservation_id)

        if not reservation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(id=reservation_id,
                                          model='Reservation')

        _reservation_destroy(session, reservation)


# Lease
def _lease_get(session, lease_id):
    query = model_query(models.Lease, session)
    return query.filter_by(id=lease_id).first()


def lease_get(lease_id):
    return _lease_get(get_session(), lease_id)


def lease_get_all():
    query = model_query(models.Lease, get_session())
    return query.all()


def lease_get_all_by_project(project_id):
    raise NotImplementedError


def lease_get_all_by_user(user_id):
    raise NotImplementedError


def lease_list(project_id=None):
    query = model_query(models.Lease, get_session())
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    return query.all()


def lease_create(values):
    values = values.copy()
    lease = models.Lease()
    reservations = values.pop("reservations", [])
    events = values.pop("events", [])
    lease.update(values)

    session = get_session()
    with session.begin():
        try:
            lease.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=lease.__class__.__name__, columns=e.columns)

        try:
            for r in reservations:
                reservation = models.Reservation()
                reservation.update({"lease_id": lease.id})
                reservation.update(r)
                reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=reservation.__class__.__name__, columns=e.columns)

        try:
            for e in events:
                event = models.Event()
                event.update({"lease_id": lease.id})
                event.update(e)
                event.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=event.__class__.__name__, columns=e.columns)

    return lease_get(lease.id)


def lease_update(lease_id, values):
    session = get_session()

    with session.begin():
        lease = _lease_get(session, lease_id)
        lease.update(values)
        lease.save(session=session)

    return lease_get(lease_id)


def lease_destroy(lease_id):
    session = get_session()
    with session.begin():
        lease = _lease_get(session, lease_id)

        if not lease:
            # raise not found error
            raise db_exc.BlazarDBNotFound(id=lease_id, model='Lease')

        for reservation in lease.reservations:
            _reservation_destroy(session, reservation)

        for event in lease.events:
            event.soft_delete(session=session)

        lease.soft_delete(session=session)


# Event
def _event_get(session, event_id, deleted=False):
    query = model_query(models.Event, session, deleted=deleted)
    return query.filter_by(id=event_id).first()


def _event_get_all(session):
    query = model_query(models.Event, session)
    return query


def event_get(event_id):
    return _event_get(get_session(), event_id)


def event_get_all():
    return _event_get_all(get_session()).all()


def _event_get_sorted_by_filters(sort_key, sort_dir, filters):
    """Return an event query filtered and sorted by name of the field."""

    sort_fn = {'desc': desc, 'asc': asc}

    events_query = _event_get_all(get_session())

    if 'status' in filters:
        events_query = (
            events_query.filter(models.Event.status == filters['status']))
    if 'lease_id' in filters:
        events_query = (
            events_query.filter(models.Event.lease_id == filters['lease_id']))
    if 'event_type' in filters:
        events_query = events_query.filter(models.Event.event_type ==
                                           filters['event_type'])
    if 'time' in filters:
        border = filters['time']['border']
        if filters['time']['op'] == 'lt':
            events_query = events_query.filter(models.Event.time < border)
        elif filters['time']['op'] == 'le':
            events_query = events_query.filter(models.Event.time <= border)
        elif filters['time']['op'] == 'gt':
            events_query = events_query.filter(models.Event.time > border)
        elif filters['time']['op'] == 'ge':
            events_query = events_query.filter(models.Event.time >= border)
        elif filters['time']['op'] == 'eq':
            events_query = events_query.filter(models.Event.time == border)

    events_query = events_query.order_by(
        sort_fn[sort_dir](getattr(models.Event, sort_key))
    )

    return events_query


def event_get_first_sorted_by_filters(sort_key, sort_dir, filters):
    """Return first result for events

    Return the first result for all events matching the filters
    and sorted by name of the field.
    """

    return _event_get_sorted_by_filters(sort_key, sort_dir, filters).first()


def event_get_all_sorted_by_filters(sort_key, sort_dir, filters):
    """Return events filtered and sorted by name of the field."""

    return _event_get_sorted_by_filters(sort_key, sort_dir, filters).all()


def event_create(values):
    values = values.copy()
    event = models.Event()
    event.update(values)

    session = get_session()
    with session.begin():
        try:
            event.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=event.__class__.__name__, columns=e.columns)

    return event_get(event.id)


def event_update(event_id, values):
    session = get_session()

    with session.begin():
        # NOTE(jason): Allow updating soft-deleted events
        event = _event_get(session, event_id, deleted=True)
        event.update(values)
        event.save(session=session)

    return event_get(event_id)


def event_destroy(event_id):
    session = get_session()
    with session.begin():
        event = _event_get(session, event_id)

        if not event:
            # raise not found error
            raise db_exc.BlazarDBNotFound(id=event_id, model='Event')

        event.soft_delete(session=session)


# ComputeHostReservation
def _host_reservation_get(session, host_reservation_id):
    query = model_query(models.ComputeHostReservation, session)
    return query.filter_by(id=host_reservation_id).first()


def host_reservation_get(host_reservation_id):
    return _host_reservation_get(get_session(),
                                 host_reservation_id)


def host_reservation_get_all():
    query = model_query(models.ComputeHostReservation, get_session())
    return query.all()


def _host_reservation_get_by_reservation_id(session, reservation_id):
    query = model_query(models.ComputeHostReservation, session)
    return query.filter_by(reservation_id=reservation_id).first()


def host_reservation_get_by_reservation_id(reservation_id):
    return _host_reservation_get_by_reservation_id(get_session(),
                                                   reservation_id)


def host_reservation_create(values):
    values = values.copy()
    host_reservation = models.ComputeHostReservation()
    host_reservation.update(values)

    session = get_session()
    with session.begin():
        try:
            host_reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=host_reservation.__class__.__name__, columns=e.columns)

    return host_reservation_get(host_reservation.id)


def host_reservation_update(host_reservation_id, values):
    session = get_session()

    with session.begin():
        host_reservation = _host_reservation_get(session,
                                                 host_reservation_id)
        host_reservation.update(values)
        host_reservation.save(session=session)

    return host_reservation_get(host_reservation_id)


def host_reservation_destroy(host_reservation_id):
    session = get_session()
    with session.begin():
        host_reservation = _host_reservation_get(session,
                                                 host_reservation_id)

        if not host_reservation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=host_reservation_id, model='ComputeHostReservation')

        host_reservation.soft_delete(session=session)


# InstanceReservation
def instance_reservation_create(values):
    value = values.copy()
    instance_reservation = models.InstanceReservations()
    instance_reservation.update(value)

    session = get_session()
    with session.begin():
        try:
            instance_reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=instance_reservation.__class__.__name__,
                columns=e.columns)

    return instance_reservation_get(instance_reservation.id)


def instance_reservation_get(instance_reservation_id, session=None):
    if not session:
        session = get_session()
    query = model_query(models.InstanceReservations, session)
    return query.filter_by(id=instance_reservation_id).first()


def instance_reservation_update(instance_reservation_id, values):
    session = get_session()

    with session.begin():
        instance_reservation = instance_reservation_get(
            instance_reservation_id, session)

        if not instance_reservation:
            raise db_exc.BlazarDBNotFound(
                id=instance_reservation_id, model='InstanceReservations')

        instance_reservation.update(values)
        instance_reservation.save(session=session)

    return instance_reservation_get(instance_reservation_id)


def instance_reservation_destroy(instance_reservation_id):
    session = get_session()
    with session.begin():
        instance = instance_reservation_get(instance_reservation_id)

        if not instance:
            raise db_exc.BlazarDBNotFound(
                id=instance_reservation_id, model='InstanceReservations')

        instance.soft_delete(session=session)


# ComputeHostAllocation
def _host_allocation_get(session, host_allocation_id):
    query = model_query(models.ComputeHostAllocation, session)
    return query.filter_by(id=host_allocation_id).first()


def host_allocation_get(host_allocation_id):
    return _host_allocation_get(get_session(),
                                host_allocation_id)


def host_allocation_get_all():
    query = model_query(models.ComputeHostAllocation, get_session())
    return query.all()


def host_allocation_get_all_by_values(**kwargs):
    """Returns all entries filtered by col=value."""
    allocation_query = model_query(models.ComputeHostAllocation, get_session())
    for name, value in kwargs.items():
        column = getattr(models.ComputeHostAllocation, name, None)
        if column:
            allocation_query = allocation_query.filter(column == value)
    return allocation_query.all()


def host_allocation_create(values):
    values = values.copy()
    host_allocation = models.ComputeHostAllocation()
    host_allocation.update(values)

    session = get_session()
    with session.begin():
        try:
            host_allocation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=host_allocation.__class__.__name__, columns=e.columns)

    return host_allocation_get(host_allocation.id)


def host_allocation_update(host_allocation_id, values):
    session = get_session()

    with session.begin():
        host_allocation = _host_allocation_get(session,
                                               host_allocation_id)
        host_allocation.update(values)
        host_allocation.save(session=session)

    return host_allocation_get(host_allocation_id)


def host_allocation_destroy(host_allocation_id):
    session = get_session()
    with session.begin():
        host_allocation = _host_allocation_get(session,
                                               host_allocation_id)

        if not host_allocation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=host_allocation_id, model='ComputeHostAllocation')

        host_allocation.soft_delete(session=session)


# ComputeHost
def _host_get(session, host_id):
    query = model_query(models.ComputeHost, session)
    return query.filter_by(id=host_id).first()


def _host_get_all(session):
    query = model_query(models.ComputeHost, session)
    return query


def host_get(host_id):
    return _host_get(get_session(), host_id)


def host_list():
    return model_query(models.ComputeHost, get_session()).all()


def host_get_all_by_filters(filters):
    """Returns hosts filtered by name of the field."""

    hosts_query = _host_get_all(get_session())

    if 'status' in filters:
        hosts_query = hosts_query.filter(
            models.ComputeHost.status == filters['status'])

    return hosts_query.all()


def host_get_all_by_queries(queries):
    """Returns hosts filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
        http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators

    """
    hosts_query = model_query(models.ComputeHost, get_session())

    oper = {
        '<': ['lt', lambda a, b: a >= b],
        '>': ['gt', lambda a, b: a <= b],
        '<=': ['le', lambda a, b: a > b],
        '>=': ['ge', lambda a, b: a < b],
        '==': ['eq', lambda a, b: a != b],
        '!=': ['ne', lambda a, b: a == b],
    }

    hosts = []
    for query in queries:
        try:
            key, op, value = query.split(' ', 2)
        except ValueError:
            raise db_exc.BlazarDBInvalidFilter(query_filter=query)

        column = getattr(models.ComputeHost, key, None)
        if column is not None:
            if op == 'in':
                filt = column.in_(value.split(','))
            else:
                if op in oper:
                    op = oper[op][0]
                try:
                    attr = [e for e in ['%s', '%s_', '__%s__']
                            if hasattr(column, e % op)][0] % op
                except IndexError:
                    raise db_exc.BlazarDBInvalidFilterOperator(
                        filter_operator=op)

                if value == 'null':
                    value = None

                filt = getattr(column, attr)(value)

            hosts_query = hosts_query.filter(filt)
        else:
            # looking for extra capabilities matches
            extra_filter = (
                _host_extra_capability_query(get_session())
                .filter(models.ExtraCapability.capability_name == key)
            ).all()

            if not extra_filter:
                raise db_exc.BlazarDBNotFound(
                    id=key, model='ComputeHostExtraCapability')

            for host, capability_name in extra_filter:
                if op in oper and oper[op][1](host.capability_value, value):
                    hosts.append(host.computehost_id)
                elif op not in oper:
                    msg = 'Operator %s for extra capabilities not implemented'
                    raise NotImplementedError(msg % op)

            # We must also avoid selecting any host which doesn't have the
            # extra capability present.
            all_hosts = [h.id for h in hosts_query.all()]
            extra_filter_hosts = [h.computehost_id for h, _ in extra_filter]
            hosts += [h for h in all_hosts if h not in extra_filter_hosts]

    return hosts_query.filter(~models.ComputeHost.id.in_(hosts)).all()


def reservable_host_get_all_by_queries(queries):
    """Returns reservable hosts filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
        http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators

    """
    queries.append('reservable == 1')
    return host_get_all_by_queries(queries)


def unreservable_host_get_all_by_queries(queries):
    """Returns unreservable hosts filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
        http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators

    """

    # TODO(hiro-kobayashi): support the expression 'reservable == False'
    queries.append('reservable == 0')
    return host_get_all_by_queries(queries)


def host_create(values):
    values = values.copy()
    host = models.ComputeHost()
    host.update(values)

    session = get_session()
    with session.begin():
        try:
            host.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=host.__class__.__name__, columns=e.columns)

    return host_get(host.id)


def host_update(host_id, values):
    session = get_session()

    with session.begin():
        host = _host_get(session, host_id)
        host.update(values)
        host.save(session=session)

    return host_get(host_id)


def host_destroy(host_id):
    session = get_session()
    with session.begin():
        host = _host_get(session, host_id)

        if not host:
            # raise not found error
            raise db_exc.BlazarDBNotFound(id=host_id, model='Host')

        host.soft_delete(session=session)
        # Also delete this host's extra capabilities
        for capability in host.computehost_extra_capabilities:
            capability.soft_delete(session=session)


# ComputeHostExtraCapability

def _host_extra_capability_query(session):
    return (
        model_query(models.ComputeHostExtraCapability, session)
        .join(models.ExtraCapability)
        .add_column(models.ExtraCapability.capability_name))


def _host_extra_capability_get(session, host_extra_capability_id):
    query = _host_extra_capability_query(session).filter(
        models.ComputeHostExtraCapability.id == host_extra_capability_id)

    return query.first()


def host_extra_capability_get(host_extra_capability_id):
    return _host_extra_capability_get(get_session(),
                                      host_extra_capability_id)


def _host_extra_capability_get_all_per_host(session, host_id):
    query = _host_extra_capability_query(session).filter(
        models.ComputeHostExtraCapability.computehost_id == host_id)

    return query


def host_extra_capability_get_all_per_host(host_id):
    return _host_extra_capability_get_all_per_host(get_session(),
                                                   host_id).all()


def host_extra_capability_create(values):
    values = values.copy()

    resource_property = resource_property_get_or_create(
        'physical:host', values.get('capability_name'))

    del values['capability_name']
    values['capability_id'] = resource_property.id

    host_extra_capability = models.ComputeHostExtraCapability()
    host_extra_capability.update(values)

    session = get_session()

    with session.begin():
        try:
            host_extra_capability.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=host_extra_capability.__class__.__name__,
                columns=e.columns)

    return host_extra_capability_get(host_extra_capability.id)


def host_extra_capability_update(host_extra_capability_id, values):
    session = get_session()

    with session.begin():
        host_extra_capability, _ = (
            _host_extra_capability_get(session,
                                       host_extra_capability_id))
        host_extra_capability.update(values)
        host_extra_capability.save(session=session)

    return host_extra_capability_get(host_extra_capability_id)


def host_extra_capability_destroy(host_extra_capability_id):
    session = get_session()
    with session.begin():
        host_extra_capability = _host_extra_capability_get(
            session, host_extra_capability_id)

        if not host_extra_capability:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=host_extra_capability_id,
                model='ComputeHostExtraCapability')

        host_extra_capability[0].soft_delete(session=session)


def host_extra_capability_get_all_per_name(host_id, capability_name):
    session = get_session()

    with session.begin():
        query = _host_extra_capability_get_all_per_host(session, host_id)
        return query.filter(
            models.ExtraCapability.capability_name == capability_name).all()


# FloatingIP reservation

def fip_reservation_create(fip_reservation_values):
    values = fip_reservation_values.copy()
    fip_reservation = models.FloatingIPReservation()
    fip_reservation.update(values)

    session = get_session()
    with session.begin():
        try:
            fip_reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=fip_reservation.__class__.__name__, columns=e.columns)

    return fip_reservation_get(fip_reservation.id)


def _fip_reservation_get(session, fip_reservation_id):
    query = model_query(models.FloatingIPReservation, session)
    return query.filter_by(id=fip_reservation_id).first()


def fip_reservation_get(fip_reservation_id):
    return _fip_reservation_get(get_session(), fip_reservation_id)


def fip_reservation_update(fip_reservation_id, fip_reservation_values):
    session = get_session()

    with session.begin():
        fip_reservation = _fip_reservation_get(session, fip_reservation_id)
        fip_reservation.update(fip_reservation_values)
        fip_reservation.save(session=session)

    return fip_reservation_get(fip_reservation_id)


def fip_reservation_destroy(fip_reservation_id):
    session = get_session()
    with session.begin():
        fip_reservation = _fip_reservation_get(session, fip_reservation_id)

        if not fip_reservation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=fip_reservation_id, model='FloatingIPReservation')

        fip_reservation.soft_delete(session=session)
        session.delete(fip_reservation)


# Required FIP

def required_fip_create(required_fip_values):
    values = required_fip_values.copy()
    required_fip = models.RequiredFloatingIP()
    required_fip.update(values)

    session = get_session()
    with session.begin():
        try:
            required_fip.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=required_fip.__class__.__name__, columns=e.columns)

    return required_fip_get(required_fip.id)


def _required_fip_get(session, required_fip_id):
    query = model_query(models.RequiredFloatingIP, session)
    return query.filter_by(id=required_fip_id).first()


def required_fip_get(required_fip_id):
    return _required_fip_get(get_session(), required_fip_id)


def required_fip_update(required_fip_id, required_fip_values):
    session = get_session()

    with session.begin():
        required_fip = _required_fip_get(session, required_fip_id)
        required_fip.update(required_fip_values)
        required_fip.save(session=session)

    return required_fip_get(required_fip_id)


def required_fip_destroy(required_fip_id):
    session = get_session()
    with session.begin():
        required_fip = _required_fip_get(session, required_fip_id)

        if not required_fip:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=required_fip_id, model='RequiredFloatingIP')

        required_fip.soft_delete(session=session)
        session.delete(required_fip)


def required_fip_destroy_by_fip_reservation_id(fip_reservation_id):
    session = get_session()
    with session.begin():
        required_fips = model_query(
            models.RequiredFloatingIP, session).filter_by(
            floatingip_reservation_id=fip_reservation_id)
        for required_fip in required_fips:
            required_fip_destroy(required_fip['id'])


# FloatingIP Allocation

def _fip_allocation_get(session, fip_allocation_id):
    query = model_query(models.FloatingIPAllocation, session)
    return query.filter_by(id=fip_allocation_id).first()


def fip_allocation_get(fip_allocation_id):
    return _fip_allocation_get(get_session(), fip_allocation_id)


def fip_allocation_create(allocation_values):
    values = allocation_values.copy()
    fip_allocation = models.FloatingIPAllocation()
    fip_allocation.update(values)

    session = get_session()
    with session.begin():
        try:
            fip_allocation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=fip_allocation.__class__.__name__, columns=e.columns)

    return fip_allocation_get(fip_allocation.id)


def fip_allocation_get_all_by_values(**kwargs):
    """Returns all entries filtered by col=value."""
    allocation_query = model_query(models.FloatingIPAllocation, get_session())
    for name, value in kwargs.items():
        column = getattr(models.FloatingIPAllocation, name, None)
        if column:
            allocation_query = allocation_query.filter(column == value)
    return allocation_query.all()


def fip_allocation_destroy(allocation_id):
    session = get_session()
    with session.begin():
        fip_allocation = _fip_allocation_get(session, allocation_id)

        if not fip_allocation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=allocation_id, model='FloatingIPAllocation')

        fip_allocation.soft_delete(session=session)
        session.delete(fip_allocation)


def fip_allocation_update(allocation_id, allocation_values):
    session = get_session()

    with session.begin():
        fip_allocation = _fip_allocation_get(session, allocation_id)
        fip_allocation.update(allocation_values)
        fip_allocation.save(session=session)

    return fip_allocation_get(allocation_id)


# Floating IP
def _floatingip_get(session, floatingip_id):
    query = model_query(models.FloatingIP, session)
    return query.filter_by(id=floatingip_id).first()


def _floatingip_get_all(session):
    query = model_query(models.FloatingIP, session)
    return query


def fip_get_all_by_queries(queries):
    """Returns Floating IPs filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
        http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators

    """
    fips_query = model_query(models.FloatingIP, get_session())

    oper = {
        '<': ['lt', lambda a, b: a >= b],
        '>': ['gt', lambda a, b: a <= b],
        '<=': ['le', lambda a, b: a > b],
        '>=': ['ge', lambda a, b: a < b],
        '==': ['eq', lambda a, b: a != b],
        '!=': ['ne', lambda a, b: a == b],
    }

    for query in queries:
        try:
            key, op, value = query.split(' ', 2)
        except ValueError:
            raise db_exc.BlazarDBInvalidFilter(query_filter=query)

        column = getattr(models.FloatingIP, key, None)
        if column is not None:
            if op == 'in':
                filt = column.in_(value.split(','))
            else:
                if op in oper:
                    op = oper[op][0]
                try:
                    attr = [e for e in ['%s', '%s_', '__%s__']
                            if hasattr(column, e % op)][0] % op
                except IndexError:
                    raise db_exc.BlazarDBInvalidFilterOperator(
                        filter_operator=op)

                if value == 'null':
                    value = None

                filt = getattr(column, attr)(value)

            fips_query = fips_query.filter(filt)
        else:
            raise db_exc.BlazarDBInvalidFilter(query_filter=query)

    return fips_query.all()


def reservable_fip_get_all_by_queries(queries):
    """Returns reservable fips filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
        http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators

    """
    queries.append('reservable == 1')
    return fip_get_all_by_queries(queries)


def floatingip_get(floatingip_id):
    return _floatingip_get(get_session(), floatingip_id)


def floatingip_list():
    return model_query(models.FloatingIP, get_session()).all()


def floatingip_create(values):
    values = values.copy()
    floatingip = models.FloatingIP()
    floatingip.update(values)

    session = get_session()
    with session.begin():
        try:
            floatingip.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=floatingip.__class__.__name__, columns=e.columns)

    return floatingip_get(floatingip.id)


def floatingip_destroy(floatingip_id):
    session = get_session()
    with session.begin():
        floatingip = _floatingip_get(session, floatingip_id)

        if not floatingip:
            # raise not found error
            raise db_exc.BlazarDBNotFound(id=floatingip_id, model='FloatingIP')

        floatingip.soft_delete(session=session)


# Networks

def _network_get(session, network_id):
    query = model_query(models.NetworkSegment, session)
    return query.filter_by(id=network_id).first()


def _network_get_all(session):
    query = model_query(models.NetworkSegment, session)
    return query


def network_get(network_id):
    return _network_get(get_session(), network_id)


def network_list():
    return model_query(models.NetworkSegment, get_session()).all()


def network_create(values):
    values = values.copy()
    network = models.NetworkSegment()
    network.update(values)

    session = get_session()
    with session.begin():
        try:
            network.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=network.__class__.__name__, columns=e.columns)

    return network_get(network.id)


def network_update(network_id, values):
    session = get_session()

    with session.begin():
        network = _network_get(session, network_id)
        network.update(values)
        network.save(session=session)

    return network_get(network_id)


def network_destroy(network_id):
    capability_ids = []
    session = get_session()
    with session.begin():
        network = _network_get(session, network_id)

        if not network:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=network_id, model='Network segment')

        network.soft_delete(session=session)

        # Also delete this network's extra capabilities
        # TODO raises exception (soft delete does not work)
        #for capability in network_extra_capability_get_all_per_network(network_id):
        #    capability.soft_delete(session=session)

# NetworkAllocation

def _network_allocation_get(session, network_allocation_id):
    query = model_query(models.NetworkAllocation, session)
    return query.filter_by(id=network_allocation_id).first()


def network_allocation_get(network_allocation_id):
    return _network_allocation_get(get_session(),
                                   network_allocation_id)


def network_allocation_get_all():
    query = model_query(models.NetworkAllocation, get_session())
    return query.all()


def network_allocation_create(values):
    values = values.copy()
    network_allocation = models.NetworkAllocation()
    network_allocation.update(values)

    session = get_session()
    with session.begin():
        try:
            network_allocation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=network_allocation.__class__.__name__, columns=e.columns)

    return network_allocation_get(network_allocation.id)


def network_allocation_get_all_by_values(**kwargs):
    """Returns all entries filtered by col=value."""
    allocation_query = model_query(models.NetworkAllocation, get_session())
    for name, value in kwargs.items():
        column = getattr(models.NetworkAllocation, name, None)
        if column:
            allocation_query = allocation_query.filter(column == value)
    return allocation_query.all()


def network_allocation_destroy(network_allocation_id):
    session = get_session()
    with session.begin():
        network_allocation = _network_allocation_get(session,
                                                     network_allocation_id)

        if not network_allocation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=network_allocation_id, model='NetworkAllocation')

        network_allocation.soft_delete(session=session)


# NetworkReservation

def network_reservation_create(values):
    value = values.copy()
    network_reservation = models.NetworkReservation()
    network_reservation.update(value)

    session = get_session()
    with session.begin():
        try:
            network_reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=network_reservation.__class__.__name__,
                columns=e.columns)

    return network_reservation_get(network_reservation.id)


def network_reservation_get(network_reservation_id, session=None):
    if not session:
        session = get_session()
    query = model_query(models.NetworkReservation, session)
    return query.filter_by(id=network_reservation_id).first()


def network_reservation_update(network_reservation_id, values):
    session = get_session()

    with session.begin():
        network_reservation = network_reservation_get(
            network_reservation_id, session)

        if not network_reservation:
            raise db_exc.BlazarDBNotFound(
                id=network_reservation_id, model='NetworkReservation')

        network_reservation.update(values)
        network_reservation.save(session=session)

    return network_reservation_get(network_reservation_id)


def network_reservation_destroy(network_reservation_id):
    session = get_session()
    with session.begin():
        network = network_reservation_get(network_reservation_id)

        if not network:
            raise db_exc.BlazarDBNotFound(
                id=network_reservation_id, model='NetworkReservation')

        network.soft_delete(session=session)


def network_get_all_by_filters(filters):
    """Returns networks filtered by name of the field."""

    networks_query = _network_get_all(get_session())

    if 'status' in filters:
        networks_query = networks_query.filter(
            models.NetworkSegment.status == filters['status'])

    return networks_query.all()


def network_get_all_by_queries(queries):
    """Return networks filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
    http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators
    """
    networks_query = model_query(models.NetworkSegment, get_session())

    oper = {
        '<': ['lt', lambda a, b: a >= b],
        '>': ['gt', lambda a, b: a <= b],
        '<=': ['le', lambda a, b: a > b],
        '>=': ['ge', lambda a, b: a < b],
        '==': ['eq', lambda a, b: a != b],
        '!=': ['ne', lambda a, b: a == b],
    }

    networks = []
    for query in queries:
        try:
            key, op, value = query.split(' ', 2)
        except ValueError:
            raise db_exc.BlazarDBInvalidFilter(query_filter=query)

        column = getattr(models.NetworkSegment, key, None)
        if column is not None:
            if op == 'in':
                filt = column.in_(value.split(','))
            else:
                if op in oper:
                    op = oper[op][0]
                try:
                    attr = [e for e in ['%s', '%s_', '__%s__']
                            if hasattr(column, e % op)][0] % op
                except IndexError:
                    raise db_exc.BlazarDBInvalidFilterOperator(
                        filter_operator=op)

                if value == 'null':
                    value = None

                filt = getattr(column, attr)(value)

            networks_query = networks_query.filter(filt)
        else:
            # looking for extra capabilities matches
            extra_filter = (
                _network_extra_capability_query(get_session())
                .filter(models.ExtraCapability.capability_name == key)
            ).all()
            if not extra_filter:
                raise db_exc.BlazarDBNotFound(
                    id=key, model='NetworkSegmentExtraCapability')
            for network, capability_name in extra_filter:
                if op in oper and oper[op][1](network.capability_value, value):
                    networks.append(network.network_id)
                elif op not in oper:
                    msg = 'Operator %s for extra capabilities not implemented'
                    raise NotImplementedError(msg % op)

            # We must also avoid selecting any network which doesn't have the
            # extra capability present.
            all_networks = [h.id for h in networks_query.all()]
            extra_filter_networks = [h.network_id for h, _ in extra_filter]
            networks += [h for h in all_networks if h not in
                         extra_filter_networks]

    return networks_query.filter(~models.NetworkSegment.id.in_(networks)).all()


def reservable_network_get_all_by_queries(queries):
    """Return reservable networks filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
    http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
        #sqlalchemy.sql.operators.ColumnOperators
    """
    queries.append('reservable == 1')
    return network_get_all_by_queries(queries)


def unreservable_network_get_all_by_queries(queries):
    """Return unreservable networks filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
    http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
        #sqlalchemy.sql.operators.ColumnOperators
    """
    # TODO(hiro-kobayashi): support the expression 'reservable == False'
    queries.append('reservable == 0')
    return network_get_all_by_queries(queries)


# NetworkSegmentExtraCapability

def _network_extra_capability_query(session):
    return (
        model_query(models.NetworkSegmentExtraCapability, session)
        .join(models.ExtraCapability)
        .add_column(models.ExtraCapability.capability_name))


def _network_extra_capability_get(session, network_extra_capability_id):
    query = _network_extra_capability_query(session).filter(
        models.NetworkSegmentExtraCapability.id == network_extra_capability_id)

    return query.first()


def network_extra_capability_get(network_extra_capability_id):
    return _network_extra_capability_get(get_session(),
                                         network_extra_capability_id)


def _network_extra_capability_get_all_per_network(session, network_id):
    query = _network_extra_capability_query(session).filter(
        models.NetworkSegmentExtraCapability.network_id == network_id)

    return query


def network_extra_capability_get_all_per_network(network_id):
    return _network_extra_capability_get_all_per_network(get_session(),
                                                         network_id).all()


def network_extra_capability_create(values):
    values = values.copy()

    resource_property = _resource_property_get_or_create(
        get_session(), 'network', values.get('capability_name'))

    del values['capability_name']
    values['capability_id'] = resource_property.id

    network_extra_capability = models.NetworkSegmentExtraCapability()
    network_extra_capability.update(values)

    session = get_session()

    with session.begin():
        try:
            network_extra_capability.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=network_extra_capability.__class__.__name__,
                columns=e.columns)

    return network_extra_capability_get(network_extra_capability.id)


def network_extra_capability_update(network_extra_capability_id, values):
    session = get_session()

    with session.begin():
        network_extra_capability, _ = (
            _network_extra_capability_get(session,
                                          network_extra_capability_id))
        network_extra_capability.update(values)
        network_extra_capability.save(session=session)

    return network_extra_capability_get(network_extra_capability_id)


def network_extra_capability_destroy(network_extra_capability_id):
    session = get_session()
    with session.begin():
        network_extra_capability = _network_extra_capability_get(
            session, network_extra_capability_id)

        if not network_extra_capability:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=network_extra_capability_id,
                model='NetworkSegmentExtraCapability')

        network_extra_capability[0].soft_delete(session=session)


def network_extra_capability_get_all_per_name(network_id, capability_name):
    session = get_session()

    with session.begin():
        query = _network_extra_capability_get_all_per_network(
            session, network_id)
        return query.filter_by(capability_name=capability_name).all()


def network_extra_capability_get_latest_per_name(network_id, capability_name):
    session = get_session()

    with session.begin():
        query = _network_extra_capability_get_all_per_network(session,
                                                              network_id)
        return (
            query
            .filter(models.ExtraCapability.capability_name == capability_name)
            .order_by(models.NetworkSegmentExtraCapability.created_at.desc())
            .first())

# Devices


def _device_get(session, device_id):
    query = model_query(models.Device, session)
    return query.filter_by(id=device_id).first()


def _device_get_all(session):
    query = model_query(models.Device, session)
    return query


def device_get(device_id):
    return _device_get(get_session(), device_id)


def device_list():
    return model_query(models.Device, get_session()).all()


def device_create(values):
    values = values.copy()
    device = models.Device()
    device.update(values)

    session = get_session()
    with session.begin():
        try:
            device.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=device.__class__.__name__, columns=e.columns)

    return device_get(device.id)


def device_update(device_id, values):
    session = get_session()

    with session.begin():
        device = _device_get(session, device_id)
        device.update(values)
        device.save(session=session)

    return device_get(device_id)


def device_destroy(device_id):
    session = get_session()
    with session.begin():
        device = _device_get(session, device_id)

        if not device:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=device_id, model='Device')

        device.soft_delete(session=session)

        # Also delete this device's extra capabilities
        for capability in device_extra_capability_get_all_per_device(device_id):
            capability.soft_delete(session=session)


# DeviceAllocation

def _device_allocation_get(session, device_allocation_id):
    query = model_query(models.DeviceAllocation, session)
    return query.filter_by(id=device_allocation_id).first()


def device_allocation_get(device_allocation_id):
    return _device_allocation_get(get_session(),
                                  device_allocation_id)


def device_allocation_get_all():
    query = model_query(models.DeviceAllocation, get_session())
    return query.all()


def device_allocation_create(values):
    values = values.copy()
    device_allocation = models.DeviceAllocation()
    device_allocation.update(values)

    session = get_session()
    with session.begin():
        try:
            device_allocation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=device_allocation.__class__.__name__, columns=e.columns)

    return device_allocation_get(device_allocation.id)


def device_allocation_get_all_by_values(**kwargs):
    """Returns all entries filtered by col=value."""
    allocation_query = model_query(models.DeviceAllocation, get_session())
    for name, value in kwargs.items():
        column = getattr(models.DeviceAllocation, name, None)
        if column:
            allocation_query = allocation_query.filter(column == value)
    return allocation_query.all()


def device_allocation_update(device_allocation_id, values):
    session = get_session()

    with session.begin():
        device_allocation = _device_allocation_get(session,
                                                   device_allocation_id)
        device_allocation.update(values)
        device_allocation.save(session=session)

    return device_allocation_get(device_allocation_id)


def device_allocation_destroy(device_allocation_id):
    session = get_session()
    with session.begin():
        device_allocation = _device_allocation_get(session,
                                                   device_allocation_id)

        if not device_allocation:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=device_allocation_id, model='DeviceAllocation')

        device_allocation.soft_delete(session=session)


# DeviceReservation

def device_reservation_create(values):
    value = values.copy()
    device_reservation = models.DeviceReservation()
    device_reservation.update(value)

    session = get_session()
    with session.begin():
        try:
            device_reservation.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=device_reservation.__class__.__name__,
                columns=e.columns)

    return device_reservation_get(device_reservation.id)


def device_reservation_get(device_reservation_id, session=None):
    if not session:
        session = get_session()
    query = model_query(models.DeviceReservation, session)
    return query.filter_by(id=device_reservation_id).first()


def device_reservation_update(device_reservation_id, values):
    session = get_session()

    with session.begin():
        device_reservation = device_reservation_get(
            device_reservation_id, session)

        if not device_reservation:
            raise db_exc.BlazarDBNotFound(
                id=device_reservation_id, model='DeviceReservation')

        device_reservation.update(values)
        device_reservation.save(session=session)

    return device_reservation_get(device_reservation_id)


def device_reservation_destroy(device_reservation_id):
    session = get_session()
    with session.begin():
        device = device_reservation_get(device_reservation_id)

        if not device:
            raise db_exc.BlazarDBNotFound(
                id=device_reservation_id, model='DeviceReservation')

        device.soft_delete(session=session)


def device_get_all_by_filters(filters):
    """Returns devices filtered by name of the field."""

    devices_query = _device_get_all(get_session())

    if 'status' in filters:
        devices_query = devices_query.filter(
            models.Device.status == filters['status'])

    return devices_query.all()


def device_get_all_by_queries(queries):
    """Return devices filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
    http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
            #sqlalchemy.sql.operators.ColumnOperators
    """
    devices_query = model_query(models.Device, get_session())

    oper = {
        '<': ['lt', lambda a, b: a >= b],
        '>': ['gt', lambda a, b: a <= b],
        '<=': ['le', lambda a, b: a > b],
        '>=': ['ge', lambda a, b: a < b],
        '==': ['eq', lambda a, b: a != b],
        '!=': ['ne', lambda a, b: a == b],
    }

    devices = []
    for query in queries:
        try:
            key, op, value = query.split(' ', 2)
        except ValueError:
            raise db_exc.BlazarDBInvalidFilter(query_filter=query)

        column = getattr(models.Device, key, None)
        if column is not None:
            if op == 'in':
                filt = column.in_(value.split(','))
            else:
                if op in oper:
                    op = oper[op][0]
                try:
                    attr = [e for e in ['%s', '%s_', '__%s__']
                            if hasattr(column, e % op)][0] % op
                except IndexError:
                    raise db_exc.BlazarDBInvalidFilterOperator(
                        filter_operator=op)

                if value == 'null':
                    value = None

                filt = getattr(column, attr)(value)

            devices_query = devices_query.filter(filt)
        else:
            # looking for extra capabilities matches
            extra_filter = (
                _device_extra_capability_query(get_session())
                .filter(models.ExtraCapability.capability_name == key)
            ).all()

            if not extra_filter:
                raise db_exc.BlazarDBNotFound(
                    id=key, model='DeviceExtraCapability')

            for device, capability_name in extra_filter:
                if op in oper and oper[op][1](device.capability_value, value):
                    devices.append(device.device_id)
                elif op not in oper:
                    msg = 'Operator %s for extra capabilities not implemented'
                    raise NotImplementedError(msg % op)

            # We must also avoid selecting any device which doesn't have the
            # extra capability present.
            all_devices = [h.id for h in devices_query.all()]
            extra_filter_devices = [h.device_id for h, _ in extra_filter]
            devices += [h for h in all_devices if h not in
                        extra_filter_devices]

    return devices_query.filter(~models.Device.id.in_(devices)).all()


def reservable_device_get_all_by_queries(queries):
    """Return reservable devices filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
    http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
        #sqlalchemy.sql.operators.ColumnOperators
    """
    queries.append('reservable == 1')
    return device_get_all_by_queries(queries)


def unreservable_device_get_all_by_queries(queries):
    """Return unreservable devices filtered by an array of queries.

    :param queries: array of queries "key op value" where op can be
    http://docs.sqlalchemy.org/en/rel_0_7/core/expression_api.html
        #sqlalchemy.sql.operators.ColumnOperators
    """
    # TODO(hiro-kobayashi): support the expression 'reservable == False'
    queries.append('reservable == 0')
    return device_get_all_by_queries(queries)


# DeviceExtraCapability

def _device_extra_capability_query(session):
    return (
        model_query(models.DeviceExtraCapability, session)
        .join(models.ExtraCapability)
        .add_column(models.ExtraCapability.capability_name))


def _device_extra_capability_get(session, device_extra_capability_id):
    query = _device_extra_capability_query(session).filter(
        models.DeviceExtraCapability.id == device_extra_capability_id)

    return query.first()


def device_extra_capability_get(device_extra_capability_id):
    return _device_extra_capability_get(get_session(),
                                        device_extra_capability_id)


def _device_extra_capability_get_all_per_device(session, device_id):
    query = _device_extra_capability_query(session).filter(
        models.DeviceExtraCapability.device_id == device_id)

    return query


def device_extra_capability_get_all_per_device(device_id):
    return _device_extra_capability_get_all_per_device(get_session(),
                                                       device_id).all()


def device_extra_capability_create(values):
    values = values.copy()

    resource_property = _resource_property_get_or_create(
        get_session(), 'device', values.get('capability_name'))

    del values['capability_name']
    values['capability_id'] = resource_property.id

    device_extra_capability = models.DeviceExtraCapability()
    device_extra_capability.update(values)

    session = get_session()

    with session.begin():
        try:
            device_extra_capability.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=device_extra_capability.__class__.__name__,
                columns=e.columns)

    return device_extra_capability_get(device_extra_capability.id)


def device_extra_capability_update(device_extra_capability_id, values):
    session = get_session()

    with session.begin():
        device_extra_capability, _ = (
            _device_extra_capability_get(session,
                                         device_extra_capability_id))
        device_extra_capability.update(values)
        device_extra_capability.save(session=session)

    return device_extra_capability_get(device_extra_capability_id)


def device_extra_capability_destroy(device_extra_capability_id):
    session = get_session()
    with session.begin():
        device_extra_capability = _device_extra_capability_get(
            session, device_extra_capability_id)

        if not device_extra_capability:
            # raise not found error
            raise db_exc.BlazarDBNotFound(
                id=device_extra_capability_id,
                model='DeviceExtraCapability')

        device_extra_capability[0].soft_delete(session=session)


def device_extra_capability_get_all_per_name(device_id, capability_name):
    session = get_session()

    with session.begin():
        query = _device_extra_capability_get_all_per_device(
            session, device_id)
        return query.filter_by(capability_name=capability_name).all()


def device_extra_capability_get_latest_per_name(device_id, capability_name):
    session = get_session()

    with session.begin():
        query = _device_extra_capability_get_all_per_device(session,
                                                            device_id)
        return (
            query
            .filter(models.ExtraCapability.capability_name == capability_name)
            .order_by(models.DeviceExtraCapability.created_at.desc())
            .first())

# Resource Properties


def _resource_property_get(session, resource_type, capability_name):
    query = (
        model_query(models.ExtraCapability, session)
        .filter_by(resource_type=resource_type)
        .filter_by(capability_name=capability_name))

    return query.first()


def resource_property_get(resource_type, capability_name):
    return _resource_property_get(get_session(), resource_type,
                                  capability_name)


def resource_properties_list(resource_type):
    if resource_type not in EXTRA_CAPABILITY_MODELS:
        raise db_exc.BlazarDBExtraCapabilitiesNotEnabled(
            resource_type=resource_type)

    session = get_session()

    with session.begin():

        resource_model = EXTRA_CAPABILITY_MODELS[resource_type]
        query = _read_deleted_filter(session.query(
            models.ExtraCapability.capability_name,
            models.ExtraCapability.private,
            resource_model.capability_value,
        ).join(resource_model), resource_model, deleted=False).distinct()

        return query.all()


def _resource_property_create(session, values):
    values = values.copy()

    resource_property = models.ExtraCapability()
    resource_property.update(values)

    with session.begin():
        try:
            resource_property.save(session=session)
        except common_db_exc.DBDuplicateEntry as e:
            # raise exception about duplicated columns (e.columns)
            raise db_exc.BlazarDBDuplicateEntry(
                model=resource_property.__class__.__name__,
                columns=e.columns)

    return resource_property_get(values.get('resource_type'),
                                 values.get('capability_name'))


def resource_property_create(values):
    return _resource_property_create(get_session(), values)


def resource_property_update(resource_type, property_name, values):
    if resource_type not in EXTRA_CAPABILITY_MODELS:
        raise db_exc.BlazarDBExtraCapabilitiesNotEnabled(
            resource_type=resource_type)

    values = values.copy()
    session = get_session()

    with session.begin():
        resource_property = _resource_property_get(
            session, resource_type, property_name)

        if not resource_property:
            raise db_exc.BlazarDBInvalidExtraCapability(
                property_name=property_name,
                resource_type=resource_type)

        resource_property.update(values)
        resource_property.save(session=session)

    return resource_property_get(resource_type, property_name)


def _resource_property_get_or_create(session, resource_type, capability_name):
    if capability_name in FORBIDDEN_EXTRA_CAPABILITY_NAMES:
        raise db_exc.BlazarDBForbiddenExtraCapability(
            property_name=capability_name)

    resource_property = _resource_property_get(
        session, resource_type, capability_name)

    if resource_property:
        return resource_property
    else:
        rp_values = {
            'resource_type': resource_type,
            'capability_name': capability_name}

        return resource_property_create(rp_values)


def resource_property_get_or_create(resource_type, capability_name):
    return _resource_property_get_or_create(
        get_session(), resource_type, capability_name)
