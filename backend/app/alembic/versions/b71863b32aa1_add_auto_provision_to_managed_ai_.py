"""add auto provision to managed ai credential

Zero-touch onboarding, phase 2. Three columns on ``managed_ai_credential``:

* ``auto_provision_roles`` — which roles' *newly created* accounts receive
  this credential automatically. NOT NULL with a ``'[]'`` server default, so
  every existing record backfills to "nobody", which is the only safe
  backfill: a permissive default would hand a company API key to the next
  person who signs up, with no admin action anywhere in the story.
* ``model_override_conversation`` / ``model_override_building`` — the model id
  written onto the member's profile for the modes this record wires. Nullable;
  NULL means "no override", which is the existing behaviour.

Autogenerate also proposed three ``cli_device_login_request`` timestamp
alterations. They are unrelated pre-existing drift between that model and the
column type actually in the database (the model is the wrong side, not the
DB), so they are deliberately NOT in this migration — a schema change nobody
asked for does not belong in a feature's migration, and applying it would drop
the timezone from live rows.

Revision ID: b71863b32aa1
Revises: 1d737d7ef0a0
Create Date: 2026-09-05 21:21:41.049839

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'b71863b32aa1'
down_revision = '1d737d7ef0a0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'managed_ai_credential',
        sa.Column(
            'auto_provision_roles',
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        'managed_ai_credential',
        sa.Column(
            'model_override_conversation',
            sqlmodel.sql.sqltypes.AutoString(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        'managed_ai_credential',
        sa.Column(
            'model_override_building',
            sqlmodel.sql.sqltypes.AutoString(length=255),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('managed_ai_credential', 'model_override_building')
    op.drop_column('managed_ai_credential', 'model_override_conversation')
    op.drop_column('managed_ai_credential', 'auto_provision_roles')
