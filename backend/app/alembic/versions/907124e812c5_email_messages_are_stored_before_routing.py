"""email messages are stored before routing

Revision ID: 907124e812c5
Revises: ca0192122e0c
Create Date: 2026-08-25 02:24:34.615961

``email_message.agent_id`` was NOT NULL because the only writer — the old
per-agent email integration — always knew the agent before it stored anything.
The email *channel* transport does not: a channel message is classified after
it arrives, and the arrivals most worth a durable record are exactly the ones
classification never sees (a sender denied by the whitelist, by the channel
policy, or by user resolution). Those declines are deliberately silent to the
sender on a polled transport, and their only other trace is
``ChannelDebugBuffer``, which is in-memory and process-local — so before this
change a denied sender left nothing at all behind a restart.

The row is therefore written on arrival with no agent, and
``EmailChannelAdapter.record_routing_outcome`` stamps ``agent_id`` and
``session_id`` afterwards if routing produces them. NULL means "arrived, not
routed", not "missing data".

Autogenerate additionally proposed three ``alter_column`` operations on
``cli_device_login_request`` (``last_polled_at``, ``created_at``,
``expires_at``, TIMESTAMP(timezone=True) → DateTime). They are a known
type-affinity artifact — the model declares a plain ``datetime`` while the live
column is timezone-aware — unrelated to this change, and stripped from both
directions.

This migration is deliberately the one ALTER and nothing else. The downgrade is
the same ALTER in reverse and will therefore **fail on a database that has
unrouted rows** — which is any database that has polled an email channel. That
is left as an honest failure rather than papered over with a DELETE: restoring
NOT NULL means destroying exactly the audit records this change exists to keep,
and that is an operator's decision to take explicitly, not a side effect of
`alembic downgrade`.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '907124e812c5'
down_revision = 'ca0192122e0c'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('email_message', 'agent_id',
               existing_type=sa.UUID(),
               nullable=True)


def downgrade():
    op.alter_column('email_message', 'agent_id',
               existing_type=sa.UUID(),
               nullable=False)
