"""Add durable task delegation metadata and structured results.

Revision ID: ba517c930def
Revises: 32563e0d9399
"""
from alembic import op
import sqlalchemy as sa

revision = 'ba517c930def'
down_revision = '32563e0d9399'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('input_task', sa.Column('delegation_metadata', sa.JSON(), nullable=True))
    op.add_column('input_task', sa.Column('delegation_result', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('input_task', 'delegation_result')
    op.drop_column('input_task', 'delegation_metadata')
