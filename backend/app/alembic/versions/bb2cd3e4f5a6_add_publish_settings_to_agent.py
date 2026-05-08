"""add publish_settings JSON column to agent

Revision ID: bb2cd3e4f5a6
Revises: aa1bc2d3e4f5
Create Date: 2026-05-08 09:00:00.000000

Phase 5 of the install-experience-redesign plan: persist a per-spec
``provided_by`` override map on the publisher install. The override map
lives under ``publish_settings.credential_overrides[<spec_name>]``; the
publish-time spec collector consults it before falling back to inference
from ``Credential.allow_sharing``.

The column is JSON, defaulted to an empty dict so older rows are
naturally readable without a backfill. Downgrade drops the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "bb2cd3e4f5a6"
down_revision = "aa1bc2d3e4f5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent",
        sa.Column(
            "publish_settings",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )


def downgrade():
    op.drop_column("agent", "publish_settings")
