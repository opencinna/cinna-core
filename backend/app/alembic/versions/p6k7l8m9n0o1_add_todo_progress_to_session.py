"""add todo_progress to session and input_task

Revision ID: p6k7l8m9n0o1
Revises: o5j6k7l8m9n0
Create Date: 2026-01-18

Adds todo_progress JSON field to session and input_task tables for tracking TodoWrite
tool progress during agent execution. This enables real-time progress display in the
Tasks view with persistence for page refreshes.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'p6k7l8m9n0o1'
down_revision = 'o5j6k7l8m9n0'
branch_labels = None
depends_on = None


def upgrade():
    # Add todo_progress JSON column to session - stores list of TodoItem dicts from TodoWrite tool
    # Structure: [{"content": str, "activeForm": str, "status": "pending"|"in_progress"|"completed"}, ...]
    op.add_column('session', sa.Column('todo_progress', sa.JSON(), nullable=True))

    # Add todo_progress JSON column to input_task - persists for task-level display
    op.add_column('input_task', sa.Column('todo_progress', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('input_task', 'todo_progress')
    op.drop_column('session', 'todo_progress')
