"""add credential mcp_consumer_agent_id

Revision ID: e5f972e7e32e
Revises: 3c3c37a5e144
Create Date: 2026-06-24 10:34:28.311393

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e5f972e7e32e'
down_revision = '3c3c37a5e144'
branch_labels = None
depends_on = None


def upgrade():
    # Agent2agent MCP consumer-pair binding (Fix 2/4/5). Nullable FK -> agent.id
    # with ON DELETE SET NULL, indexed for the one-per-pair / unlink-detection
    # queries. Only ever populated for agent2agent mcp_provider credentials.
    op.add_column(
        'credential',
        sa.Column('mcp_consumer_agent_id', sa.Uuid(), nullable=True),
    )
    op.create_index(
        'ix_credential_mcp_consumer_agent_id',
        'credential',
        ['mcp_consumer_agent_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_credential_mcp_consumer_agent_id',
        'credential',
        'agent',
        ['mcp_consumer_agent_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_credential_mcp_consumer_agent_id', 'credential', type_='foreignkey'
    )
    op.drop_index('ix_credential_mcp_consumer_agent_id', table_name='credential')
    op.drop_column('credential', 'mcp_consumer_agent_id')
