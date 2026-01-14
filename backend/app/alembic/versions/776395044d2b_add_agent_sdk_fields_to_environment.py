"""add agent_sdk fields to environment

Revision ID: 776395044d2b
Revises: a1b8a2315572
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '776395044d2b'
down_revision = 'a1b8a2315572'
branch_labels = None
depends_on = None


def upgrade():
    # Add SDK selection fields to agent_environment table
    # These are immutable after creation and default to claude-code/anthropic
    op.add_column('agent_environment', sa.Column('agent_sdk_conversation', sa.String(), nullable=True))
    op.add_column('agent_environment', sa.Column('agent_sdk_building', sa.String(), nullable=True))


def downgrade():
    op.drop_column('agent_environment', 'agent_sdk_building')
    op.drop_column('agent_environment', 'agent_sdk_conversation')
