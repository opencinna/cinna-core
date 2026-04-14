"""add identity mcp tables

Revision ID: f7d39032b418
Revises: 516c73047c76
Create Date: 2026-04-14 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'f7d39032b418'
down_revision = '516c73047c76'
branch_labels = None
depends_on = None


def upgrade():
    # Create identity_agent_binding table
    op.create_table(
        'identity_agent_binding',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.Uuid(), nullable=False),
        sa.Column('trigger_prompt', sa.Text(), nullable=False),
        sa.Column('message_patterns', sa.Text(), nullable=True),
        sa.Column('session_mode', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'agent_id', name='uq_identity_agent_binding'),
    )
    op.create_index(
        op.f('ix_identity_agent_binding_owner_id'),
        'identity_agent_binding',
        ['owner_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_identity_agent_binding_agent_id'),
        'identity_agent_binding',
        ['agent_id'],
        unique=False,
    )

    # Create identity_binding_assignment table
    op.create_table(
        'identity_binding_assignment',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('binding_id', sa.Uuid(), nullable=False),
        sa.Column('target_user_id', sa.Uuid(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False),
        sa.Column('auto_enable', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['binding_id'], ['identity_agent_binding.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['target_user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'binding_id', 'target_user_id', name='uq_identity_binding_assignment'
        ),
    )
    op.create_index(
        op.f('ix_identity_binding_assignment_binding_id'),
        'identity_binding_assignment',
        ['binding_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_identity_binding_assignment_target_user_id'),
        'identity_binding_assignment',
        ['target_user_id'],
        unique=False,
    )

    # Add identity columns to session table
    op.add_column(
        'session',
        sa.Column('identity_caller_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_session_identity_caller_id',
        'session',
        'user',
        ['identity_caller_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_session_identity_caller_id',
        'session',
        ['identity_caller_id'],
    )

    op.add_column(
        'session',
        sa.Column('identity_binding_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_session_identity_binding_id',
        'session',
        'identity_agent_binding',
        ['identity_binding_id'],
        ['id'],
        ondelete='SET NULL',
    )

    op.add_column(
        'session',
        sa.Column('identity_binding_assignment_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_session_identity_binding_assignment_id',
        'session',
        'identity_binding_assignment',
        ['identity_binding_assignment_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    # Remove session columns
    op.drop_constraint('fk_session_identity_binding_assignment_id', 'session', type_='foreignkey')
    op.drop_column('session', 'identity_binding_assignment_id')

    op.drop_constraint('fk_session_identity_binding_id', 'session', type_='foreignkey')
    op.drop_column('session', 'identity_binding_id')

    op.drop_index('ix_session_identity_caller_id', table_name='session')
    op.drop_constraint('fk_session_identity_caller_id', 'session', type_='foreignkey')
    op.drop_column('session', 'identity_caller_id')

    # Drop assignment table first (FK dependency)
    op.drop_index(
        op.f('ix_identity_binding_assignment_target_user_id'),
        table_name='identity_binding_assignment',
    )
    op.drop_index(
        op.f('ix_identity_binding_assignment_binding_id'),
        table_name='identity_binding_assignment',
    )
    op.drop_table('identity_binding_assignment')

    # Then drop binding table
    op.drop_index(
        op.f('ix_identity_agent_binding_agent_id'),
        table_name='identity_agent_binding',
    )
    op.drop_index(
        op.f('ix_identity_agent_binding_owner_id'),
        table_name='identity_agent_binding',
    )
    op.drop_table('identity_agent_binding')
