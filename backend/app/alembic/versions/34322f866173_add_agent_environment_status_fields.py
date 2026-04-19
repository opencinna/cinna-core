"""add agent environment status fields

Revision ID: 34322f866173
Revises: d7e34bcff709
Create Date: 2026-04-19 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '34322f866173'
down_revision = 'd7e34bcff709'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent_environment', sa.Column('status_file_raw', sa.Text(), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_severity', sa.String(length=16), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_summary', sa.String(length=512), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_reported_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_reported_at_source', sa.String(length=16), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_fetched_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_prev_severity', sa.String(length=16), nullable=True))
    op.add_column('agent_environment', sa.Column('status_file_severity_changed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('agent_environment', 'status_file_severity_changed_at')
    op.drop_column('agent_environment', 'status_file_prev_severity')
    op.drop_column('agent_environment', 'status_file_fetched_at')
    op.drop_column('agent_environment', 'status_file_reported_at_source')
    op.drop_column('agent_environment', 'status_file_reported_at')
    op.drop_column('agent_environment', 'status_file_summary')
    op.drop_column('agent_environment', 'status_file_severity')
    op.drop_column('agent_environment', 'status_file_raw')
