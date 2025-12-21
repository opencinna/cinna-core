"""Add Google OAuth fields to User table

Revision ID: a1b2c3d4e5f6
Revises: 9c0a54914c78
Create Date: 2025-12-21 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '9c0a54914c78'
branch_labels = None
depends_on = None


def upgrade():
    # Add google_id column (nullable, unique)
    op.add_column('user', sa.Column('google_id', sa.String(length=255), nullable=True))
    op.create_index(
        op.f('ix_user_google_id'),
        'user',
        ['google_id'],
        unique=True,
        postgresql_where=sa.text('google_id IS NOT NULL')
    )

    # Make hashed_password nullable (OAuth users may not have password)
    op.alter_column('user', 'hashed_password',
                    existing_type=sa.String(),
                    nullable=True)


def downgrade():
    # Restore hashed_password to non-nullable (may fail if OAuth-only users exist)
    op.alter_column('user', 'hashed_password',
                    existing_type=sa.String(),
                    nullable=False)

    # Drop google_id index and column
    op.drop_index(op.f('ix_user_google_id'), table_name='user')
    op.drop_column('user', 'google_id')
