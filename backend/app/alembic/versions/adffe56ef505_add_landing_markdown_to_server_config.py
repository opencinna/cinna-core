"""add landing_markdown to server_config

Revision ID: adffe56ef505
Revises: 89878dff89a6
Create Date: 2026-09-06 08:02:23.158594

Adds the admin-authored welcome copy rendered on the public `/start` page.

`NOT NULL DEFAULT ''` rather than nullable: the singleton row already exists
on every deployed instance, so the server default is what backfills it, and
every reader then sees `""` for "no welcome written" instead of having to
decide what `NULL` means. No index — nothing queries by it; it is only ever
read as part of the singleton row.

Autogenerate also proposed three `cli_device_login_request` timestamp
alterations. They are pre-existing model/DB drift where the *model* is wrong
(the columns are correctly `timestamptz` in the database) and have nothing to
do with this change, so they are deliberately not carried here.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'adffe56ef505'
down_revision = '89878dff89a6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'server_config',
        sa.Column(
            'landing_markdown',
            sa.Text(),
            nullable=False,
            server_default='',
        ),
    )


def downgrade():
    op.drop_column('server_config', 'landing_markdown')
