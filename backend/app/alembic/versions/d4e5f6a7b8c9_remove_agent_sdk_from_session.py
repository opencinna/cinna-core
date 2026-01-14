"""remove agent_sdk from session

Revision ID: d4e5f6a7b8c9
Revises: c8d9e0f1a2b3
Create Date: 2026-01-14

The agent_sdk field is no longer needed on sessions because:
- SDK selection is now determined by the environment (agent_sdk_conversation, agent_sdk_building)
- Agent-env detects SDK settings files at runtime
- No need to pass agent_sdk through requests anymore
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    # Remove agent_sdk column from session table
    # This field is no longer used - SDK selection is now determined by environment settings
    op.drop_column('session', 'agent_sdk')


def downgrade():
    # Re-add agent_sdk column if needed
    op.add_column('session', sa.Column('agent_sdk', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='claude'))
    # Remove the server default after adding the column
    op.alter_column('session', 'agent_sdk', server_default=None)
