"""add agent_improvement_request table

Creates the one table behind Agent Improvement Requests — a consent-gated,
one-directional share of a frozen session snapshot from the session owner to the
agent's owner (see ``docs/plans/agent_improvement_requests_plan.md`` §3.1/§7).

Additive only: no existing table is touched and there is no backfill.

Notes:

- ``status`` / ``source`` are plain ``VARCHAR`` with server defaults, NOT
  Postgres enums — adding a value later then needs no migration (same rationale
  as ``session.status`` and ``agent_api_token.kind``). The server defaults are
  kept (rather than dropped as in ``e4c1b7d92f08``) because these two columns
  are also the resting state of every row, not a one-time backfill.
- ``snapshot`` / ``context`` default to ``'{}'::json``, mirroring
  ``agent_bundle_revision.manifest``.
- Cascade behaviour is deliberate and asymmetric: the receiving agent and both
  users CASCADE (a deleted recipient makes the row meaningless; a user who
  deletes their account withdraws the data they shared), while the source
  session and source agent SET NULL (the snapshot IS the payload — provenance is
  best-effort).
- ``ix_air_owner_created`` is DESC on ``created_at`` so the CLI's "newest first"
  cross-agent list is a plain index scan.

Autogenerate also reported pre-existing local drift unrelated to this feature
(``session.channel_*``, ``app_agent_route.channels``,
``cli_device_login_request`` timestamp types). All of it was removed from this
migration — it belongs to whichever change introduced it, not here.

Revision ID: 227785421f7a
Revises: 04e32c2c255a
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "227785421f7a"
down_revision = "04e32c2c255a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_improvement_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("source_agent_id", sa.Uuid(), nullable=True),
        sa.Column("target_agent_id", sa.Uuid(), nullable=False),
        sa.Column("bundle_uuid", sa.Uuid(), nullable=True),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'new'"),
            nullable=False,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=16),
            server_default=sa.text("'web_ui'"),
            nullable=False,
        ),
        sa.Column(
            "snapshot",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "context",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_message_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_truncated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["bundle_uuid"], ["agent_bundle.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requester_user_id"], ["user.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_agent_id"], ["agent.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["target_agent_id"], ["agent.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The Configuration-tab card query.
    op.create_index(
        "ix_air_target_status",
        "agent_improvement_request",
        ["target_agent_id", "status"],
        unique=False,
    )
    # The CLI cross-agent list ("everything I own, newest first").
    op.create_index(
        "ix_air_owner_created",
        "agent_improvement_request",
        ["owner_user_id", sa.text("created_at DESC")],
        unique=False,
    )
    # "My submitted requests".
    op.create_index(
        "ix_air_requester",
        "agent_improvement_request",
        ["requester_user_id"],
        unique=False,
    )
    # Per-session rate-limit check.
    op.create_index(
        "ix_air_session",
        "agent_improvement_request",
        ["session_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_air_session", table_name="agent_improvement_request")
    op.drop_index("ix_air_requester", table_name="agent_improvement_request")
    op.drop_index("ix_air_owner_created", table_name="agent_improvement_request")
    op.drop_index("ix_air_target_status", table_name="agent_improvement_request")
    op.drop_table("agent_improvement_request")
