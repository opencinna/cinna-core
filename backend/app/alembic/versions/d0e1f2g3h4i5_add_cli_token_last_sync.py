"""add cli token last sync timestamp (merges with detach-sessions branch)

Revision ID: d0e1f2g3h4i5
Revises: c9d0e1f2g3h4, e5f4a3b21c87
Create Date: 2026-04-23 10:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd0e1f2g3h4i5'
down_revision = ('c9d0e1f2g3h4', 'e5f4a3b21c87')
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'cli_token',
        sa.Column('last_sync_connected_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column('cli_token', 'last_sync_connected_at')
