"""add mcp_transport_session table

Revision ID: a7v8w9x0y1z2
Revises: z6u7v8w9x0y1
Create Date: 2026-03-09 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7v8w9x0y1z2'
down_revision = 'z6u7v8w9x0y1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mcp_transport_session',
        sa.Column('session_id', sa.String(), nullable=False),
        sa.Column('connector_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('session_id'),
        sa.ForeignKeyConstraint(
            ['connector_id'],
            ['mcp_connector.id'],
            ondelete='CASCADE',
        ),
    )
    op.create_index(
        'ix_mcp_transport_session_connector_id',
        'mcp_transport_session',
        ['connector_id'],
    )


def downgrade():
    op.drop_index('ix_mcp_transport_session_connector_id')
    op.drop_table('mcp_transport_session')
