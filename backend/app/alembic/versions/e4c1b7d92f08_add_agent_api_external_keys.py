"""add agent_api external keys (token kind/subject/expiry + producer opt-in)

Backs the "External Keys" mode of the Agent REST API (plan D1). One migration
for all four columns:

- ``agent_api_token.kind``            — "connection" | "external". Plain
  VARCHAR(20), deliberately NOT a Postgres native enum: ``ALTER TYPE`` is
  non-transactional and adding a value later is a migration hazard. Added with a
  ``server_default`` so existing rows backfill to ``connection``, then the
  default is dropped so the model-level ``Field(default=...)`` is the single
  source of truth (same pattern as ``agent_api_identity_enabled`` in
  ``25a74abc7f4a``).
- ``agent_api_token.subject_user_id`` — the platform user an external key acts
  as; NULL for connection tokens. ``ON DELETE CASCADE`` (a deleted user's keys
  go with them), indexed.
- ``agent_api_token.expires_at``      — optional expiry for external keys
  (TIMESTAMPTZ, NULL = never).
- ``agent.agent_api_external_access_enabled`` — producer opt-in that must be on
  before a key can be minted. Same server_default-then-drop treatment.

Revision ID: e4c1b7d92f08
Revises: 878bc3f6579f
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e4c1b7d92f08"
down_revision = "878bc3f6579f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_api_token",
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
            server_default="connection",
        ),
    )
    op.alter_column("agent_api_token", "kind", server_default=None)

    op.add_column(
        "agent_api_token",
        sa.Column("subject_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_api_token_subject_user_id_user",
        "agent_api_token",
        "user",
        ["subject_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_agent_api_token_subject_user_id"),
        "agent_api_token",
        ["subject_user_id"],
        unique=False,
    )

    op.add_column(
        "agent_api_token",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "agent",
        sa.Column(
            "agent_api_external_access_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("agent", "agent_api_external_access_enabled", server_default=None)


def downgrade():
    op.drop_column("agent", "agent_api_external_access_enabled")
    op.drop_column("agent_api_token", "expires_at")
    op.drop_index(
        op.f("ix_agent_api_token_subject_user_id"), table_name="agent_api_token"
    )
    op.drop_constraint(
        "fk_agent_api_token_subject_user_id_user",
        "agent_api_token",
        type_="foreignkey",
    )
    op.drop_column("agent_api_token", "subject_user_id")
    op.drop_column("agent_api_token", "kind")
