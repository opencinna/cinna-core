"""add default sdk fields to user

Revision ID: c8d9e0f1a2b3
Revises: 776395044d2b
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8d9e0f1a2b3'
down_revision = '776395044d2b'
branch_labels = None
depends_on = None


def upgrade():
    # Add default SDK preference fields to user table
    # These are used as defaults when creating new environments
    op.add_column('user', sa.Column('default_sdk_conversation', sa.String(length=50), nullable=True))
    op.add_column('user', sa.Column('default_sdk_building', sa.String(length=50), nullable=True))

    # Set default value for existing users
    op.execute("UPDATE \"user\" SET default_sdk_conversation = 'claude-code/anthropic' WHERE default_sdk_conversation IS NULL")
    op.execute("UPDATE \"user\" SET default_sdk_building = 'claude-code/anthropic' WHERE default_sdk_building IS NULL")


def downgrade():
    op.drop_column('user', 'default_sdk_building')
    op.drop_column('user', 'default_sdk_conversation')
