"""add user_trusted_device

Revision ID: 465c41b435ab
Revises: 581dd9e44be1
Create Date: 2026-06-05 20:47:42.065724

Adds the ``user_trusted_device`` table backing the "Do not ask on this
device" 2FA-skip sub-feature. One row per trusted device per user; the
plaintext token is never stored — only a bcrypt hash in ``token_hash``.

Backfill: none — feature is opt-in per device.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '465c41b435ab'
down_revision = '581dd9e44be1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_trusted_device',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'label',
            sqlmodel.sql.sqltypes.AutoString(length=256),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_user_trusted_device_user_expires',
        'user_trusted_device',
        ['user_id', 'expires_at'],
        unique=False,
    )
    op.create_index(
        op.f('ix_user_trusted_device_user_id'),
        'user_trusted_device',
        ['user_id'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_user_trusted_device_user_id'),
        table_name='user_trusted_device',
    )
    op.drop_index(
        'ix_user_trusted_device_user_expires',
        table_name='user_trusted_device',
    )
    op.drop_table('user_trusted_device')
