# Copyright 2012 OpenStack Foundation
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

from oslo_log import log as logging
from sqlalchemy import Column, DateTime
from sqlalchemy import Index, Integer, MetaData, String, Table

from conveyor.db.sqlalchemy import types

LOG = logging.getLogger(__name__)


# Note on the autoincrement flag: this is defaulted for primary key columns
# of integral type, so is no longer set explicitly in such cases.

def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    plan = Table('plans', meta,
                 Column('created_at', DateTime(timezone=False)),
                 Column('updated_at', DateTime(timezone=False)),
                 Column('deleted_at', DateTime(timezone=False)),
                 Column('id', Integer, primary_key=True, nullable=False),
                 Column('plan_id', String(length=36), nullable=False),
                 Column('plan_name', String(length=255), nullable=True),

                 Column('project_id', String(length=36)),
                 Column('user_id', String(length=36)),

                 Column('task_status', String(length=255)),
                 Column('plan_status', String(length=255)),
                 Column('plan_type', String(length=255)),
                 Column('clone_resources', types.Json),
                 Column('stack_id', String(length=36)),
                 Column('deleted', Integer),
                 mysql_engine='InnoDB',
                 mysql_charset='utf8'
                 )

    # create all tables
    plan.create()
    Index('plan_id', plan.c.plan_id, unique=True).create()


def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    for table in ('plans', ):
        for prefix in ('', 'shadow_'):
            table_name = prefix + table
            if migrate_engine.has_table(table_name):
                instance_extra = Table(table_name, meta, autoload=True)
                instance_extra.drop()
