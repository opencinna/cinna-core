"""add allow_env_panel to guest shares

Revision ID: x4s5t6u7v8w9
Revises: 1cfe565b5e39
Create Date: 2026-03-06

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'x4s5t6u7v8w9'
down_revision = '1cfe565b5e39'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agent_guest_share', sa.Column('allow_env_panel', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('agent_guest_share', 'allow_env_panel')
