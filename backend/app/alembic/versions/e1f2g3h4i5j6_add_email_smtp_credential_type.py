"""add_email_smtp_to_credential_type_enum

Revision ID: e1f2g3h4i5j6
Revises: 4d769edd79d2
Create Date: 2026-03-17 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'e1f2g3h4i5j6'
down_revision = '4d769edd79d2'
branch_labels = None
depends_on = None


def upgrade():
    # Add 'EMAIL_SMTP' value to the credentialtype enum
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'EMAIL_SMTP'")


def downgrade():
    # Note: PostgreSQL does not support removing enum values directly.
    # Recreating the enum type without the value would require a full table rewrite.
    # For safety, this is a no-op — remove credentials of this type manually before downgrading.
    pass
