"""add schedules JSON column to agent_bundle_revision

Revision ID: cd4ef5a6b7c8
Revises: ad1f9c2e4b73
Create Date: 2026-05-24 10:00:00.000000

Bundle-propagated agent schedulers (P1): snapshot the publisher install's
``AgentSchedule`` rows into the bundle revision so consumer installs receive
the publisher's schedules pre-populated. Each entry is
``{name, cron_string, description, prompt, schedule_type, command, enabled}``;
``next_execution`` / ``last_execution`` are never snapshotted.

The column is JSON, defaulted to an empty list so revisions published before
this field existed are naturally readable without a backfill. Downgrade drops
the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "cd4ef5a6b7c8"
down_revision = "ad1f9c2e4b73"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_bundle_revision",
        sa.Column(
            "schedules",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade():
    op.drop_column("agent_bundle_revision", "schedules")
