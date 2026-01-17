"""add ai credential shares

Revision ID: i9d0e1f2g3h4
Revises: h8c9d0e1f2g3
Create Date: 2026-01-16

Adds ai_credential_shares table for sharing AI credentials between users.
Used when agents are shared with AI credentials provision enabled.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'i9d0e1f2g3h4'
down_revision = 'h8c9d0e1f2g3'
branch_labels = None
depends_on = None


def upgrade():
    # Create ai_credential_shares table
    op.create_table('ai_credential_shares',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('ai_credential_id', sa.Uuid(), nullable=False),
        sa.Column('shared_with_user_id', sa.Uuid(), nullable=False),
        sa.Column('shared_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('shared_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['ai_credential_id'], ['ai_credential.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shared_with_user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shared_by_user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ai_credential_id', 'shared_with_user_id', name='uq_ai_credential_shares_credential_user')
    )

    # Create indexes for ai_credential_shares
    op.create_index(
        op.f('ix_ai_credential_shares_ai_credential_id'),
        'ai_credential_shares',
        ['ai_credential_id'],
        unique=False
    )
    op.create_index(
        op.f('ix_ai_credential_shares_shared_with_user_id'),
        'ai_credential_shares',
        ['shared_with_user_id'],
        unique=False
    )


def downgrade():
    # Drop ai_credential_shares table and indexes
    op.drop_index(op.f('ix_ai_credential_shares_shared_with_user_id'), table_name='ai_credential_shares')
    op.drop_index(op.f('ix_ai_credential_shares_ai_credential_id'), table_name='ai_credential_shares')
    op.drop_table('ai_credential_shares')
