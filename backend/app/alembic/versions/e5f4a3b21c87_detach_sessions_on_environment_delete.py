"""detach sessions on environment delete

Revision ID: e5f4a3b21c87
Revises: d1e2f3a4b5c6
Create Date: 2026-04-22 12:00:00.000000

Makes session.environment_id nullable and switches the FK from
ON DELETE CASCADE to ON DELETE SET NULL. When an AgentEnvironment is
deleted, its sessions are detached instead of wiped; on the next message
send they auto-rebind to the agent's current active environment.

Note: downgrade re-adds the NOT NULL constraint and will fail once any
detached sessions exist (environment_id IS NULL). An operator running
the downgrade must first delete or re-bind those rows.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f4a3b21c87'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('session', 'environment_id',
                    existing_type=sa.Uuid(),
                    nullable=True)
    op.drop_constraint('session_environment_id_fkey', 'session', type_='foreignkey')
    op.create_foreign_key(
        'session_environment_id_fkey',
        'session',
        'agent_environment',
        ['environment_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint('session_environment_id_fkey', 'session', type_='foreignkey')
    op.create_foreign_key(
        'session_environment_id_fkey',
        'session',
        'agent_environment',
        ['environment_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.alter_column('session', 'environment_id',
                    existing_type=sa.Uuid(),
                    nullable=False)
