"""add version column to agent_bundle_revision

Revision ID: i7e8f9a0b1c2
Revises: h6d7e8f9a0b1
Create Date: 2026-05-07 13:00:00.000000

Persist a human-friendly version label (e.g. "1.0", "1.1", "2.0") on each
bundle revision. Set by the publisher at publish time; the internal
monotonic ``revision_number`` is unchanged and continues to drive snapshot
paths and ordering. Existing revisions get NULL — the UI falls back to
``v{revision_number}`` for those rows.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "i7e8f9a0b1c2"
down_revision = "h6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_bundle_revision",
        sa.Column("version", sa.String(length=64), nullable=True),
    )


def downgrade():
    op.drop_column("agent_bundle_revision", "version")
