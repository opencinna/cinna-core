"""add agent_api to credential_type enum

Revision ID: aa44agentapi04
Revises: aa33agentapi03
Create Date: 2026-05-25 11:05:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'aa44agentapi04'
down_revision = 'aa33agentapi03'
branch_labels = None
depends_on = None


def upgrade():
    # Add 'AGENT_API' value to the credentialtype Postgres enum.
    # SQLAlchemy's default Enum serialisation uses the Python enum *member name*
    # (uppercase) as the stored value, consistent with existing members like
    # 'API_TOKEN' and 'SSH_KEY'. Mirrors 774f47bf7fdd / f8a91b2c3e4d.
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'AGENT_API'")


def downgrade():
    # PostgreSQL does not support removing enum values directly.
    # Rolling back would require recreating the enum type, which is risky while
    # rows of this type may still exist. Left as a no-op, matching the pattern
    # used by other credentialtype-adding migrations.
    pass
