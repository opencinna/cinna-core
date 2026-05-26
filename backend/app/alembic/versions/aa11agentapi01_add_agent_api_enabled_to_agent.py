"""add agent_api_enabled to agent

Revision ID: aa11agentapi01
Revises: cd4ef5a6b7c8
Create Date: 2026-05-25 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'aa11agentapi01'
down_revision = 'cd4ef5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent', sa.Column('agent_api_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade():
    op.drop_column('agent', 'agent_api_enabled')
