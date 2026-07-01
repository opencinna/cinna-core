"""add origin discriminator to agent_bundle_revision

Distinguishes user-facing catalog publish revisions (``publish``) from the
internal git-versioning baselines (``git``) persisted by checkout / pull /
push / connect. The Revisions UI and the publish version-suggestion now list
only ``publish`` rows, while git baselines remain the internal dirty-check SSOT.

Existing rows backfill to ``publish`` via the server default — historical git
baselines are indistinguishable in the DB and stay labelled ``publish`` (only
rows created after this migration carry the correct ``git`` origin).

Revision ID: 878bc3f6579f
Revises: d9b3e1a7c45f
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "878bc3f6579f"
down_revision = "d9b3e1a7c45f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_bundle_revision",
        sa.Column(
            "origin",
            sa.String(length=32),
            nullable=False,
            server_default="publish",
        ),
    )


def downgrade():
    op.drop_column("agent_bundle_revision", "origin")
