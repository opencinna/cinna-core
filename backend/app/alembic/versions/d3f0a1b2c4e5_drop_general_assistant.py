"""Drop general assistant fields from agent and user

The General Assistant feature has been removed (replaced by the account-CLI
local-orchestrator workflow). This migration drops the columns and the partial
unique index added by ``4d769edd79d2_add_general_assistant``.

Revision ID: d3f0a1b2c4e5
Revises: 5abf2cec7a18
Create Date: 2026-06-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3f0a1b2c4e5'
down_revision = '5abf2cec7a18'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('ix_agent_general_assistant_per_user', table_name='agent')
    op.drop_column('agent', 'is_general_assistant')
    op.drop_column('user', 'general_assistant_enabled')


def downgrade():
    op.add_column(
        'user',
        sa.Column(
            'general_assistant_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'agent',
        sa.Column(
            'is_general_assistant',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.create_index(
        'ix_agent_general_assistant_per_user',
        'agent',
        ['owner_id'],
        unique=True,
        postgresql_where=sa.text('is_general_assistant = true'),
    )
