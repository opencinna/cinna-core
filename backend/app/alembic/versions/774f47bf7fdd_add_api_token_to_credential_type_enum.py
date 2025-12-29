"""add_api_token_to_credential_type_enum

Revision ID: 774f47bf7fdd
Revises: e346c86d4373
Create Date: 2025-12-29 11:27:17.897888

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '774f47bf7fdd'
down_revision = 'e346c86d4373'
branch_labels = None
depends_on = None


def upgrade():
    # Add 'API_TOKEN' value to the credentialtype enum
    op.execute("ALTER TYPE credentialtype ADD VALUE 'API_TOKEN'")


def downgrade():
    # Note: PostgreSQL does not support removing enum values directly
    # You would need to recreate the enum type to remove a value
    # For safety, we'll leave this as a no-op
    # If you need to rollback, you'll need to manually handle it
    pass
