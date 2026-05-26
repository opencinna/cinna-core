"""add agent api spec + policy cache to environment

Revision ID: aa22agentapi02
Revises: aa11agentapi01
Create Date: 2026-05-25 10:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'aa22agentapi02'
down_revision = 'aa11agentapi01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent_environment', sa.Column('agent_api_spec_parsed', sa.JSON(), nullable=True))
    op.add_column('agent_environment', sa.Column('agent_api_spec_fetched_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_environment', sa.Column('agent_api_spec_error', sa.String(length=512), nullable=True))
    op.add_column('agent_environment', sa.Column('agent_api_policy_cache', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('agent_environment', 'agent_api_policy_cache')
    op.drop_column('agent_environment', 'agent_api_spec_error')
    op.drop_column('agent_environment', 'agent_api_spec_fetched_at')
    op.drop_column('agent_environment', 'agent_api_spec_parsed')
