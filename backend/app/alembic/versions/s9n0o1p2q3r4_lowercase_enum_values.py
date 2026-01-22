"""lowercase enum values in database

Revision ID: s9n0o1p2q3r4
Revises: a9a79425daf2
Create Date: 2026-01-22

Converts uppercase enum names stored in database to lowercase enum values.
This aligns database values with Python enum values after switching from
PostgreSQL native enums to VARCHAR columns.

Affected tables:
- ai_credential.type: ANTHROPIC -> anthropic, MINIMAX -> minimax, OPENAI_COMPATIBLE -> openai_compatible
- agent_access_tokens.mode: CONVERSATION -> conversation, BUILDING -> building
- agent_access_tokens.scope: LIMITED -> limited, GENERAL -> general
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 's9n0o1p2q3r4'
down_revision = 'a9a79425daf2'
branch_labels = None
depends_on = None


def upgrade():
    # Lowercase ai_credential.type
    op.execute("UPDATE ai_credential SET type = LOWER(type) WHERE type != LOWER(type)")

    # Lowercase agent_access_tokens.mode and scope
    op.execute("UPDATE agent_access_tokens SET mode = LOWER(mode) WHERE mode != LOWER(mode)")
    op.execute("UPDATE agent_access_tokens SET scope = LOWER(scope) WHERE scope != LOWER(scope)")


def downgrade():
    # Convert back to uppercase enum names
    op.execute("UPDATE ai_credential SET type = UPPER(type) WHERE type != UPPER(type)")
    op.execute("UPDATE agent_access_tokens SET mode = UPPER(mode) WHERE mode != UPPER(mode)")
    op.execute("UPDATE agent_access_tokens SET scope = UPPER(scope) WHERE scope != UPPER(scope)")
