"""add email task processing fields

Revision ID: h5c3d4e6f7g8
Revises: g4b2c3d5e6f7
Create Date: 2026-02-23 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h5c3d4e6f7g8'
down_revision = 'g4b2c3d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    # Add process_as to agent_email_integration
    op.add_column(
        'agent_email_integration',
        sa.Column('process_as', sa.String(), nullable=False, server_default='new_session')
    )

    # Add email source tracking fields to input_task
    op.add_column(
        'input_task',
        sa.Column('source_email_message_id', sa.Uuid(), nullable=True)
    )
    op.add_column(
        'input_task',
        sa.Column('source_agent_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_input_task_source_email_message_id',
        'input_task', 'email_message',
        ['source_email_message_id'], ['id'],
        ondelete='SET NULL'
    )
    op.create_foreign_key(
        'fk_input_task_source_agent_id',
        'input_task', 'agent',
        ['source_agent_id'], ['id'],
        ondelete='SET NULL'
    )

    # Add input_task_id to email_message
    op.add_column(
        'email_message',
        sa.Column('input_task_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_email_message_input_task_id',
        'email_message', 'input_task',
        ['input_task_id'], ['id'],
        ondelete='SET NULL'
    )

    # Add input_task_id to outgoing_email_queue
    op.add_column(
        'outgoing_email_queue',
        sa.Column('input_task_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_outgoing_email_queue_input_task_id',
        'outgoing_email_queue', 'input_task',
        ['input_task_id'], ['id'],
        ondelete='SET NULL'
    )

    # Make session_id nullable on outgoing_email_queue
    op.alter_column(
        'outgoing_email_queue',
        'session_id',
        existing_type=sa.Uuid(),
        nullable=True
    )

    # Make message_id nullable on outgoing_email_queue
    op.alter_column(
        'outgoing_email_queue',
        'message_id',
        existing_type=sa.Uuid(),
        nullable=True
    )


def downgrade():
    # Make message_id non-nullable again
    op.alter_column(
        'outgoing_email_queue',
        'message_id',
        existing_type=sa.Uuid(),
        nullable=False
    )

    # Make session_id non-nullable again
    op.alter_column(
        'outgoing_email_queue',
        'session_id',
        existing_type=sa.Uuid(),
        nullable=False
    )

    # Remove input_task_id from outgoing_email_queue
    op.drop_constraint('fk_outgoing_email_queue_input_task_id', 'outgoing_email_queue', type_='foreignkey')
    op.drop_column('outgoing_email_queue', 'input_task_id')

    # Remove input_task_id from email_message
    op.drop_constraint('fk_email_message_input_task_id', 'email_message', type_='foreignkey')
    op.drop_column('email_message', 'input_task_id')

    # Remove source tracking fields from input_task
    op.drop_constraint('fk_input_task_source_agent_id', 'input_task', type_='foreignkey')
    op.drop_constraint('fk_input_task_source_email_message_id', 'input_task', type_='foreignkey')
    op.drop_column('input_task', 'source_agent_id')
    op.drop_column('input_task', 'source_email_message_id')

    # Remove process_as from agent_email_integration
    op.drop_column('agent_email_integration', 'process_as')
