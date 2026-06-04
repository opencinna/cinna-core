"""add agent status_refresh_command

Revision ID: 1a43b403f066
Revises: 9675dc695735
Create Date: 2026-06-04 05:06:31.769211

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1a43b403f066'
down_revision = '9675dc695735'
branch_labels = None
depends_on = None


def upgrade():
    # Additive: existing rows backfill to "/run:status" via the server default.
    op.add_column(
        'agent',
        sa.Column(
            'status_refresh_command',
            sa.String(length=1024),
            server_default='/run:status',
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('agent', 'status_refresh_command')
