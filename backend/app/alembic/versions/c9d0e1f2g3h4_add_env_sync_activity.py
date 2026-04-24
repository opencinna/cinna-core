"""add env sync activity fields

Revision ID: c9d0e1f2g3h4
Revises: z6u7v8w9x0y1
Create Date: 2026-04-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d0e1f2g3h4'
down_revision = 'a7v8w9x0y1z2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_environment',
        sa.Column('last_sync_activity_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'agent_environment',
        sa.Column('sync_active', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    # Partial index for fast lookup of envs with active sync
    op.create_index(
        'ix_agent_environment_sync_active',
        'agent_environment',
        ['sync_active'],
        postgresql_where=sa.text('sync_active = TRUE'),
    )


def downgrade():
    op.drop_index('ix_agent_environment_sync_active', table_name='agent_environment')
    op.drop_column('agent_environment', 'sync_active')
    op.drop_column('agent_environment', 'last_sync_activity_at')
