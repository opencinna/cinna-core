"""make desktop_oauth_client.origin explicit at insert time

Backfills any NULL ``origin`` and then **drops the standing server default**.

Why the default has to go, given that a previous revision deliberately added it:
the two jobs it was doing are in conflict, and only one of them was wanted.

  1. Give pre-existing rows a true value. Every row created before the CLI
     exchange endpoint really was a browser consent, so that backfill is
     correct — and it is preserved here, as a one-time UPDATE.
  2. Supply a value for any *future* insert that does not state one. This is
     the part that must not survive. ``origin`` is trusted in the direction
     "no cli_exchange badge ⇒ no CLI-minted session", so a creation path that
     forgets to set it would be handed the reassuring answer for free and the
     badge would quietly stop meaning anything.

Removing the Python-side default alone does not achieve (2): SQLModel does not
validate ``table=True`` models on construction, so the attribute is simply
``None``, and the server default then fills it in on INSERT. Verified against a
live session before writing this — the row persisted as ``browser_consent``
with no error anywhere. With the column NOT NULL and no default, the same insert
raises instead, which is the loud failure that was wanted.

Revision ID: c9a2f5b1d604
Revises: b4c1d7e93f28
Create Date: 2026-09-03 12:20:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c9a2f5b1d604'
down_revision = 'b4c1d7e93f28'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE desktop_oauth_client SET origin = 'browser_consent' "
        "WHERE origin IS NULL"
    )
    op.alter_column('desktop_oauth_client', 'origin', server_default=None)


def downgrade():
    op.alter_column(
        'desktop_oauth_client',
        'origin',
        server_default=sa.text("'browser_consent'"),
    )
