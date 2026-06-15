"""add email confirmation fields to user

Revision ID: 61220fd330c3
Revises: 3a52a997a322
Create Date: 2026-06-14 20:59:26.910804

Adds the email-confirmation marker and cooldown anchors to ``user``.

Backfill policy (D9): this feature ships onto a live instance with existing,
legitimately-active users. Defaulting them to unconfirmed would immediately
break their notifications, agent email replies, and clamp their agent limit
to 5 — a regression for trusted existing accounts. The anti-abuse target is
*new* signups on a public server. So ``email_confirmed`` is added with
``server_default true`` (backfilling all existing rows to confirmed), then
the server default is switched to ``false`` so NEW inserts default to
unconfirmed at the DB level too. ``email_confirmed_at`` is backfilled to
``now()`` for the existing rows for UI consistency.

The autogenerate run also surfaced unrelated, pre-existing type-reflection
drift on ai_credential / credential / mcp_connector / user_trusted_device.
That drift is intentionally NOT included here — this migration only adds the
four new columns.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '61220fd330c3'
down_revision = '3a52a997a322'
branch_labels = None
depends_on = None


def upgrade():
    # 1) Add email_confirmed NOT NULL with server_default true so every
    #    existing row is backfilled to confirmed.
    op.add_column(
        'user',
        sa.Column(
            'email_confirmed',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # 2) Switch the server default to false so NEW rows are unconfirmed at the
    #    DB level too — only the backfill of existing rows above is confirmed.
    op.alter_column('user', 'email_confirmed', server_default=sa.false())

    op.add_column('user', sa.Column('email_confirmed_at', sa.DateTime(), nullable=True))
    op.add_column('user', sa.Column('last_confirmation_email_sent_at', sa.DateTime(), nullable=True))
    op.add_column('user', sa.Column('last_password_recovery_email_sent_at', sa.DateTime(), nullable=True))

    # 3) Backfill email_confirmed_at = now() for the rows we just confirmed
    #    (UI consistency — they all became confirmed in this migration).
    op.execute(
        "UPDATE \"user\" SET email_confirmed_at = NOW() "
        "WHERE email_confirmed = true AND email_confirmed_at IS NULL"
    )


def downgrade():
    op.drop_column('user', 'last_password_recovery_email_sent_at')
    op.drop_column('user', 'last_confirmation_email_sent_at')
    op.drop_column('user', 'email_confirmed_at')
    op.drop_column('user', 'email_confirmed')
