"""add last_update_status to agent

Revision ID: a9a79425daf2
Revises: r8m9n0o1p2q3
Create Date: 2026-01-22 07:57:16.715078

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = 'a9a79425daf2'
down_revision = 'r8m9n0o1p2q3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent', sa.Column('last_update_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade():
    op.drop_column('agent', 'last_update_status')
