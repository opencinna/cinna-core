"""add origin to desktop_oauth_client

Records how a native-client session's tokens were granted: through the browser
consent flow, or by exchanging a CLI account token at
``POST /api/v1/cli/account/desktop-token``. Every pre-existing row predates that
endpoint, so the server default backfills them as ``browser_consent`` — which is
true of every one of them by construction.

NOTE: ``--autogenerate`` also proposed three ``cli_device_login_request``
timestamp alterations (TIMESTAMP(timezone=True) → DateTime). Those are
pre-existing model/DB drift unrelated to this change — the DB columns are
timezone-aware and correct, the model annotations are not — and applying them
would strip the tz from live columns. Deliberately dropped from this revision.

Revision ID: 21729ab9b43d
Revises: 7de57f5d4b8f
Create Date: 2026-09-03 09:13:09.053206

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '21729ab9b43d'
down_revision = '7de57f5d4b8f'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'desktop_oauth_client',
        sa.Column(
            'origin',
            sqlmodel.sql.sqltypes.AutoString(length=32),
            server_default='browser_consent',
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column('desktop_oauth_client', 'origin')
