"""drop agent collaboration tables

Revision ID: c802804db41c
Revises: b8w9x0y1z2a3
Create Date: 2026-04-04 18:30:27.671939

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c802804db41c'
down_revision = 'b8w9x0y1z2a3'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('collaboration_subtask')
    op.drop_table('agent_collaboration')


def downgrade():
    op.create_table('agent_collaboration',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('title', sa.VARCHAR(length=500), autoincrement=False, nullable=False),
    sa.Column('description', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('status', sa.VARCHAR(length=50), autoincrement=False, nullable=False),
    sa.Column('coordinator_agent_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('source_session_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('shared_context', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['coordinator_agent_id'], ['agent.id'], name='agent_collaboration_coordinator_agent_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_id'], ['user.id'], name='agent_collaboration_owner_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_session_id'], ['session.id'], name='agent_collaboration_source_session_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='agent_collaboration_pkey'),
    )
    op.create_table('collaboration_subtask',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('collaboration_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('target_agent_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_message', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=50), autoincrement=False, nullable=False),
    sa.Column('result_summary', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('input_task_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('session_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('order', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['collaboration_id'], ['agent_collaboration.id'], name=op.f('collaboration_subtask_collaboration_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['input_task_id'], ['input_task.id'], name=op.f('collaboration_subtask_input_task_id_fkey'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['session.id'], name=op.f('collaboration_subtask_session_id_fkey'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['target_agent_id'], ['agent.id'], name=op.f('collaboration_subtask_target_agent_id_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('collaboration_subtask_pkey')),
    )
