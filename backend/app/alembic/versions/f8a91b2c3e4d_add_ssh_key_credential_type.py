"""add ssh_key credential type

Revision ID: f8a91b2c3e4d
Revises: d40c20201e5b
Create Date: 2026-04-24 12:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "f8a91b2c3e4d"
down_revision = "d40c20201e5b"
branch_labels = None
depends_on = None


def upgrade():
    # Add 'SSH_KEY' value to the credentialtype Postgres enum.
    # SQLAlchemy's default Enum serialisation uses the Python enum *member name*
    # (uppercase) as the stored value, consistent with existing members like
    # 'API_TOKEN' and 'GOOGLE_SERVICE_ACCOUNT'.
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'SSH_KEY'")


def downgrade():
    # PostgreSQL does not support removing enum values directly.
    # Rolling back would require recreating the enum type, which is risky while
    # rows of this type may still exist. Left as a no-op, matching the pattern
    # used by other credentialtype-adding migrations.
    pass
