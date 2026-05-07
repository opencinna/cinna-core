"""add agent_webhook and agent_webhook_log tables

Revision ID: ba8f1f14621f
Revises: f8a91b2c3e4d
Create Date: 2026-04-24 19:52:08.989227

Creates two new tables for the Agent Webhooks feature:

- ``agent_webhook`` — one configured webhook per row, scoped to an agent.
  Carries the public ``webhook_id`` slug, the Fernet-encrypted bearer token,
  and type-specific fields (prompt/session_mode for session webhooks;
  command/command_timeout_seconds for script webhooks).

- ``agent_webhook_log`` — immutable append-only invocation log. FK to
  ``agent_webhook`` (CASCADE), ``agent`` (CASCADE), and ``session`` (SET NULL
  so logs survive session deletion).
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'ba8f1f14621f'
down_revision = 'f8a91b2c3e4d'
branch_labels = None
depends_on = None


def upgrade():
    # ---- agent_webhook ----
    op.create_table(
        'agent_webhook',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('payload_template', sa.Text(), nullable=True),
        # Session-type fields
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('session_mode', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        # Script-type fields
        sa.Column('command', sa.Text(), nullable=True),
        sa.Column('command_timeout_seconds', sa.Integer(), nullable=True),
        # Token / URL slug
        sa.Column('webhook_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('webhook_token_encrypted', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('webhook_token_prefix', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
        # Execution tracking
        sa.Column('last_execution', sa.DateTime(), nullable=True),
        # Timestamps
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_webhook_agent_id', 'agent_webhook', ['agent_id'], unique=False)
    op.create_index('ix_agent_webhook_owner_id', 'agent_webhook', ['owner_id'], unique=False)
    op.create_index('ix_agent_webhook_webhook_id', 'agent_webhook', ['webhook_id'], unique=True)

    # ---- agent_webhook_log ----
    op.create_table(
        'agent_webhook_log',
        sa.Column('id', sa.Uuid(), nullable=False),
        # FK column intentionally named ``webhook_id_fk`` to avoid clashing
        # with the public ``webhook_id`` slug column on the parent table.
        sa.Column('webhook_id_fk', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.Uuid(), nullable=False),
        sa.Column('webhook_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        # Request metadata
        sa.Column('remote_ip', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('headers_subset', sa.JSON(), nullable=True),
        sa.Column('payload_received', sa.Text(), nullable=True),
        sa.Column('payload_content_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        # Session-type execution details
        sa.Column('prompt_used', sa.Text(), nullable=True),
        # Script-type execution details
        sa.Column('command_executed', sa.Text(), nullable=True),
        sa.Column('command_output', sa.Text(), nullable=True),
        sa.Column('command_stderr', sa.Text(), nullable=True),
        sa.Column('command_exit_code', sa.Integer(), nullable=True),
        # Linked session (session-type only) — SET NULL on session delete so
        # the log row survives.
        sa.Column('session_id', sa.Uuid(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('executed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['webhook_id_fk'], ['agent_webhook.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['session.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_webhook_log_webhook_fk', 'agent_webhook_log', ['webhook_id_fk'], unique=False)
    op.create_index('ix_agent_webhook_log_agent_id', 'agent_webhook_log', ['agent_id'], unique=False)
    op.create_index('ix_agent_webhook_log_executed_at', 'agent_webhook_log', ['executed_at'], unique=False)


def downgrade():
    op.drop_index('ix_agent_webhook_log_executed_at', table_name='agent_webhook_log')
    op.drop_index('ix_agent_webhook_log_agent_id', table_name='agent_webhook_log')
    op.drop_index('ix_agent_webhook_log_webhook_fk', table_name='agent_webhook_log')
    op.drop_table('agent_webhook_log')

    op.drop_index('ix_agent_webhook_webhook_id', table_name='agent_webhook')
    op.drop_index('ix_agent_webhook_owner_id', table_name='agent_webhook')
    op.drop_index('ix_agent_webhook_agent_id', table_name='agent_webhook')
    op.drop_table('agent_webhook')
