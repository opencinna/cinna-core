"""add_credential_share_source

Revision ID: 3c3c37a5e144
Revises: 65d1ef4899be
Create Date: 2026-06-24 07:07:35.187532

Adds a nullable provenance marker to ``credential_shares``. Existing rows
backfill to NULL (read as "direct" everywhere). No index, no FK, no data
backfill — the column is read only after enumerating a user's shares, which
is already indexed and small per user.

NOTE: Alembic autogen also surfaced unrelated TIMESTAMP→DateTime type drift on
``cli_device_login_request`` columns; that drift is intentionally NOT applied
here (this migration captures only the new ``source`` column).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '3c3c37a5e144'
down_revision = '65d1ef4899be'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'credential_shares',
        sa.Column('source', sa.String(length=20), nullable=True),
    )


def downgrade():
    op.drop_column('credential_shares', 'source')
