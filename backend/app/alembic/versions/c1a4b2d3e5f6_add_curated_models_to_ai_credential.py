"""add curated models to ai_credential

Revision ID: c1a4b2d3e5f6
Revises: d3782dd039a5
Create Date: 2026-06-14 09:30:00.000000

Adds admin-curated model metadata to both the managed parent and the per-user
child credential rows (see admin_curated_model_list):

- ``managed_ai_credential.default_model`` (VARCHAR(255), nullable) — the admin's
  preferred default model (bare concrete id).
- ``managed_ai_credential.available_models`` (JSON, nullable) — admin-curated
  list of selectable model ids.
- ``ai_credential.default_model`` (VARCHAR(255), nullable) — mirror of the
  parent value, written through by reconcile. Read by SDK resolution + native
  config.
- ``ai_credential.available_models`` (JSON, nullable) — mirror of the parent
  value, written through by reconcile. Read by model pickers + native config.

All four are plain non-secret columns (no FK / index changes). No data backfill:
existing rows get ``NULL`` (preserves current behavior — catalog default +
discovered list).

Hand-trimmed: the autogenerate run also picks up unrelated TIMESTAMP/AutoString/
JSON-nullability type-comparison drift on other tables
(ai_credential.models_discovered_at, credential.service_uri,
mcp_connector.allowed_user_ids, user_trusted_device.{expires,created,last_used}_at).
Those are spurious dialect/type-rendering diffs, not real schema changes, and are
removed from this migration (consistent with prior migrations d3782dd039a5 /
2f2d8e49501d).
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = 'c1a4b2d3e5f6'
down_revision = 'd3782dd039a5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'managed_ai_credential',
        sa.Column(
            'default_model',
            sqlmodel.sql.sqltypes.AutoString(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        'managed_ai_credential',
        sa.Column('available_models', sa.JSON(), nullable=True),
    )
    op.add_column(
        'ai_credential',
        sa.Column(
            'default_model',
            sqlmodel.sql.sqltypes.AutoString(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        'ai_credential',
        sa.Column('available_models', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('ai_credential', 'available_models')
    op.drop_column('ai_credential', 'default_model')
    op.drop_column('managed_ai_credential', 'available_models')
    op.drop_column('managed_ai_credential', 'default_model')
