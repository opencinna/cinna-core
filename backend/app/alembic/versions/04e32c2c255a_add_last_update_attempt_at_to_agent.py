"""add last_update_attempt_at to agent

Records when the automatic bundle-update sweep last *attempted* an apply on an
install. Stamped and committed before ``InstallService.apply_update`` runs, so a
crash mid-apply still records the attempt; paired with
``last_update_status = 'failed'`` it drives the sweep's retry backoff.

Nullable with no backfill: NULL means "never attempted", which the backoff check
treats as eligible.

Note: the autogenerate run for this revision also picked up unrelated drift
(app_agent_route / user_app_agent_route channel columns, session channel_*
columns and indexes, cli_device_login_request timezone-awareness). All of it
belongs to other in-flight work and was removed by hand — this migration adds
exactly one column.

Revision ID: 04e32c2c255a
Revises: e4c1b7d92f08
Create Date: 2026-08-07 17:23:31.743026

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '04e32c2c255a'
down_revision = 'e4c1b7d92f08'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent',
        sa.Column('last_update_attempt_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('agent', 'last_update_attempt_at')
