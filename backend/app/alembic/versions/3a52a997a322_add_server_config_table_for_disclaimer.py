"""add server_config table for disclaimer

Revision ID: 3a52a997a322
Revises: c1a4b2d3e5f6
Create Date: 2026-06-14 19:18:23.424071

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '3a52a997a322'
down_revision = 'c1a4b2d3e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'server_config',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('disclaimer_enabled', sa.Boolean(), nullable=False),
        sa.Column('disclaimer_markdown', sa.Text(), nullable=False),
        sa.Column('disclaimer_display_mode', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('disclaimer_version', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['updated_by_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('server_config')
