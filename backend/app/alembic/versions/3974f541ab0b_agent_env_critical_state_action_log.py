"""agent env critical state + action log

Revision ID: 3974f541ab0b
Revises: 6e6af979678c
Create Date: 2026-06-20 15:33:57.956752

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '3974f541ab0b'
down_revision = '6e6af979678c'
branch_labels = None
depends_on = None


def upgrade():
    # New append-only action/event log for agent-environment operations.
    op.create_table('agent_env_action_log',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('environment_id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('action', sa.String(length=48), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('cause', sa.String(length=64), nullable=True),
    sa.Column('summary', sa.String(length=512), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['environment_id'], ['agent_environment.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agent_env_action_log_agent_id', 'agent_env_action_log', ['agent_id'], unique=False)
    op.create_index('ix_agent_env_action_log_environment_id', 'agent_env_action_log', ['environment_id'], unique=False)
    op.create_index('ix_agent_env_action_log_executed_at', 'agent_env_action_log', ['executed_at'], unique=False)
    op.add_column('agent_environment', sa.Column('critical_state', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('agent_environment', sa.Column('critical_cause', sa.String(length=64), nullable=True))
    op.add_column('agent_environment', sa.Column('critical_since', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_agent_environment_critical_state', 'agent_environment', ['critical_state'], unique=False, postgresql_where=sa.text('critical_state = true'))


def downgrade():
    op.drop_index('ix_agent_environment_critical_state', table_name='agent_environment', postgresql_where=sa.text('critical_state = true'))
    op.drop_column('agent_environment', 'critical_since')
    op.drop_column('agent_environment', 'critical_cause')
    op.drop_column('agent_environment', 'critical_state')
    op.drop_index('ix_agent_env_action_log_executed_at', table_name='agent_env_action_log')
    op.drop_index('ix_agent_env_action_log_environment_id', table_name='agent_env_action_log')
    op.drop_index('ix_agent_env_action_log_agent_id', table_name='agent_env_action_log')
    op.drop_table('agent_env_action_log')
