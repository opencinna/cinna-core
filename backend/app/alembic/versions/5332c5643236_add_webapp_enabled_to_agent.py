"""add webapp_enabled to agent

Revision ID: 5332c5643236
Revises: x4s5t6u7v8w9
Create Date: 2026-03-07 09:52:59.944241

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5332c5643236'
down_revision = 'x4s5t6u7v8w9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent', sa.Column('webapp_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade():
    op.drop_column('agent', 'webapp_enabled')
