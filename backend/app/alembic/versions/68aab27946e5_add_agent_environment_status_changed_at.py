"""add agent_environment.status_changed_at

Adds the staleness signal the system status-repair scheduler needs to find
environments abandoned in a transitional status ("creating", "building",
"starting", "activating", "rebuilding").

No existing column could serve: ``updated_at`` has no ``onupdate`` and the
lifecycle never writes it, ``last_activity_at`` is bumped by usage-intent, and
``last_health_check`` is written only on a successful probe.

Existing rows are backfilled with ``now()`` via the column's server default, so
no environment looks stale the instant this migration lands — the reconciler
starts its clock at deploy time.

Revision ID: 68aab27946e5
Revises: c9a2f5b1d604
Create Date: 2026-09-05 15:46:54.819528

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '68aab27946e5'
down_revision = 'c9a2f5b1d604'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_environment',
        sa.Column(
            'status_changed_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column('agent_environment', 'status_changed_at')
