"""add inactivity_period_limit to agent

Revision ID: 3b7ae224b1c7
Revises: 94f7ed6ae0d9
Create Date: 2026-02-27 09:13:25.735812

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '3b7ae224b1c7'
down_revision = '94f7ed6ae0d9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent', sa.Column('inactivity_period_limit', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade():
    op.drop_column('agent', 'inactivity_period_limit')
