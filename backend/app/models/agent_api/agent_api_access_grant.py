"""
Agent REST API per-user access grant (L2 scopes).

The producer agent's owner assigns **scopes** to individual platform users from
the agent-api "Access & Scopes" UI. The proxy resolves these LIVE on every call
(``grant(producer_agent_in_url, owner_user_id) -> scopes``) and injects them as
``X-Cinna-Caller-Scopes`` so the producer can do capability-level authorization.

Design (plan D5):
- One grant per ``(producer_agent_id, user_id)`` — enforced by a unique
  constraint. Editing/deleting a grant takes effect on the *next* call (no token
  re-mint, no re-sync) — that is the "control access from my side" requirement.
- ``scopes`` is a free-form list of scope names drawn from the producer's
  ``policy.yaml`` catalog. No grant ⇒ no scopes injected ⇒ the producer decides
  what an unscoped caller may do.
- Owner-gated: only the producer agent's owner (or a superuser) may CRUD grants.
"""
import uuid
from datetime import datetime, UTC

import sqlalchemy as sa
from sqlalchemy import JSON
from sqlmodel import Field, SQLModel, Column


# Shared properties
class AgentApiAccessGrantBase(SQLModel):
    # Scope names from the producer's policy.yaml catalog. Free-form strings;
    # the producer interprets them. Empty list = an explicit "known user, no
    # scopes" grant (still attributes the caller, grants nothing).
    scopes: list[str] = []


# Properties to receive on grant creation
class AgentApiAccessGrantCreate(AgentApiAccessGrantBase):
    # The platform user being granted access. (producer_agent_id comes from the
    # route path; created_by/owner come from the authenticated caller.)
    user_id: uuid.UUID


# Properties to receive on grant update (scopes only — identity is immutable)
class AgentApiAccessGrantUpdate(SQLModel):
    scopes: list[str] | None = None


# Database model
class AgentApiAccessGrant(AgentApiAccessGrantBase, table=True):
    __tablename__ = "agent_api_access_grant"
    __table_args__ = (
        # One grant per (producer, user).
        sa.UniqueConstraint(
            "producer_agent_id", "user_id",
            name="uq_agent_api_access_grant_producer_user",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # The API-exposing agent (Agent A).
    producer_agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True
    )
    # The granted cinna-core user.
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    # Scope names (stored as a JSON list of str).
    scopes: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # The producer owner who created the grant (audit / provenance).
    created_by: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Resolved display projection for a granted platform user (for the UI picker).
class AgentApiGrantUser(SQLModel):
    id: uuid.UUID
    email: str
    full_name: str | None = None


# Properties to return via API
class AgentApiAccessGrantPublic(SQLModel):
    id: uuid.UUID
    producer_agent_id: uuid.UUID
    user_id: uuid.UUID
    scopes: list[str]
    # Resolved display info for user_id (name/email for the picker). None when
    # the user no longer resolves.
    user: AgentApiGrantUser | None = None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AgentApiAccessGrantsPublic(SQLModel):
    data: list[AgentApiAccessGrantPublic]
    count: int


# ── Scope catalog (read from the producer's cached policy.yaml) ───────────────


class AgentApiScope(SQLModel):
    """One available scope the producer declared in policy.yaml."""
    name: str
    description: str | None = None


class AgentApiScopeCatalog(SQLModel):
    """The available-scope catalog offered to the owner's scope picker."""
    scopes: list[AgentApiScope]
