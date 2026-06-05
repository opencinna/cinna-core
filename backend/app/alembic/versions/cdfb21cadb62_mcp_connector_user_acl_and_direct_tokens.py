"""mcp_connector_user_acl_and_direct_tokens

Revision ID: cdfb21cadb62
Revises: 465c41b435ab
Create Date: 2026-06-05 21:43:38.993403

Adds the user-id ACL and direct-token columns to the MCP connector feature:
- mcp_connector.allowed_user_ids (JSON) — exact platform-user ACL
- mcp_connector.allow_token_access (bool) — gates direct-token generation
- mcp_token.label (str, nullable) — name for direct tokens
- mcp_token.last_used_at (datetime, nullable) — "last used" display parity

Also performs a best-effort backfill of allowed_user_ids from allowed_emails:
for each connector, any allowed_email matching a user (case-insensitive) gets
that user's id appended to allowed_user_ids. Unmatched emails stay in
allowed_emails so they keep working via the fallback ACL.
"""
import json

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = 'cdfb21cadb62'
down_revision = '465c41b435ab'
branch_labels = None
depends_on = None


def upgrade():
    # --- mcp_connector columns ---
    op.add_column(
        'mcp_connector',
        sa.Column('allowed_user_ids', sa.JSON(), nullable=False, server_default='[]'),
    )
    op.add_column(
        'mcp_connector',
        sa.Column(
            'allow_token_access',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Drop the server defaults — the model supplies them at the application
    # layer (matches the convention of existing columns).
    op.alter_column('mcp_connector', 'allowed_user_ids', server_default=None)
    op.alter_column('mcp_connector', 'allow_token_access', server_default=None)

    # --- mcp_token columns ---
    op.add_column(
        'mcp_token',
        sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        'mcp_token',
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
    )

    # --- best-effort email -> user_id backfill ---
    _backfill_allowed_user_ids()


def _backfill_allowed_user_ids():
    """Map each connector's allowed_emails to user ids where a user exists."""
    bind = op.get_bind()

    connectors = bind.execute(
        sa.text(
            "SELECT id, allowed_emails, allowed_user_ids FROM mcp_connector"
        )
    ).fetchall()

    for row in connectors:
        connector_id, allowed_emails, allowed_user_ids = row

        # JSON columns may come back as python objects or json strings depending
        # on the driver — normalise both.
        emails = _as_list(allowed_emails)
        existing_ids = _as_list(allowed_user_ids)
        if not emails:
            continue

        existing_id_set = {str(u) for u in existing_ids}
        new_ids = list(existing_ids)

        for email in emails:
            if not email:
                continue
            user_row = bind.execute(
                sa.text("SELECT id FROM \"user\" WHERE lower(email) = lower(:email)"),
                {"email": email},
            ).fetchone()
            if user_row:
                user_id = str(user_row[0])
                if user_id not in existing_id_set:
                    new_ids.append(user_id)
                    existing_id_set.add(user_id)

        if len(new_ids) != len(existing_ids):
            bind.execute(
                sa.text(
                    "UPDATE mcp_connector SET allowed_user_ids = :ids WHERE id = :cid"
                ),
                {"ids": json.dumps(new_ids), "cid": str(connector_id)},
            )


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def downgrade():
    op.drop_column('mcp_token', 'last_used_at')
    op.drop_column('mcp_token', 'label')
    op.drop_column('mcp_connector', 'allow_token_access')
    op.drop_column('mcp_connector', 'allowed_user_ids')
