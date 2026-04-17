"""add desktop auth tables

Revision ID: d3e4f5a6b7c8
Revises: 2c222ba66e57
Create Date: 2026-04-16 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3e4f5a6b7c8'
down_revision = '2c222ba66e57'
branch_labels = None
depends_on = None


def upgrade():
    # Create desktop_oauth_client table
    op.create_table(
        'desktop_oauth_client',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('device_name', sa.String(length=200), nullable=False),
        sa.Column('platform', sa.String(length=50), nullable=True),
        sa.Column('app_version', sa.String(length=50), nullable=True),
        sa.Column('is_revoked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_desktop_oauth_client_client_id',
        'desktop_oauth_client',
        ['client_id'],
        unique=True,
    )
    op.create_index(
        'ix_desktop_oauth_client_user_id',
        'desktop_oauth_client',
        ['user_id'],
        unique=False,
    )

    # Create desktop_refresh_token table
    op.create_table(
        'desktop_refresh_token',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('client_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('token_family', sa.Uuid(), nullable=False),
        sa.Column('is_revoked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['desktop_oauth_client.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_desktop_refresh_token_hash',
        'desktop_refresh_token',
        ['token_hash'],
        unique=True,
    )
    op.create_index(
        'ix_desktop_refresh_token_client_id',
        'desktop_refresh_token',
        ['client_id'],
        unique=False,
    )
    op.create_index(
        'ix_desktop_refresh_token_family',
        'desktop_refresh_token',
        ['token_family'],
        unique=False,
    )

    # Create desktop_auth_code table
    op.create_table(
        'desktop_auth_code',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code_hash', sa.String(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('code_challenge', sa.String(length=128), nullable=False),
        sa.Column('redirect_uri', sa.String(length=255), nullable=False),
        sa.Column('is_used', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_desktop_auth_code_hash',
        'desktop_auth_code',
        ['code_hash'],
        unique=True,
    )


def downgrade():
    # Drop in reverse FK dependency order
    op.drop_index('ix_desktop_auth_code_hash', table_name='desktop_auth_code')
    op.drop_table('desktop_auth_code')

    op.drop_index('ix_desktop_refresh_token_family', table_name='desktop_refresh_token')
    op.drop_index('ix_desktop_refresh_token_client_id', table_name='desktop_refresh_token')
    op.drop_index('ix_desktop_refresh_token_hash', table_name='desktop_refresh_token')
    op.drop_table('desktop_refresh_token')

    op.drop_index('ix_desktop_oauth_client_user_id', table_name='desktop_oauth_client')
    op.drop_index('ix_desktop_oauth_client_client_id', table_name='desktop_oauth_client')
    op.drop_table('desktop_oauth_client')
