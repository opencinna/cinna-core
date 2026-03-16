"""Add general assistant fields to agent and user

Revision ID: 4d769edd79d2
Revises: z6u7v8w9x0y1
Create Date: 2026-03-15 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4d769edd79d2'
down_revision = 'f55c23690563'
branch_labels = None
depends_on = None


def upgrade():
    # Add is_general_assistant flag to agent table (default false for existing rows)
    op.add_column(
        'agent',
        sa.Column(
            'is_general_assistant',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    # Partial unique index: at most one General Assistant per user
    op.create_index(
        'ix_agent_general_assistant_per_user',
        'agent',
        ['owner_id'],
        unique=True,
        postgresql_where=sa.text('is_general_assistant = true'),
    )
    # Add general_assistant_enabled flag to user table (default false for existing users)
    op.add_column(
        'user',
        sa.Column(
            'general_assistant_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade():
    op.drop_index('ix_agent_general_assistant_per_user', table_name='agent')
    op.drop_column('agent', 'is_general_assistant')
    op.drop_column('user', 'general_assistant_enabled')
