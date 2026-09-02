"""add_local_agent_kit_enabled_to_server_config

Revision ID: 7de57f5d4b8f
Revises: 1367eb81ac66
Create Date: 2026-09-02 05:42:49.132400

Instance-level opt-out for the public, auth-free Local Agent Kit surface
(`/agent-start` and `/api/agent-start`). Opt-out, not opt-in: the server default keeps the
existing singleton row — and every fresh instance — publishing the kit.

Autogenerate additionally proposed re-typing the three `cli_device_login_request`
timestamp columns from `TIMESTAMP WITH TIME ZONE` to naive `DateTime`. That is a
known false positive: the model is the side that is wrong, the database column is
correct, and narrowing it here would silently drop the offsets. Those statements
were removed by hand.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '7de57f5d4b8f'
down_revision = '1367eb81ac66'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'server_config',
        sa.Column(
            'local_agent_kit_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade():
    op.drop_column('server_config', 'local_agent_kit_enabled')
