"""agent_improvement_request kind

Agent-API capability requests reuse the improvement-request row: kind is
session (a frozen session share, every existing row) or agent_api.
Adds the producer-inbox and rate-limit indexes.

Revision ID: 39b73f2a5ab4
Revises: 713c48f71fc6
Create Date: 2026-11-12 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '39b73f2a5ab4'
down_revision = '713c48f71fc6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_improvement_request',
        sa.Column(
            'kind',
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'session'"),
        ),
    )
    op.create_index(
        'ix_air_target_kind_status',
        'agent_improvement_request',
        ['target_agent_id', 'kind', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_air_requester_target_created',
        'agent_improvement_request',
        ['requester_user_id', 'target_agent_id', sa.text('created_at DESC')],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_air_requester_target_created', table_name='agent_improvement_request')
    op.drop_index('ix_air_target_kind_status', table_name='agent_improvement_request')
    op.drop_column('agent_improvement_request', 'kind')
