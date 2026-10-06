"""add chain_depth to input_task

Revision ID: e5a9c3d7f2b4
Revises: d4f8b2c6e1a3
Create Date: 2026-10-06 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5a9c3d7f2b4'
down_revision = 'd4f8b2c6e1a3'
branch_labels = None
depends_on = None


def upgrade():
    # Cinna Task Protocol chain depth of a tool-created task (1 = created by an
    # agent in a chat with no task, 2 = from work that was itself delegated).
    # NULL for tasks a person created and for rows from before the protocol.
    op.add_column('input_task', sa.Column('chain_depth', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('input_task', 'chain_depth')
