"""add clone_update_request table

Revision ID: r8m9n0o1p2q3
Revises: q7l8m9n0o1p2
Create Date: 2026-01-21

Adds clone_update_request table for tracking update requests from parent agents to clones.
Each request stores the specific actions to be performed (copy_files_folder, rebuild_environment).
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'r8m9n0o1p2q3'
down_revision = 'q7l8m9n0o1p2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('clone_update_request',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('clone_agent_id', sa.Uuid(), nullable=False),
        sa.Column('parent_agent_id', sa.Uuid(), nullable=False),
        sa.Column('pushed_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('copy_files_folder', sa.Boolean(), nullable=False, default=False),
        sa.Column('rebuild_environment', sa.Boolean(), nullable=False, default=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('dismissed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['clone_agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['pushed_by_user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    # Add index for efficient lookup of pending requests by clone
    op.create_index(
        'ix_clone_update_request_clone_status',
        'clone_update_request',
        ['clone_agent_id', 'status'],
        unique=False
    )


def downgrade():
    op.drop_index('ix_clone_update_request_clone_status', table_name='clone_update_request')
    op.drop_table('clone_update_request')
