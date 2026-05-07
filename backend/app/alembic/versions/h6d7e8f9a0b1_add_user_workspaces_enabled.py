"""add user.workspaces_enabled column

Revision ID: h6d7e8f9a0b1
Revises: g5c6d7e8f9a0
Create Date: 2026-05-07 12:00:00.000000

Persist the per-user "Workspaces Enabled" UI preference on the User
profile so it survives across browsers and devices. Default is False —
new users start without workspace filtering and can opt in via Settings
→ Interface → Workspaces card.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "h6d7e8f9a0b1"
down_revision = "g5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user",
        sa.Column(
            "workspaces_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column("user", "workspaces_enabled")
