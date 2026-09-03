"""add account-token provenance to desktop_oauth_client

Adds ``minted_by_account_token_id`` — the CLI account token that bought a desktop
session through ``POST /api/v1/cli/account/desktop-token`` — so revoking that
account token also disconnects the desktop session it minted, mirroring the
existing child-CLI-token cascade.

Nullable with no backfill, and that is correct rather than lazy: every existing
row was granted by a browser consent (the exchange endpoint ships with this
revision), and a browser-granted client has no account token behind it. NULL is
the true value, not a missing one.

SET NULL on delete, matching ``cli_device_login_request.minted_token_id``:
deleting a CLI token row must not delete the desktop session record with it.

Kept as its own revision rather than folded into 21729ab9b43d, which is already
applied to developer and test databases — editing an applied revision leaves
those schemas silently short of a column no migration will ever add.

Revision ID: b4c1d7e93f28
Revises: 21729ab9b43d
Create Date: 2026-09-03 11:40:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b4c1d7e93f28'
down_revision = '21729ab9b43d'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'desktop_oauth_client',
        sa.Column('minted_by_account_token_id', sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f('ix_desktop_oauth_client_minted_by_account_token_id'),
        'desktop_oauth_client',
        ['minted_by_account_token_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_desktop_oauth_client_minted_by_account_token_id_cli_token',
        'desktop_oauth_client',
        'cli_token',
        ['minted_by_account_token_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_desktop_oauth_client_minted_by_account_token_id_cli_token',
        'desktop_oauth_client',
        type_='foreignkey',
    )
    op.drop_index(
        op.f('ix_desktop_oauth_client_minted_by_account_token_id'),
        table_name='desktop_oauth_client',
    )
    op.drop_column('desktop_oauth_client', 'minted_by_account_token_id')
