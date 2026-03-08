"""add webapp_share_id to session

Revision ID: z6u7v8w9x0y1
Revises: y5t6u7v8w9x0
Create Date: 2026-03-08 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'z6u7v8w9x0y1'
down_revision = 'y5t6u7v8w9x0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'session',
        sa.Column('webapp_share_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_session_webapp_share_id',
        'session',
        'agent_webapp_share',
        ['webapp_share_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_session_webapp_share_id',
        'session',
        ['webapp_share_id'],
    )


def downgrade():
    op.drop_index('ix_session_webapp_share_id', table_name='session')
    op.drop_constraint('fk_session_webapp_share_id', 'session', type_='foreignkey')
    op.drop_column('session', 'webapp_share_id')
