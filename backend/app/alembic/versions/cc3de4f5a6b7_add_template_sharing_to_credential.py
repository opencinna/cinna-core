"""add template-sharing fields to credential

Revision ID: cc3de4f5a6b7
Revises: bb2cd3e4f5a6
Create Date: 2026-05-08 12:00:00.000000

Adds two new columns to ``credential`` for the new "Share as Template"
mode: ``allow_template_sharing`` (boolean) marks a credential as
template-shareable; ``template_private_fields`` (JSON list of field
names) records which credential_data fields are private and must be
filled in by the installer when the bundle materialises a template
credential.

Both columns default to safe values (``false`` / ``[]``) so the column
add is a no-op for existing rows. Downgrade drops them.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "cc3de4f5a6b7"
down_revision = "bb2cd3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "credential",
        sa.Column(
            "allow_template_sharing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "credential",
        sa.Column(
            "template_private_fields",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade():
    op.drop_column("credential", "template_private_fields")
    op.drop_column("credential", "allow_template_sharing")
