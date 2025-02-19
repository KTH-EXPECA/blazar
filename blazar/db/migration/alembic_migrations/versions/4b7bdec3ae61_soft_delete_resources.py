# Copyright 2022 OpenStack Foundation.
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

"""Soft delete resources

Revision ID: 4b7bdec3ae61
Revises: beebda67d373
Create Date: 2022-05-04 17:19:47.103803

"""

# revision identifiers, used by Alembic.
revision = '4b7bdec3ae61'
down_revision = '8fe921c50030'

from alembic import op
import sqlalchemy as sa


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('extra_capabilities',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('extra_capabilities',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('computehosts',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('computehosts',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('computehost_extra_capabilities',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('computehost_extra_capabilities',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('floatingips',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('floatingips',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('network_segments',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('network_segments',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('networksegment_extra_capabilities',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('networksegment_extra_capabilities',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('devices',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('devices',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('device_extra_capabilities',
                  sa.Column('deleted', sa.String(length=36), nullable=True))
    op.add_column('device_extra_capabilities',
                  sa.Column('deleted_at', sa.DateTime(), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('extra_capabilities', 'deleted')
    op.drop_column('extra_capabilities', 'deleted_at')
    op.drop_column('computehosts', 'deleted')
    op.drop_column('computehosts', 'deleted_at')
    op.drop_column('computehost_extra_capabilities', 'deleted')
    op.drop_column('computehost_extra_capabilities', 'deleted_at')
    op.drop_column('floatingips', 'deleted')
    op.drop_column('floatingips', 'deleted_at')
    op.drop_column('network_segments', 'deleted')
    op.drop_column('network_segments', 'deleted_at')
    op.drop_column('networksegment_extra_capabilities', 'deleted')
    op.drop_column('networksegment_extra_capabilities', 'deleted_at')
    op.drop_column('devices', 'deleted')
    op.drop_column('devices', 'deleted_at')
    op.drop_column('device_extra_capabilites', 'deleted')
    op.drop_column('device_extra_capabilites', 'deleted_at')
    # ### end Alembic commands ###
