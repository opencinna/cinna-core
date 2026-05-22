"""add_user_2fa_tables

Adds optional two-factor authentication artefacts:
- Three columns on ``user`` (``two_factor_enabled``,
  ``two_factor_enrolled_at``, ``two_factor_last_used_at``).
- Four new tables: ``user_passkey``, ``user_totp_secret``,
  ``user_recovery_code``, ``user_mfa_challenge``.

See ``docs/drafts/user-2fa-passkeys-totp_plan.md`` for the full design.

Revision ID: 538667612c3d
Revises: dd4ef5a6b7c8
Create Date: 2026-05-21 15:55:09.211406

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '538667612c3d'
down_revision = 'dd4ef5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    # ── user_mfa_challenge ─────────────────────────────────────────────
    op.create_table(
        'user_mfa_challenge',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'challenge_token',
            sqlmodel.sql.sqltypes.AutoString(length=128),
            nullable=False,
        ),
        sa.Column('webauthn_challenge', sa.LargeBinary(), nullable=True),
        sa.Column(
            'first_factor',
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_mfa_challenge_challenge_token'),
        'user_mfa_challenge',
        ['challenge_token'],
        unique=True,
    )
    op.create_index(
        'ix_user_mfa_challenge_user_created',
        'user_mfa_challenge',
        ['user_id', 'created_at'],
        unique=False,
    )
    op.create_index(
        op.f('ix_user_mfa_challenge_user_id'),
        'user_mfa_challenge',
        ['user_id'],
        unique=False,
    )

    # ── user_passkey ───────────────────────────────────────────────────
    op.create_table(
        'user_passkey',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('credential_id', sa.LargeBinary(), nullable=False),
        sa.Column('public_key', sa.LargeBinary(), nullable=False),
        sa.Column('sign_count', sa.Integer(), nullable=False),
        sa.Column(
            'transports', sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column(
            'aaguid',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
        sa.Column(
            'nickname',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column(
            'device_type',
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
        ),
        sa.Column('backed_up', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_passkey_credential_id'),
        'user_passkey',
        ['credential_id'],
        unique=True,
    )
    op.create_index(
        op.f('ix_user_passkey_user_id'),
        'user_passkey',
        ['user_id'],
        unique=False,
    )

    # ── user_recovery_code ─────────────────────────────────────────────
    op.create_table(
        'user_recovery_code',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'code_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('batch_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_recovery_code_user_id'),
        'user_recovery_code',
        ['user_id'],
        unique=False,
    )
    op.create_index(
        'ix_user_recovery_code_user_used',
        'user_recovery_code',
        ['user_id', 'used_at'],
        unique=False,
    )

    # ── user_totp_secret ───────────────────────────────────────────────
    op.create_table(
        'user_totp_secret',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('secret_encrypted', sa.Text(), nullable=False),
        sa.Column(
            'algorithm',
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
        ),
        sa.Column('digits', sa.Integer(), nullable=False),
        sa.Column('period', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_step', sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_totp_secret_user_id'),
        'user_totp_secret',
        ['user_id'],
        unique=True,
    )

    # ── user — three new columns ───────────────────────────────────────
    # ``two_factor_enabled`` is NOT NULL with a server_default of False so
    # the column can be added in-place against existing rows. The default
    # is kept after backfill — every new row is created with ``False``.
    op.add_column(
        'user',
        sa.Column(
            'two_factor_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'user',
        sa.Column('two_factor_enrolled_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'user',
        sa.Column('two_factor_last_used_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('user', 'two_factor_last_used_at')
    op.drop_column('user', 'two_factor_enrolled_at')
    op.drop_column('user', 'two_factor_enabled')

    op.drop_index(
        op.f('ix_user_totp_secret_user_id'), table_name='user_totp_secret'
    )
    op.drop_table('user_totp_secret')

    op.drop_index(
        'ix_user_recovery_code_user_used', table_name='user_recovery_code'
    )
    op.drop_index(
        op.f('ix_user_recovery_code_user_id'),
        table_name='user_recovery_code',
    )
    op.drop_table('user_recovery_code')

    op.drop_index(
        op.f('ix_user_passkey_user_id'), table_name='user_passkey'
    )
    op.drop_index(
        op.f('ix_user_passkey_credential_id'), table_name='user_passkey'
    )
    op.drop_table('user_passkey')

    op.drop_index(
        op.f('ix_user_mfa_challenge_user_id'),
        table_name='user_mfa_challenge',
    )
    op.drop_index(
        'ix_user_mfa_challenge_user_created',
        table_name='user_mfa_challenge',
    )
    op.drop_index(
        op.f('ix_user_mfa_challenge_challenge_token'),
        table_name='user_mfa_challenge',
    )
    op.drop_table('user_mfa_challenge')
