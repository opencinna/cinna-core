"""
Agent REST API per-group access grant (L2 scopes for a user group).

The producer agent's owner assigns scopes to a **user group** from the
"Access & Scopes" UI. The proxy resolves them LIVE on every call: a caller's
effective scopes are their personal grant's scopes plus the scopes of every
group they actively belong to (see
``AgentApiGrantService.effective_scopes``). No per-member rows are written, so
a membership change takes effect on the next call.

Kept in its own table, separate from ``agent_api_access_grant`` (user groups
plan D5): a read site that was never taught about groups fails closed.

``created_by`` is nullable SET NULL DEFERRABLE INITIALLY DEFERRED (plan D11):
deleting the producer owner reaches the row by the agent CASCADE and by
``created_by`` in one statement.
"""
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import JSON
from sqlmodel import Column, Field, SQLModel

from app.models.users.user_group import UserGroupRef


class AgentApiAccessGroupGrantCreate(SQLModel):
    """Body of ``POST /agents/{id}/agent-api/group-grants``."""

    group_id: uuid.UUID
    scopes: list[str] = []


class AgentApiAccessGroupGrantUpdate(SQLModel):
    """Body of ``PUT .../group-grants/{grant_id}`` (scopes only)."""

    scopes: list[str] | None = None


class AgentApiAccessGroupGrant(SQLModel, table=True):
    __tablename__ = "agent_api_access_group_grant"
    __table_args__ = (
        sa.UniqueConstraint(
            "producer_agent_id", "group_id",
            name="uq_agent_api_access_group_grant_producer_group",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    producer_agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True
    )
    group_id: uuid.UUID = Field(
        foreign_key="user_group.id", nullable=False, ondelete="CASCADE", index=True
    )
    scopes: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_by: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid,
            sa.ForeignKey(
                "user.id",
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentApiAccessGroupGrantPublic(SQLModel):
    id: uuid.UUID
    producer_agent_id: uuid.UUID
    group_id: uuid.UUID
    # None when the group no longer resolves.
    group: UserGroupRef | None = None
    # Active members only.
    member_count: int = 0
    scopes: list[str]
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class AgentApiAccessGroupGrantsPublic(SQLModel):
    data: list[AgentApiAccessGroupGrantPublic]
    count: int
