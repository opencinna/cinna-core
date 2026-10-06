"""add task_protocol_version to agent_environment

Revision ID: d4f8b2c6e1a3
Revises: c3e7a1d9b2f4
Create Date: 2026-10-06 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4f8b2c6e1a3'
down_revision = 'c3e7a1d9b2f4'
branch_labels = None
depends_on = None


def upgrade():
    # Cinna Task Protocol version advertised by the env-core. Existing rows are
    # images built before the protocol, so they read as 0.
    op.add_column(
        'agent_environment',
        sa.Column('task_protocol_version', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade():
    op.drop_column('agent_environment', 'task_protocol_version')
