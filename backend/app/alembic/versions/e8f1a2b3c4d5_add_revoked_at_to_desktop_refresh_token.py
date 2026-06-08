"""add revoked_at to desktop_refresh_token

Adds a nullable, timezone-aware ``revoked_at`` column to ``desktop_refresh_token``
to support the refresh-token rotation reuse-grace window (OWASP / RFC 9700
§4.14.2). Legacy rows get NULL and therefore fall through to genuine-replay
(family-revocation) behaviour, which is the safe default.

Revision ID: e8f1a2b3c4d5
Revises: ab55mcpprovider01
Create Date: 2026-06-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e8f1a2b3c4d5'
down_revision = 'ab55mcpprovider01'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'desktop_refresh_token',
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column('desktop_refresh_token', 'revoked_at')
