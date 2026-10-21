"""input_task session_owned

A Cinna Task Protocol task whose own conversation claimed it (tasks_create
category active, or an attaching tasks_report) settles by reports only.

Revision ID: d26d5d827d17
Revises: 6d9138d0019c
Create Date: 2026-10-21 12:27:40.601582

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd26d5d827d17'
down_revision = '6d9138d0019c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'input_task',
        sa.Column('session_owned', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade():
    op.drop_column('input_task', 'session_owned')
