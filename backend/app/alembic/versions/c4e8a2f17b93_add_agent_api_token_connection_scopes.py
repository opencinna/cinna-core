"""add_agent_api_token_connection_scopes

A shareable agent API connection carries owner-chosen scopes on its token
row (``agent_api_token.scopes``, connection tokens only). Every call through
the connection gets them on top of the caller's own grant. Existing rows get
``[]``, which is today's behaviour.

Revision ID: c4e8a2f17b93
Revises: a7c3e91d4b20
Create Date: 2026-11-17 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c4e8a2f17b93'
down_revision = 'a7c3e91d4b20'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_api_token',
        sa.Column(
            'scopes',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade():
    op.drop_column('agent_api_token', 'scopes')
