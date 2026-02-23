"""add_google_service_account_to_credential_type_enum

Revision ID: g4b2c3d5e6f7
Revises: f3a1b2c4d5e6
Create Date: 2026-02-23 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'g4b2c3d5e6f7'
down_revision = 'f3a1b2c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    # Add 'GOOGLE_SERVICE_ACCOUNT' value to the credentialtype enum
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'GOOGLE_SERVICE_ACCOUNT'")


def downgrade():
    # Note: PostgreSQL does not support removing enum values directly
    # You would need to recreate the enum type to remove a value
    # For safety, we'll leave this as a no-op
    pass
