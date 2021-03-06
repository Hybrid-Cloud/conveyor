# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Piston Cloud Computing, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
SQLAlchemy models for conveyor data.
"""

import six
import uuid

from oslo_db.sqlalchemy import models
from oslo_utils import timeutils
import sqlalchemy
from sqlalchemy import Column, Index, Integer, String
from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref
from sqlalchemy.orm import relationship
from sqlalchemy.orm import session as orm_session

from conveyor.db.sqlalchemy import types

BASE = declarative_base()


def MediumText():
    return Text().with_variant(MEDIUMTEXT(), 'mysql')


class ConveyorBase(models.SoftDeleteMixin,
                   models.TimestampMixin,
                   models.ModelBase):
    metadata = None

    # TODO(ekudryashova): remove this after both conveyor and oslo.db
    # will use oslo.utils library
    # NOTE: Both projects(conveyor and oslo.db) use `timeutils.utcnow`, which
    # returns specified time(if override_time is set). Time overriding is
    # only used by unit tests, but in a lot of places, temporarily overriding
    # this columns helps to avoid lots of calls of timeutils.set_override
    # from different places in unit tests.
    created_at = Column(DateTime, default=lambda: timeutils.utcnow())
    updated_at = Column(DateTime, onupdate=lambda: timeutils.utcnow())

    def save(self, session=None):
        from conveyor.db.sqlalchemy import api

        if session is None:
            session = api.get_session()

        super(ConveyorBase, self).save(session=session)

    def expire(self, session=None, attrs=None):
        """Expire this object ()."""
        if not session:
            session = get_session()
        session.expire(self, attrs)

    def refresh(self, session=None, attrs=None):
        """Refresh this object."""
        if not session:
            session = get_session()
        session.refresh(self, attrs)

    def delete(self, session=None):
        """Delete this object."""
        if not session:
            session = get_session()
        session.begin(subtransactions=True)
        session.delete(self)
        session.commit()

    def update_and_save(self, values, session=None):
        if not session:
            session = get_session()
        session.begin(subtransactions=True)
        for k, v in six.iteritems(values):
            setattr(self, k, v)
        session.commit()


class CopyBase(models.TimestampMixin,
               models.ModelBase):
    metadata = None

    # TODO(ekudryashova): remove this after both conveyor and oslo.db
    # will use oslo.utils library
    # NOTE: Both projects(conveyor and oslo.db) use `timeutils.utcnow`, which
    # returns specified time(if override_time is set). Time overriding is
    # only used by unit tests, but in a lot of places, temporarily overriding
    # this columns helps to avoid lots of calls of timeutils.set_override
    # from different places in unit tests.
    created_at = Column(DateTime, default=lambda: timeutils.utcnow())
    updated_at = Column(DateTime, onupdate=lambda: timeutils.utcnow())

    # def save(self, session=None):
    #     from conveyor.db.sqlalchemy import api
    #
    #     if session is None:
    #         session = api.get_session()
    #     session.begin(subtransactions=True)
    #     super(CopyBase, self).save(session=session)
    #     session.commit()

    def expire(self, session=None, attrs=None):
        """Expire this object ()."""
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.expire(self, attrs)

    def refresh(self, session=None, attrs=None):
        """Refresh this object."""
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.begin(subtransactions=True)
        session.refresh(self, attrs)
        session.commit()

    def delete(self, session=None):
        """Delete this object."""
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.begin(subtransactions=True)
        session.delete(self)
        session.commit()

    def update_and_save(self, values, session=None):
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
                # from conveyor.db.sqlalchemy import api as db_api
                # session = db_api.get_session_heat()
        session.begin(subtransactions=True)
        for k, v in six.iteritems(values):
            setattr(self, k, v)
        super(CopyBase, self).save(session=session)
        # session.update(self)
        session.commit()


class Plan(BASE, ConveyorBase):
    """Represents a plan."""
    __tablename__ = "plans"
    __table_args__ = (
        Index('plan_id', 'plan_id', unique=True),
    )

    id = Column(Integer, primary_key=True)
    plan_id = Column(String(length=36), nullable=False)
    plan_name = Column(String(length=255), nullable=True)
    project_id = Column(String(length=36), nullable=False)
    user_id = Column(String(length=36), nullable=False)
    task_status = Column(String(length=255))
    plan_status = Column(String(length=255))
    plan_type = Column(String(length=255))
    clone_resources = Column(types.Json)
    stack_id = Column(String(length=36))


class PlanTemplate(BASE, ConveyorBase):
    """Represents an unparsed template which should be in JSON format."""

    __tablename__ = 'plan_template'
    id = Column(Integer, primary_key=True)
    plan_id = Column(String(length=36), nullable=False)
    template = Column(types.Json)


class PlanClonedResources(BASE, ConveyorBase):
    """Represents an unparsed template which should be in JSON format."""

    __tablename__ = 'plan_cloned_resources'
    id = Column(Integer, primary_key=True)
    plan_id = Column(String(length=36), nullable=False)
    destination = Column(String(length=36), nullable=False)
    relation = Column(types.Json)
    dependencies = Column(types.Json)


class PlanAavailabilityZoneMapper(BASE, ConveyorBase):
    """Represents an unparsed template which should be in JSON format."""

    __tablename__ = 'plan_availability_zone_mapper'
    id = Column(Integer, primary_key=True)
    plan_id = Column(String(length=36), nullable=False)
    az_mapper = Column(types.Json)


class ConveyorConfig(BASE, ConveyorBase):
    """Represents an ConveyorConfig."""

    __tablename__ = 'conveyor_config'
    id = Column(Integer, primary_key=True)
    config_key = Column(String(length=36), nullable=False)
    config_value = Column(String(length=36), nullable=False)


def get_session():
    from conveyor.db.sqlalchemy import api as db_api
    return db_api.get_session()


class HeatBase(models.ModelBase, models.TimestampMixin):
    """Base class for Heat Models."""
    __table_args__ = {'mysql_engine': 'InnoDB'}

    def expire(self, session=None, attrs=None):
        """Expire this object ()."""
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.expire(self, attrs)

    def refresh(self, session=None, attrs=None):
        """Refresh this object."""
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.refresh(self, attrs)

    def delete(self, session=None):
        """Delete this object."""
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.begin(subtransactions=True)
        session.delete(self)
        session.commit()

    def update_and_save(self, values, session=None):
        if not session:
            session = orm_session.Session.object_session(self)
            if not session:
                session = get_session()
        session.begin(subtransactions=True)
        for k, v in six.iteritems(values):
            setattr(self, k, v)
        session.commit()


class SoftDelete(object):
    deleted_at = sqlalchemy.Column(sqlalchemy.DateTime)

    def soft_delete(self, session=None):
        """Mark this object as deleted."""
        self.update_and_save({'deleted_at': timeutils.utcnow()},
                             session=session)


class StateAware(object):
    action = sqlalchemy.Column('action', sqlalchemy.String(255))
    status = sqlalchemy.Column('status', sqlalchemy.String(255))
    status_reason = sqlalchemy.Column('status_reason', sqlalchemy.Text)


class RawTemplate(BASE, CopyBase):
    """Represents an unparsed template which should be in JSON format."""

    __tablename__ = 'raw_template'
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    template = sqlalchemy.Column(types.Json)
    files = sqlalchemy.Column(types.Json)
    environment = sqlalchemy.Column('environment', types.Json)


class StackTag(BASE, CopyBase):
    """Key/value store of arbitrary stack tags."""

    __tablename__ = 'stack_tag'

    id = sqlalchemy.Column('id',
                           sqlalchemy.Integer,
                           primary_key=True,
                           nullable=False)
    tag = sqlalchemy.Column('tag', sqlalchemy.Unicode(80))
    stack_id = sqlalchemy.Column('stack_id',
                                 sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('stack.id'),
                                 nullable=False)


class SyncPoint(BASE, CopyBase):
    """Represents a syncpoint for a stack that is being worked on."""

    __tablename__ = 'sync_point'
    __table_args__ = (
        sqlalchemy.PrimaryKeyConstraint('entity_id',
                                        'traversal_id',
                                        'is_update'),
        sqlalchemy.ForeignKeyConstraint(['stack_id'], ['stack.id'])
    )

    entity_id = sqlalchemy.Column(sqlalchemy.String(36))
    traversal_id = sqlalchemy.Column(sqlalchemy.String(36))
    is_update = sqlalchemy.Column(sqlalchemy.Boolean)
    # integer field for atomic update operations
    atomic_key = sqlalchemy.Column(sqlalchemy.Integer, nullable=False)
    stack_id = sqlalchemy.Column(sqlalchemy.String(36),
                                 nullable=False)
    input_data = sqlalchemy.Column(types.Json)


class Stack(BASE, CopyBase, SoftDelete, StateAware):
    """Represents a stack created by the heat engine."""

    __tablename__ = 'stack'
    __table_args__ = (
        sqlalchemy.Index('ix_stack_name', 'name', mysql_length=255),
        sqlalchemy.Index('ix_stack_tenant', 'tenant', mysql_length=255),
    )

    id = sqlalchemy.Column(sqlalchemy.String(36), primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    name = sqlalchemy.Column(sqlalchemy.String(255))
    raw_template_id = sqlalchemy.Column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey('raw_template.id'),
        nullable=False)
    raw_template = relationship(RawTemplate, cascade="all,delete",
                                backref=backref('stack'),
                                foreign_keys=[raw_template_id],
                                lazy='subquery')
    prev_raw_template_id = sqlalchemy.Column(
        'prev_raw_template_id',
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey('raw_template.id'))
    prev_raw_template = relationship(RawTemplate,
                                     foreign_keys=[prev_raw_template_id],
                                     lazy='subquery')
    username = sqlalchemy.Column(sqlalchemy.String(256))
    tenant = sqlalchemy.Column(sqlalchemy.String(256))
    user_creds_id = sqlalchemy.Column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey('user_creds.id'))
    owner_id = sqlalchemy.Column(sqlalchemy.String(36), index=True)
    parent_resource_name = sqlalchemy.Column(sqlalchemy.String(255))
    timeout = sqlalchemy.Column(sqlalchemy.Integer)
    disable_rollback = sqlalchemy.Column(sqlalchemy.Boolean, nullable=False)
    stack_user_project_id = sqlalchemy.Column(sqlalchemy.String(64))
    backup = sqlalchemy.Column('backup', sqlalchemy.Boolean)
    nested_depth = sqlalchemy.Column('nested_depth', sqlalchemy.Integer)
    convergence = sqlalchemy.Column('convergence', sqlalchemy.Boolean)
    tags = relationship(StackTag, cascade="all,delete",
                        backref=backref('stack'), lazy='subquery')
    # lazy='joined'
    current_traversal = sqlalchemy.Column('current_traversal',
                                          sqlalchemy.String(36))
    current_deps = sqlalchemy.Column('current_deps', types.Json)

    # Override timestamp column to store the correct value: it should be the
    # time the create/update call was issued, not the time the DB entry is
    # created/modified. (bug #1193269)
    updated_at = sqlalchemy.Column(sqlalchemy.DateTime)


class StackLock(BASE, CopyBase):
    """Store stack locks for deployments with multiple-engines."""

    __tablename__ = 'stack_lock'

    stack_id = sqlalchemy.Column(sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('stack.id'),
                                 primary_key=True)
    engine_id = sqlalchemy.Column(sqlalchemy.String(36))


class UserCreds(BASE, CopyBase):
    """Represents user credentials.

    Also, mirrors the 'context' handed in by wsgi.
    """

    __tablename__ = 'user_creds'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    username = sqlalchemy.Column(sqlalchemy.String(255))
    password = sqlalchemy.Column(sqlalchemy.String(255))
    region_name = sqlalchemy.Column(sqlalchemy.String(255))
    decrypt_method = sqlalchemy.Column(sqlalchemy.String(64))
    tenant = sqlalchemy.Column(sqlalchemy.String(1024))
    auth_url = sqlalchemy.Column(sqlalchemy.Text)
    tenant_id = sqlalchemy.Column(sqlalchemy.String(256))
    trust_id = sqlalchemy.Column(sqlalchemy.String(255))
    trustor_user_id = sqlalchemy.Column(sqlalchemy.String(64))
    stack = relationship(Stack, backref=backref('user_creds'),
                         cascade_backrefs=False, lazy='subquery')


class Event(BASE, CopyBase):
    """Represents an event generated by the heat engine."""

    __tablename__ = 'event'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    stack_id = sqlalchemy.Column(sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('stack.id'),
                                 nullable=False)
    stack = relationship(Stack, backref=backref('events'), lazy='subquery')

    uuid = sqlalchemy.Column(sqlalchemy.String(36),
                             default=lambda: str(uuid.uuid4()),
                             unique=True)
    resource_action = sqlalchemy.Column(sqlalchemy.String(255))
    resource_status = sqlalchemy.Column(sqlalchemy.String(255))
    resource_name = sqlalchemy.Column(sqlalchemy.String(255))
    physical_resource_id = sqlalchemy.Column(sqlalchemy.String(255))
    _resource_status_reason = sqlalchemy.Column(
        'resource_status_reason', sqlalchemy.String(255))
    resource_type = sqlalchemy.Column(sqlalchemy.String(255))
    resource_properties = sqlalchemy.Column(sqlalchemy.PickleType)

    @property
    def resource_status_reason(self):
        return self._resource_status_reason

    @resource_status_reason.setter
    def resource_status_reason(self, reason):
        self._resource_status_reason = reason and reason[:255] or ''


class ResourceData(BASE, CopyBase):
    """Key/value store of arbitrary, resource-specific data."""

    __tablename__ = 'resource_data'

    id = sqlalchemy.Column('id',
                           sqlalchemy.Integer,
                           primary_key=True,
                           nullable=False)
    key = sqlalchemy.Column('key', sqlalchemy.String(255))
    value = sqlalchemy.Column('value', sqlalchemy.Text)
    redact = sqlalchemy.Column('redact', sqlalchemy.Boolean)
    decrypt_method = sqlalchemy.Column(sqlalchemy.String(64))
    resource_id = sqlalchemy.Column('resource_id',
                                    sqlalchemy.Integer,
                                    sqlalchemy.ForeignKey('resource.id'),
                                    nullable=False)


class Resource(BASE, CopyBase, StateAware):
    """Represents a resource created by the heat engine."""

    __tablename__ = 'resource'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    uuid = sqlalchemy.Column(sqlalchemy.String(36),
                             default=lambda: str(uuid.uuid4()),
                             unique=True)
    name = sqlalchemy.Column('name', sqlalchemy.String(255))
    nova_instance = sqlalchemy.Column('nova_instance', sqlalchemy.String(255))
    # odd name as "metadata" is reserved
    rsrc_metadata = sqlalchemy.Column('rsrc_metadata', types.Json)

    stack_id = sqlalchemy.Column(sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('stack.id'),
                                 nullable=False)
    stack = relationship(Stack, backref=backref('resources'), lazy='subquery')
    root_stack_id = sqlalchemy.Column(sqlalchemy.String(36), index=True)
    data = relationship(ResourceData,
                        cascade="all,delete",
                        backref=backref('resource'),
                        lazy='subquery')

    # Override timestamp column to store the correct value: it should be the
    # time the create/update call was issued, not the time the DB entry is
    # created/modified. (bug #1193269)
    updated_at = sqlalchemy.Column(sqlalchemy.DateTime)
    properties_data = sqlalchemy.Column('properties_data', types.Json)
    properties_data_encrypted = sqlalchemy.Column('properties_data_encrypted',
                                                  sqlalchemy.Boolean)
    engine_id = sqlalchemy.Column(sqlalchemy.String(36))
    atomic_key = sqlalchemy.Column(sqlalchemy.Integer)

    needed_by = sqlalchemy.Column('needed_by', types.List)
    requires = sqlalchemy.Column('requires', types.List)
    replaces = sqlalchemy.Column('replaces', sqlalchemy.Integer,
                                 default=None)
    replaced_by = sqlalchemy.Column('replaced_by', sqlalchemy.Integer,
                                    default=None)
    current_template_id = sqlalchemy.Column(
        'current_template_id',
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey('raw_template.id'))


class WatchRule(BASE, CopyBase):
    """Represents a watch_rule created by the heat engine."""

    __tablename__ = 'watch_rule'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    name = sqlalchemy.Column('name', sqlalchemy.String(255))
    rule = sqlalchemy.Column('rule', types.Json)
    state = sqlalchemy.Column('state', sqlalchemy.String(255))
    last_evaluated = sqlalchemy.Column(sqlalchemy.DateTime,
                                       default=timeutils.utcnow)

    stack_id = sqlalchemy.Column(sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('stack.id'),
                                 nullable=False)
    stack = relationship(Stack, backref=backref('watch_rule'),
                         lazy='subquery')


class WatchData(BASE, CopyBase):
    """Represents a watch_data created by the heat engine."""

    __tablename__ = 'watch_data'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    data = sqlalchemy.Column('data', types.Json)

    watch_rule_id = sqlalchemy.Column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey('watch_rule.id'),
        nullable=False)
    watch_rule = relationship(WatchRule, backref=backref('watch_data'),
                              lazy='subquery')


class SoftwareConfig(BASE, CopyBase):
    """Represents a software configuration resource.

    Represents a software configuration resource to be applied to one or more
    servers.
    """

    __tablename__ = 'software_config'

    id = sqlalchemy.Column('id', sqlalchemy.String(36), primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    name = sqlalchemy.Column('name', sqlalchemy.String(255))
    group = sqlalchemy.Column('group', sqlalchemy.String(255))
    config = sqlalchemy.Column('config', types.Json)
    tenant = sqlalchemy.Column(
        'tenant', sqlalchemy.String(64), nullable=False, index=True)


class SoftwareDeployment(BASE, CopyBase, StateAware):
    """Represents a software deployment resource.

    Represents applying a software configuration resource to a single server
    resource.
    """

    __tablename__ = 'software_deployment'
    __table_args__ = (
        sqlalchemy.Index('ix_software_deployment_created_at', 'created_at'),)

    id = sqlalchemy.Column('id', sqlalchemy.String(36), primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    config_id = sqlalchemy.Column(
        'config_id',
        sqlalchemy.String(36),
        sqlalchemy.ForeignKey('software_config.id'),
        nullable=False)
    config = relationship(SoftwareConfig, backref=backref('deployments'),
                          lazy='subquery')
    server_id = sqlalchemy.Column('server_id', sqlalchemy.String(36),
                                  nullable=False, index=True)
    input_values = sqlalchemy.Column('input_values', types.Json)
    output_values = sqlalchemy.Column('output_values', types.Json)
    tenant = sqlalchemy.Column(
        'tenant', sqlalchemy.String(64), nullable=False, index=True)
    stack_user_project_id = sqlalchemy.Column(sqlalchemy.String(64))
    updated_at = sqlalchemy.Column(sqlalchemy.DateTime)


class Snapshot(BASE, CopyBase):

    __tablename__ = 'snapshot'

    id = sqlalchemy.Column('id', sqlalchemy.String(36), primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    stack_id = sqlalchemy.Column(sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('stack.id'),
                                 nullable=False)
    name = sqlalchemy.Column('name', sqlalchemy.String(255))
    data = sqlalchemy.Column('data', types.Json)
    tenant = sqlalchemy.Column(
        'tenant', sqlalchemy.String(64), nullable=False, index=True)
    status = sqlalchemy.Column('status', sqlalchemy.String(255))
    status_reason = sqlalchemy.Column('status_reason', sqlalchemy.String(255))
    stack = relationship(Stack, backref=backref('snapshot'), lazy='subquery')


class Service(BASE, CopyBase, SoftDelete):

    __tablename__ = 'service'

    id = sqlalchemy.Column('id',
                           sqlalchemy.String(36),
                           primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    engine_id = sqlalchemy.Column('engine_id',
                                  sqlalchemy.String(36),
                                  nullable=False)
    host = sqlalchemy.Column('host',
                             sqlalchemy.String(255),
                             nullable=False)
    hostname = sqlalchemy.Column('hostname',
                                 sqlalchemy.String(255),
                                 nullable=False)
    binary = sqlalchemy.Column('binary',
                               sqlalchemy.String(255),
                               nullable=False)
    topic = sqlalchemy.Column('topic',
                              sqlalchemy.String(255),
                              nullable=False)
    report_interval = sqlalchemy.Column('report_interval',
                                        sqlalchemy.Integer,
                                        nullable=False)


class GWGroup(BASE, CopyBase):

    __tablename__ = 'gw_group'

    id = sqlalchemy.Column('id', sqlalchemy.String(36),
                           primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    name = sqlalchemy.Column('name', sqlalchemy.String(255), nullable=False)
    type = sqlalchemy.Column('type', sqlalchemy.String(255), nullable=True)
    data = sqlalchemy.Column('data', types.Json)

    sqlalchemy.Column('created_at', sqlalchemy.DateTime)
    sqlalchemy.Column('updated_at', sqlalchemy.DateTime)

    tenant = sqlalchemy.Column('tenant', sqlalchemy.String(64),
                               nullable=False)
    user = sqlalchemy.Column('user', sqlalchemy.String(64),
                             nullable=False)


class GWMember(BASE, CopyBase):

    __tablename__ = 'gw_member'

    id = sqlalchemy.Column('id', sqlalchemy.String(36),
                           primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    group_id = sqlalchemy.Column('group_id', sqlalchemy.String(36),
                                 sqlalchemy.ForeignKey('gw_group.id'),
                                 nullable=True)
    name = sqlalchemy.Column('name', sqlalchemy.String(255),
                             nullable=True)

    group = relationship(GWGroup, backref="members", lazy='subquery')


class GWAlarm(BASE, CopyBase):

    __tablename__ = 'gw_alarm'

    id = sqlalchemy.Column('id', sqlalchemy.String(36),
                           primary_key=True,
                           default=lambda: str(uuid.uuid4()))
    resource_id = sqlalchemy.Column('resource_id', sqlalchemy.String(36))
    group_id = sqlalchemy.Column('group_id', sqlalchemy.String(36),
                                 index=True,
                                 nullable=True)
    actions = sqlalchemy.Column('actions', types.Json)
    meter_name = sqlalchemy.Column('meter_name', sqlalchemy.String(255),
                                   nullable=False)
    data = sqlalchemy.Column('data', types.Json)
    tenant = sqlalchemy.Column('tenant', sqlalchemy.String(64),
                               nullable=False)
    user = sqlalchemy.Column('user', sqlalchemy.String(64), nullable=False)
    sqlalchemy.Column('created_at', sqlalchemy.DateTime)
    sqlalchemy.Column('updated_at', sqlalchemy.DateTime)


class PlanStack(BASE, ConveyorBase):
    """Represents a plan."""
    __tablename__ = "plan_stack"

    id = sqlalchemy.Column(Integer, primary_key=True)
    stack_id = sqlalchemy.Column('stack_id', sqlalchemy.String(length=36),
                                 nullable=False)
    plan_id = sqlalchemy.Column('plan_id', sqlalchemy.String(length=36),
                                nullable=False)
