"""add user details columns

Revision ID: 6e6af979678c
Revises: 61220fd330c3
Create Date: 2026-06-18 19:42:42.575259

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '6e6af979678c'
down_revision = '61220fd330c3'
branch_labels = None
depends_on = None


def upgrade():
    # User's Details feature: free-text raw input + normalized parsed map.
    # Both nullable, no server_default — NULL means "no details".
    # `details_parsed` is JSONB (not JSON) so the `user` table retains an
    # equality operator — `SELECT DISTINCT "user".*` queries (e.g. identity
    # binding lookups) fail on plain `json`, which has no equality operator.
    op.add_column('user', sa.Column('details_raw', sa.Text(), nullable=True))
    op.add_column('user', sa.Column('details_parsed', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('user', 'details_parsed')
    op.drop_column('user', 'details_raw')
