"""add task_trigger table

Revision ID: v2q3r4s5t6u7
Revises: 67bd39e7e42c
Create Date: 2026-01-27

Adds task_trigger table for automatic and event-driven task execution.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'v2q3r4s5t6u7'
down_revision = '67bd39e7e42c'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('task_trigger',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('payload_template', sqlmodel.sql.sqltypes.AutoString(length=10000), nullable=True),
        # Schedule fields
        sa.Column('cron_string', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('timezone', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('schedule_description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('last_execution', sa.DateTime(), nullable=True),
        sa.Column('next_execution', sa.DateTime(), nullable=True),
        # Exact date fields
        sa.Column('execute_at', sa.DateTime(), nullable=True),
        sa.Column('executed', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        # Webhook fields
        sa.Column('webhook_token_encrypted', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('webhook_token_prefix', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('webhook_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        # Timestamps
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        # Constraints
        sa.ForeignKeyConstraint(['task_id'], ['input_task.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    # Indexes
    op.create_index('ix_task_trigger_task_id', 'task_trigger', ['task_id'], unique=False)
    op.create_index('ix_task_trigger_schedule_poll', 'task_trigger', ['type', 'enabled', 'next_execution'], unique=False)
    op.create_index('ix_task_trigger_exact_date_poll', 'task_trigger', ['type', 'enabled', 'execute_at', 'executed'], unique=False)
    op.create_index('ix_task_trigger_webhook_id', 'task_trigger', ['webhook_id'], unique=True)
    op.create_index('ix_task_trigger_owner_id', 'task_trigger', ['owner_id'], unique=False)


def downgrade():
    op.drop_index('ix_task_trigger_owner_id', table_name='task_trigger')
    op.drop_index('ix_task_trigger_webhook_id', table_name='task_trigger')
    op.drop_index('ix_task_trigger_exact_date_poll', table_name='task_trigger')
    op.drop_index('ix_task_trigger_schedule_poll', table_name='task_trigger')
    op.drop_index('ix_task_trigger_task_id', table_name='task_trigger')
    op.drop_table('task_trigger')
