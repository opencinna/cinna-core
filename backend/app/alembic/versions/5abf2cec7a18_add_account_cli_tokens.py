"""add_account_cli_tokens

Account CLI workspace — auth spine (Phase 1).

Adds account-token support to the existing per-agent CLI tables:
- ``cli_token.token_type``                  ("cli" | "cli-account")
- ``cli_token.minted_by_account_token_id``  self-FK for child-token cascade
- ``cli_token.agent_id``                    made nullable (account tokens)
- ``cli_setup_token.kind``                  ("agent" | "account")
- ``cli_setup_token.agent_id``              made nullable (account setup tokens)

Revision ID: 5abf2cec7a18
Revises: e8f1a2b3c4d5
Create Date: 2026-06-11 15:08:48.684847

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '5abf2cec7a18'
down_revision = 'e8f1a2b3c4d5'
branch_labels = None
depends_on = None

# Constraint name for the self-referential child-token FK.
_MINTED_BY_FK = 'fk_cli_token_minted_by_account_token_id'


def upgrade():
    # cli_setup_token: kind + nullable agent_id
    op.add_column(
        'cli_setup_token',
        sa.Column(
            'kind',
            sqlmodel.sql.sqltypes.AutoString(length=20),
            nullable=False,
            server_default='agent',
        ),
    )
    op.alter_column(
        'cli_setup_token', 'agent_id',
        existing_type=sa.UUID(),
        nullable=True,
    )

    # cli_token: token_type + provenance self-FK + nullable agent_id
    op.add_column(
        'cli_token',
        sa.Column(
            'token_type',
            sqlmodel.sql.sqltypes.AutoString(length=20),
            nullable=False,
            server_default='cli',
        ),
    )
    op.add_column(
        'cli_token',
        sa.Column('minted_by_account_token_id', sa.Uuid(), nullable=True),
    )
    op.alter_column(
        'cli_token', 'agent_id',
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.create_index(
        op.f('ix_cli_token_minted_by_account_token_id'),
        'cli_token',
        ['minted_by_account_token_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_cli_token_token_type'),
        'cli_token',
        ['token_type'],
        unique=False,
    )
    op.create_foreign_key(
        _MINTED_BY_FK,
        'cli_token', 'cli_token',
        ['minted_by_account_token_id'], ['id'],
        ondelete='CASCADE',
    )


def downgrade():
    # Remove account rows before re-imposing NOT NULL on the agent_id columns —
    # account tokens / setup tokens have NULL agent_id and would violate it.
    op.execute("DELETE FROM cli_token WHERE token_type = 'cli-account'")
    op.execute("DELETE FROM cli_setup_token WHERE kind = 'account'")

    op.drop_constraint(_MINTED_BY_FK, 'cli_token', type_='foreignkey')
    op.drop_index(op.f('ix_cli_token_token_type'), table_name='cli_token')
    op.drop_index(
        op.f('ix_cli_token_minted_by_account_token_id'), table_name='cli_token'
    )
    op.alter_column(
        'cli_token', 'agent_id',
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.drop_column('cli_token', 'minted_by_account_token_id')
    op.drop_column('cli_token', 'token_type')

    op.alter_column(
        'cli_setup_token', 'agent_id',
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.drop_column('cli_setup_token', 'kind')
