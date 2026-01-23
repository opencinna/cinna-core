"""add streaming_started_at to session

Revision ID: t0o1p2q3r4s5
Revises: s9n0o1p2q3r4
Create Date: 2026-01-23

Adds streaming_started_at timestamp field to session table for tracking when
streaming began. This enables the frontend to derive streaming state from the
database rather than relying on in-memory ActiveStreamingManager.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't0o1p2q3r4s5'
down_revision = 's9n0o1p2q3r4'
branch_labels = None
depends_on = None


def upgrade():
    # Add streaming_started_at timestamp column to session
    # Set when streaming starts, cleared when streaming ends
    op.add_column('session', sa.Column('streaming_started_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('session', 'streaming_started_at')
