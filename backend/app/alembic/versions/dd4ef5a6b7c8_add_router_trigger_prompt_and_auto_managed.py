"""add router_trigger_prompt and is_auto_managed columns

Revision ID: dd4ef5a6b7c8
Revises: cc3de4f5a6b7
Create Date: 2026-05-11 10:00:00.000000

Wires bundle installs into the App MCP router automatically:

- ``agent.router_trigger_prompt`` — natural-language description of when
  the App MCP router should pick this agent. Edited by the agent owner
  on the Prompts tab.
- ``agent_bundle_revision.router_trigger_prompt`` — immutable snapshot of
  the agent's trigger prompt at publish time, propagated to installs.
- ``app_agent_route.is_auto_managed`` — flag set by ``InstallService`` for
  routes it auto-creates. apply-update refreshes auto-managed routes;
  manual edits via the PUT endpoint flip the flag off.

All three are nullable / defaulted so existing rows are valid without a
data migration. The Phase 8 backfill migration handles populating
``router_trigger_prompt`` and creating auto-routes for pre-existing
installs.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "dd4ef5a6b7c8"
down_revision = "cc3de4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent",
        sa.Column("router_trigger_prompt", sa.Text(), nullable=True),
    )
    op.add_column(
        "agent_bundle_revision",
        sa.Column("router_trigger_prompt", sa.Text(), nullable=True),
    )
    op.add_column(
        "app_agent_route",
        sa.Column(
            "is_auto_managed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade():
    op.drop_column("app_agent_route", "is_auto_managed")
    op.drop_column("agent_bundle_revision", "router_trigger_prompt")
    op.drop_column("agent", "router_trigger_prompt")
