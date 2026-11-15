"""add_web_sessions_and_session_version

Security hardening slice 1. ``user.session_version`` is stamped into every
non-desktop user JWT as ``sv``; existing rows start at 1, and a legacy token
without ``sv`` reads as 0, so every pre-deploy web JWT dies at deploy.
``user_web_session`` / ``user_web_refresh_token`` hold browser refresh
sessions (hashes only).

Revision ID: a7c3e91d4b20
Revises: 39b73f2a5ab4
Create Date: 2026-11-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a7c3e91d4b20'
down_revision = '39b73f2a5ab4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user',
        sa.Column(
            'session_version',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('1'),
        ),
    )

    op.create_table(
        'user_web_session',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('csrf_token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_web_session_user_id'),
        'user_web_session',
        ['user_id'],
        unique=False,
    )

    op.create_table(
        'user_web_refresh_token',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['session_id'], ['user_web_session.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index(
        op.f('ix_user_web_refresh_token_session_id'),
        'user_web_refresh_token',
        ['session_id'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_user_web_refresh_token_session_id'),
        table_name='user_web_refresh_token',
    )
    op.drop_table('user_web_refresh_token')
    op.drop_index(
        op.f('ix_user_web_session_user_id'), table_name='user_web_session'
    )
    op.drop_table('user_web_session')
    op.drop_column('user', 'session_version')
