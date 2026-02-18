"""add agent_session_mode and sender_email fields

Revision ID: f3a1b2c4d5e6
Revises: 8a95916ab539
Create Date: 2026-02-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'f3a1b2c4d5e6'
down_revision = '8a95916ab539'
branch_labels = None
depends_on = None


def upgrade():
    # Add agent_session_mode to agent_email_integration
    op.add_column(
        'agent_email_integration',
        sa.Column(
            'agent_session_mode',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='clone',
        )
    )

    # Add sender_email to session (owner mode: track original email sender)
    op.add_column(
        'session',
        sa.Column(
            'sender_email',
            sqlmodel.sql.sqltypes.AutoString(length=320),
            nullable=True,
        )
    )

    # Make clone_agent_id nullable in outgoing_email_queue (owner mode has no clone)
    op.alter_column(
        'outgoing_email_queue',
        'clone_agent_id',
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        'outgoing_email_queue',
        'clone_agent_id',
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.drop_column('session', 'sender_email')
    op.drop_column('agent_email_integration', 'agent_session_mode')
