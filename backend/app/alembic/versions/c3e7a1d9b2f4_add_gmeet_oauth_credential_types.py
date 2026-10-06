"""add gmeet_oauth and gmeet_oauth_readonly to credential_type enum

Revision ID: c3e7a1d9b2f4
Revises: a7f55e82a224
Create Date: 2026-10-06 12:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c3e7a1d9b2f4'
down_revision = 'a7f55e82a224'
branch_labels = None
depends_on = None


def upgrade():
    # Google Meet OAuth credential types. SQLAlchemy stores the Python enum
    # member name (uppercase), matching a52c4af4a9e5 (GCALENDAR_OAUTH, ...).
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'GMEET_OAUTH'")
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'GMEET_OAUTH_READONLY'")


def downgrade():
    # PostgreSQL does not support removing enum values directly; left as a
    # no-op, matching the other credentialtype-adding migrations.
    pass
