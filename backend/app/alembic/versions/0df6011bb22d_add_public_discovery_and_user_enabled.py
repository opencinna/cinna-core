"""add public_discovery and user_enabled_discoverable_sources

Revision ID: 0df6011bb22d
Revises: fa1b0655c531
Create Date: 2026-01-10 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '0df6011bb22d'
down_revision = 'fa1b0655c531'
branch_labels = None
depends_on = None


def upgrade():
    # Add public_discovery column to ai_knowledge_git_repo
    op.add_column(
        'ai_knowledge_git_repo',
        sa.Column('public_discovery', sa.Boolean(), nullable=False, server_default='false')
    )
    op.create_index(
        op.f('ix_ai_knowledge_git_repo_public_discovery'),
        'ai_knowledge_git_repo',
        ['public_discovery'],
        unique=False
    )

    # Add username column to user table
    op.add_column(
        'user',
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=True)
    )
    op.create_index(
        op.f('ix_user_username'),
        'user',
        ['username'],
        unique=False
    )

    # Create user_enabled_discoverable_sources table
    op.create_table(
        'user_enabled_discoverable_sources',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('git_repo_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['git_repo_id'], ['ai_knowledge_git_repo.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'idx_user_source_unique',
        'user_enabled_discoverable_sources',
        ['user_id', 'git_repo_id'],
        unique=True
    )
    op.create_index(
        op.f('ix_user_enabled_discoverable_sources_user_id'),
        'user_enabled_discoverable_sources',
        ['user_id'],
        unique=False
    )
    op.create_index(
        op.f('ix_user_enabled_discoverable_sources_git_repo_id'),
        'user_enabled_discoverable_sources',
        ['git_repo_id'],
        unique=False
    )


def downgrade():
    # Drop user_enabled_discoverable_sources table
    op.drop_index(
        op.f('ix_user_enabled_discoverable_sources_git_repo_id'),
        table_name='user_enabled_discoverable_sources'
    )
    op.drop_index(
        op.f('ix_user_enabled_discoverable_sources_user_id'),
        table_name='user_enabled_discoverable_sources'
    )
    op.drop_index(
        'idx_user_source_unique',
        table_name='user_enabled_discoverable_sources'
    )
    op.drop_table('user_enabled_discoverable_sources')

    # Drop username column from user table
    op.drop_index(op.f('ix_user_username'), table_name='user')
    op.drop_column('user', 'username')

    # Drop public_discovery column from ai_knowledge_git_repo
    op.drop_index(
        op.f('ix_ai_knowledge_git_repo_public_discovery'),
        table_name='ai_knowledge_git_repo'
    )
    op.drop_column('ai_knowledge_git_repo', 'public_discovery')
