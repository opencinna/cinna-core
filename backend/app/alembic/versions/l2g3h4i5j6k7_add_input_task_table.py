"""add input_task table and session.source_task_id

Revision ID: l2g3h4i5j6k7
Revises: k1f2g3h4i5j6
Create Date: 2026-01-17

Adds input_task table for task management workflow.
Adds source_task_id to session table for tracking which task spawned it.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'l2g3h4i5j6k7'
down_revision = 'k1f2g3h4i5j6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('input_task',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('selected_agent_id', sa.Uuid(), nullable=True),
        sa.Column('session_id', sa.Uuid(), nullable=True),
        sa.Column('user_workspace_id', sa.Uuid(), nullable=True),
        sa.Column('original_message', sqlmodel.sql.sqltypes.AutoString(length=10000), nullable=False),
        sa.Column('current_description', sqlmodel.sql.sqltypes.AutoString(length=10000), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('refinement_history', sa.JSON(), nullable=True),
        sa.Column('error_message', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('executed_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['selected_agent_id'], ['agent.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['session_id'], ['session.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_workspace_id'], ['user_workspace.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    # Add index for efficient task listing by owner and status
    op.create_index(
        'ix_input_task_owner_status',
        'input_task',
        ['owner_id', 'status'],
        unique=False
    )

    # Add source_task_id to session table (must be after input_task is created)
    op.add_column(
        'session',
        sa.Column('source_task_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_session_source_task_id',
        'session',
        'input_task',
        ['source_task_id'],
        ['id'],
        ondelete='SET NULL'
    )


def downgrade():
    # Remove source_task_id from session table first
    op.drop_constraint('fk_session_source_task_id', 'session', type_='foreignkey')
    op.drop_column('session', 'source_task_id')

    op.drop_index('ix_input_task_owner_status', table_name='input_task')
    op.drop_table('input_task')
