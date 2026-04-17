"""add desktop auth request table

Revision ID: d7e34bcff709
Revises: d3e4f5a6b7c8
Create Date: 2026-04-16 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7e34bcff709'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    # Create desktop_auth_request table
    op.create_table(
        'desktop_auth_request',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('nonce_hash', sa.String(), nullable=False),
        sa.Column('device_name', sa.String(length=200), nullable=True),
        sa.Column('platform', sa.String(length=50), nullable=True),
        sa.Column('app_version', sa.String(length=50), nullable=True),
        sa.Column('client_id', sa.String(length=64), nullable=True),
        sa.Column('code_challenge', sa.String(length=128), nullable=False),
        sa.Column('redirect_uri', sa.String(length=255), nullable=False),
        sa.Column('state', sa.String(length=255), nullable=False),
        sa.Column('is_used', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_desktop_auth_request_nonce_hash',
        'desktop_auth_request',
        ['nonce_hash'],
        unique=True,
    )
    op.create_index(
        'ix_desktop_auth_request_expires_at',
        'desktop_auth_request',
        ['expires_at'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_desktop_auth_request_expires_at', table_name='desktop_auth_request')
    op.drop_index('ix_desktop_auth_request_nonce_hash', table_name='desktop_auth_request')
    op.drop_table('desktop_auth_request')
