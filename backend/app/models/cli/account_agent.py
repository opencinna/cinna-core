"""
Account CLI — accessible-agent listing schemas.

Minimal projection of an agent returned by ``GET /api/v1/cli/account/agents``
so the account CLI token can discover what the user can access and which agents
it can mint per-agent (building) tokens for. No credentials, prompts, or env
internals are exposed.
"""
import uuid

from sqlmodel import SQLModel


class AccountAgentListItem(SQLModel):
    """One row in the accessible-agents listing for the account CLI."""
    id: uuid.UUID
    name: str
    description: str | None
    ui_color_preset: str | None
    owner_id: uuid.UUID
    user_workspace_id: uuid.UUID | None
    bundle_uuid: uuid.UUID | None
    is_publisher_install: bool
    # Derived: a consumer (bundle-owned, non-publisher) install — not a
    # sync/exec/mint target.
    is_foreign_install: bool
    # Derived: developer/admin role AND not a foreign install AND accessible.
    can_build: bool
    # Whether the agent has an active environment (for the env-active dot).
    has_active_environment: bool


class AccountAgentsPublic(SQLModel):
    data: list[AccountAgentListItem]
    count: int
