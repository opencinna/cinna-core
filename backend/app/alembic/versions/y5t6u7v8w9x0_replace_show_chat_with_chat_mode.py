"""replace show_chat with chat_mode in webapp interface config

Revision ID: y5t6u7v8w9x0
Revises: d79041738628
Create Date: 2026-03-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'y5t6u7v8w9x0'
down_revision = 'd79041738628'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_webapp_interface_config',
        sa.Column('chat_mode', sa.String(length=20), nullable=True),
    )
    op.drop_column('agent_webapp_interface_config', 'show_chat')


def downgrade():
    op.add_column(
        'agent_webapp_interface_config',
        sa.Column('show_chat', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_column('agent_webapp_interface_config', 'chat_mode')
