"""add input_task_files table

Revision ID: q7l8m9n0o1p2
Revises: p6k7l8m9n0o1
Create Date: 2026-01-20

Adds input_task_files junction table for linking file uploads to input tasks.
This enables file attachments to be included when tasks are executed.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'q7l8m9n0o1p2'
down_revision = 'p6k7l8m9n0o1'
branch_labels = None
depends_on = None


def upgrade():
    # Create input_task_files junction table
    op.create_table('input_task_files',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('file_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['input_task.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['file_uploads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    # Add indexes for efficient lookup
    op.create_index('ix_input_task_files_task_id', 'input_task_files', ['task_id'])
    op.create_index('ix_input_task_files_file_id', 'input_task_files', ['file_id'])


def downgrade():
    op.drop_index('ix_input_task_files_file_id', table_name='input_task_files')
    op.drop_index('ix_input_task_files_task_id', table_name='input_task_files')
    op.drop_table('input_task_files')
