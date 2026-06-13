"""add managed_ai_credential

Revision ID: d3782dd039a5
Revises: 2f2d8e49501d
Create Date: 2026-06-13 08:57:49.530777

Introduces the admin-managed parent/child model for AI credentials:

- New ``managed_ai_credential`` parent table. Holds canonical config (name,
  type, Fernet-encrypted key, base_url/model mirrors, default flags, SDK
  defaults) plus ``managed_by_id`` (FK ``user.id`` SET NULL — record stays
  fleet-manageable after the managing admin is deleted). Index
  ``ix_managed_ai_credential_managed_by`` for fleet listing.
- New ``ai_credential.managed_credential_id`` — nullable FK to the parent,
  ``ON DELETE SET NULL`` (safety net; real parent deletion routes through the
  reconcile/delete service path). Indexed ``ix_ai_credential_managed_credential``.

No data backfill: the database has no existing admin-managed (``is_admin_managed``)
rows, so this is pure schema. Any standalone ``is_admin_managed`` rows that ever
existed would simply keep ``managed_credential_id = NULL`` (harmless orphans).

Hand-trimmed: the autogenerate run also picked up unrelated TIMESTAMP/AutoString/
JSON-nullability type-comparison drift on other tables
(ai_credential.models_discovered_at, credential.service_uri,
mcp_connector.allowed_user_ids, user_trusted_device.*). Those are spurious
dialect/type-rendering diffs, not real schema changes, and have been removed from
this migration (consistent with the prior migration 2f2d8e49501d).
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd3782dd039a5'
down_revision = '2f2d8e49501d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'managed_ai_credential',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('encrypted_data', sa.Text(), nullable=False),
        sa.Column('base_url', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('set_as_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('set_user_sdk_defaults', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column(
            'sdk_default_modes',
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text('\'["conversation", "building"]\'::json'),
            nullable=False,
        ),
        sa.Column('expiry_notification_date', sa.DateTime(), nullable=True),
        sa.Column('managed_by_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['managed_by_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_managed_ai_credential_managed_by',
        'managed_ai_credential',
        ['managed_by_id'],
        unique=False,
    )
    op.add_column(
        'ai_credential',
        sa.Column('managed_credential_id', sa.Uuid(), nullable=True),
    )
    op.create_index(
        'ix_ai_credential_managed_credential',
        'ai_credential',
        ['managed_credential_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_ai_credential_managed_credential',
        'ai_credential',
        'managed_ai_credential',
        ['managed_credential_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_ai_credential_managed_credential', 'ai_credential', type_='foreignkey'
    )
    op.drop_index(
        'ix_ai_credential_managed_credential', table_name='ai_credential'
    )
    op.drop_column('ai_credential', 'managed_credential_id')
    op.drop_index(
        'ix_managed_ai_credential_managed_by', table_name='managed_ai_credential'
    )
    op.drop_table('managed_ai_credential')
