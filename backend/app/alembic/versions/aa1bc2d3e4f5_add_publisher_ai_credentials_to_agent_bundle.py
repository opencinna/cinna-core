"""add publisher AI credential FKs to agent_bundle

Revision ID: aa1bc2d3e4f5
Revises: i7e8f9a0b1c2
Create Date: 2026-05-07 16:00:00.000000

Phase 1 of the install-experience-redesign plan: add two nullable FK
columns on ``agent_bundle`` so the publisher can later opt to provide AI
credentials (conversation + building modes) for foreign installs. NULL
keeps the current behaviour of "user provides AI at install time".

``ON DELETE SET NULL`` so deleting the publisher's AI credential degrades
the bundle to "user provides" rather than cascading destroy.

No backfill, no data migration. Downgrade drops both columns.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "aa1bc2d3e4f5"
down_revision = "i7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_bundle",
        sa.Column(
            "publisher_ai_credential_conversation_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_bundle",
        sa.Column(
            "publisher_ai_credential_building_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_agent_bundle_publisher_ai_credential_conversation",
        "agent_bundle",
        "ai_credential",
        ["publisher_ai_credential_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_agent_bundle_publisher_ai_credential_building",
        "agent_bundle",
        "ai_credential",
        ["publisher_ai_credential_building_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        "fk_agent_bundle_publisher_ai_credential_building",
        "agent_bundle",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_bundle_publisher_ai_credential_conversation",
        "agent_bundle",
        type_="foreignkey",
    )
    op.drop_column("agent_bundle", "publisher_ai_credential_building_id")
    op.drop_column("agent_bundle", "publisher_ai_credential_conversation_id")
