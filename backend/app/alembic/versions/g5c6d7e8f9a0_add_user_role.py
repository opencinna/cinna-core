"""add user.role column with backfill

Revision ID: g5c6d7e8f9a0
Revises: f4b5c6d7e8a9
Create Date: 2026-05-06 16:00:00.000000

Phase 3 of the Agent Bundles & Installs plan — introduce the new
three-value ``UserRole`` enum on ``user``:

    * ``agent-user``       — default for non-superusers; read-only
                             access to the catalog and conversation UI.
    * ``agent-developer``  — admin-promoted; today's full developer UI
                             (agent CRUD, building-mode sessions,
                             publish, sync-prompts).
    * ``admin``            — kept in sync with ``is_superuser=True``.

``is_superuser`` continues to drive admin privileges; the new ``role``
field layers role-based gating on top for the agent-user vs. agent-
developer distinction.

Schema changes:
    * Add ``role`` column to ``user`` (string, max 32, NOT NULL).
    * Backfill: existing superusers → ``admin``; everyone else →
      ``agent-user``.

Downgrade simply drops the column.  Behaviour change is enforced in
the service layer (``RoleService``) and route guards.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "g5c6d7e8f9a0"
down_revision = "f4b5c6d7e8a9"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add the column NULLABLE first so existing rows are happy.
    op.add_column(
        "user",
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=True,
            server_default=sa.text("'agent-user'"),
        ),
    )

    # 2. Backfill — superusers become admins, the rest are agent-users.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE \"user\" "
            "SET role = CASE WHEN is_superuser = TRUE THEN 'admin' ELSE 'agent-user' END "
            "WHERE role IS NULL OR role = ''"
        )
    )

    # 3. Tighten to NOT NULL.
    op.alter_column("user", "role", nullable=False)


def downgrade():
    op.drop_column("user", "role")
