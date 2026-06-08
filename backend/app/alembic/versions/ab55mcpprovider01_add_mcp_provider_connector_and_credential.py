"""add mcp_provider credential type, agent2agent connector flag, mcp_token.credential_id

Adds the schema foundation for the Agent-to-Agent MCP Connector feature:

1. ``credentialtype`` enum gains ``MCP_PROVIDER`` (stored as the uppercase member
   name, consistent with every existing label — see aa44agentapi04). Postgres
   requires ``ALTER TYPE … ADD VALUE`` to run OUTSIDE a transaction block on some
   versions, so it is executed first with an autocommit connection before the
   table DDL.
2. ``mcp_connector.is_agent_to_agent`` BOOL NOT NULL DEFAULT false.
3. ``credential.mcp_mode_conversation`` / ``credential.mcp_mode_building``
   BOOL NOT NULL DEFAULT true (per-mode MCP injection applicability).
4. ``mcp_token.credential_id`` UUID NULL FK → credential(id) ON DELETE CASCADE
   (per-connection bound token — RD-2) + btree index.

Revision ID: ab55mcpprovider01
Revises: 2ca38822e945
Create Date: 2026-06-08 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "ab55mcpprovider01"
down_revision = "2ca38822e945"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Enum value. Must run outside a transaction on PG < 12 and is harmless to
    #    autocommit on newer versions. ``IF NOT EXISTS`` keeps it idempotent.
    #    SQLAlchemy serialises the Python enum by member NAME (uppercase), so the
    #    stored label is 'MCP_PROVIDER' (mirrors 'AGENT_API', 'API_TOKEN', etc.).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'MCP_PROVIDER'")

    # 2. Producer flag on the connector.
    op.add_column(
        "mcp_connector",
        sa.Column(
            "is_agent_to_agent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 3. Per-mode MCP injection applicability on the credential.
    op.add_column(
        "credential",
        sa.Column(
            "mcp_mode_conversation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "credential",
        sa.Column(
            "mcp_mode_building",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # 4. Per-connection bound direct token (RD-2).
    op.add_column(
        "mcp_token",
        sa.Column("credential_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_mcp_token_credential_id",
        "mcp_token",
        "credential",
        ["credential_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_mcp_token_credential_id",
        "mcp_token",
        ["credential_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_mcp_token_credential_id", table_name="mcp_token")
    op.drop_constraint("fk_mcp_token_credential_id", "mcp_token", type_="foreignkey")
    op.drop_column("mcp_token", "credential_id")
    op.drop_column("credential", "mcp_mode_building")
    op.drop_column("credential", "mcp_mode_conversation")
    op.drop_column("mcp_connector", "is_agent_to_agent")
    # The 'MCP_PROVIDER' enum value is intentionally left in place: PostgreSQL
    # cannot drop enum values without recreating the type, which is unsafe while
    # rows may reference it. Matches all prior credentialtype-adding migrations.
