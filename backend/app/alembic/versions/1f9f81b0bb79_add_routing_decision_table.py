"""add routing_decision table

Creates the one table behind Auto Routing Tuning — the durable record of a
routing decision that ``RoutingTrace`` captures in memory (see
``docs/plans/auto_routing_tuning_plan.md`` §4).

Additive only: no existing table is touched and there is no backfill. Rows are
disposable diagnostics — ``routing_trace_scheduler`` purges them past
``ROUTING_TRACE_RETENTION_DAYS`` — so nothing here needs to survive a rollback.

Notes:

- ``origin`` / ``outcome`` / ``match_method`` are plain ``VARCHAR``, not
  Postgres enums, so a new vocabulary value needs no migration (same rationale
  as ``session.status`` and ``channel_thread_binding.status``).
- ``stages`` is ``JSONB`` defaulting to ``'[]'::jsonb``. It is read whole and
  never queried by inner field; child tables would fossilise today's two-pass
  router shape. Precedent: ``input_task.refinement_history``.
- Cascade behaviour is deliberate and asymmetric. ``channel_id`` CASCADEs — a
  channel's diagnostics mean nothing without the channel. Everything else SET
  NULLs: deleting the sender, the admin who ran a simulate, or the agent/bundle
  a decision landed on must not delete the evidence *about* that decision.
- The three ``created_at DESC`` indexes back the admin list and its two
  filters; the ``message_sha256`` index is the replay/dedupe lookup that still
  works when ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is off.

Autogenerate also reported pre-existing local drift unrelated to this feature
(``cli_device_login_request`` timestamp types — the *model* is wrong there, not
the database). All of it was removed from this migration: it belongs to
whichever change fixes that model, not here.

Revision ID: 1f9f81b0bb79
Revises: 1a17557c0311
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "1f9f81b0bb79"
down_revision = "1a17557c0311"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "routing_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("thread_key", sa.String(length=512), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("message_sha256", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        # "How the last stage matched", NOT "how the decision was reached" — it
        # deliberately survives a no_match. See the model's column docs.
        sa.Column("match_method", sa.String(length=32), nullable=True),
        sa.Column("selected_agent_id", sa.Uuid(), nullable=True),
        sa.Column("selected_bundle_uuid", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "stages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["server_channel.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["selected_agent_id"], ["agent.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["selected_bundle_uuid"], ["agent_bundle.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The unfiltered admin list ("recent decisions, newest first").
    op.create_index(
        "ix_routing_decision_created",
        "routing_decision",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )
    # Filtered by channel — the common case on the tuning card.
    op.create_index(
        "ix_routing_decision_channel_created",
        "routing_decision",
        ["channel_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    # "Everything we routed for this sender."
    op.create_index(
        "ix_routing_decision_user_created",
        "routing_decision",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    # Replay/dedupe lookup by message identity, text gate or not.
    op.create_index(
        "ix_routing_decision_message_sha256",
        "routing_decision",
        ["message_sha256"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_routing_decision_message_sha256", table_name="routing_decision")
    op.drop_index("ix_routing_decision_user_created", table_name="routing_decision")
    op.drop_index("ix_routing_decision_channel_created", table_name="routing_decision")
    op.drop_index("ix_routing_decision_created", table_name="routing_decision")
    op.drop_table("routing_decision")
