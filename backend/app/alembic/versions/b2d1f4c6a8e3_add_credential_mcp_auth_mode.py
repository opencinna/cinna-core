"""add credential.mcp_auth_mode

Adds a cheap, non-secret ``mcp_auth_mode`` discriminator to ``credential`` so the
UI tab classifier can tell an auto-managed agent-to-agent MCP connection
("agent2agent" → Automatic Credentials) from a manually-added external MCP server
(none / fixed_token / oauth_dcr → My Credentials) without decrypting the blob.

Backfill: existing ``mcp_provider`` rows that have a bound direct ``mcp_token``
(``mcp_token.credential_id`` set) are agent-to-agent connections → "agent2agent".
All other ``mcp_provider`` rows (external) are left NULL, which the classifier
treats as "mine". Non-mcp rows stay NULL.

Revision ID: b2d1f4c6a8e3
Revises: e5f972e7e32e
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2d1f4c6a8e3"
down_revision = "e5f972e7e32e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "credential",
        sa.Column("mcp_auth_mode", sa.String(), nullable=True),
    )
    # Backfill agent-to-agent connections (those with a bound direct token).
    # The credentialtype enum label is stored UPPERCASE in pg_enum.
    op.execute(
        """
        UPDATE credential
        SET mcp_auth_mode = 'agent2agent'
        WHERE type = 'MCP_PROVIDER'
          AND id IN (
            SELECT credential_id FROM mcp_token
            WHERE credential_id IS NOT NULL
          )
        """
    )


def downgrade():
    op.drop_column("credential", "mcp_auth_mode")
