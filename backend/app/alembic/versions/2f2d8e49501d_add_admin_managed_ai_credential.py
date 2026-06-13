"""add admin_managed ai credential

Revision ID: 2f2d8e49501d
Revises: d3f0a1b2c4e5
Create Date: 2026-06-13 05:40:18.326791

Adds the two admin-provisioning columns to ``ai_credential``:

- ``is_admin_managed`` — the single behavioral discriminator (read-only for the
  owner). ``server_default false`` backfills all existing rows correctly:
  nothing was admin-provisioned before this migration.
- ``managed_by_id`` — audit-only FK to the provisioning admin, ``SET NULL`` on
  admin deletion so the user keeps their credential. Indexed for the admin's
  "what did I provision" listing.

Hand-trimmed: the autogenerate run also picked up unrelated TIMESTAMP/AutoString
type-comparison drift on other tables (credential.service_uri,
mcp_connector.allowed_user_ids, user_trusted_device.*, ai_credential
.models_discovered_at). Those are spurious dialect/type-rendering diffs, not
real schema changes, and have been removed from this migration.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '2f2d8e49501d'
down_revision = 'd3f0a1b2c4e5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'ai_credential',
        sa.Column(
            'is_admin_managed',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )
    op.add_column(
        'ai_credential',
        sa.Column('managed_by_id', sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f('ix_ai_credential_managed_by_id'),
        'ai_credential',
        ['managed_by_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_ai_credential_managed_by',
        'ai_credential',
        'user',
        ['managed_by_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_ai_credential_managed_by', 'ai_credential', type_='foreignkey'
    )
    op.drop_index(
        op.f('ix_ai_credential_managed_by_id'), table_name='ai_credential'
    )
    op.drop_column('ai_credential', 'managed_by_id')
    op.drop_column('ai_credential', 'is_admin_managed')
