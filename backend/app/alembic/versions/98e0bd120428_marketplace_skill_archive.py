"""marketplace_skill_archive

Adds the content-addressed archive snapshot digest and compressed size to
skills-format marketplace plugins. Both nullable, no backfill: existing rows
stay NULL (not installable on the desktop) until their marketplace is synced
again.

Revision ID: 98e0bd120428
Revises: 90a3fecdf28b
Create Date: 2026-10-18 05:51:05.814538

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '98e0bd120428'
down_revision = '90a3fecdf28b'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'llm_plugin_marketplace_plugin',
        sa.Column(
            'skill_archive_sha256',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        'llm_plugin_marketplace_plugin',
        sa.Column('skill_archive_size', sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column('llm_plugin_marketplace_plugin', 'skill_archive_size')
    op.drop_column('llm_plugin_marketplace_plugin', 'skill_archive_sha256')
