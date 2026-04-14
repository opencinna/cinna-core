import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import Text, UniqueConstraint


# ---------------------------------------------------------------------------
# Database tables
# ---------------------------------------------------------------------------


class IdentityAgentBinding(SQLModel, table=True):
    """Binds a specific agent to a user's identity for Stage 2 routing.

    Identity owner configures which of their agents are reachable via identity.
    Different users may see different agents (controlled by IdentityBindingAssignment).
    """

    __tablename__ = "identity_agent_binding"
    __table_args__ = (
        UniqueConstraint("owner_id", "agent_id", name="uq_identity_agent_binding"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE", index=True)
    trigger_prompt: str = Field(sa_column=Column(Text, nullable=False))
    message_patterns: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    session_mode: str = Field(max_length=20, default="conversation")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IdentityBindingAssignment(SQLModel, table=True):
    """Per-agent user access for identity routing.

    Controls which users can reach which agents behind a given identity.
    This enables different callers to see different subsets of the owner's agents.
    """

    __tablename__ = "identity_binding_assignment"
    __table_args__ = (
        UniqueConstraint(
            "binding_id", "target_user_id", name="uq_identity_binding_assignment"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    binding_id: uuid.UUID = Field(
        foreign_key="identity_agent_binding.id", ondelete="CASCADE", index=True
    )
    target_user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", index=True
    )
    # Owner-level toggle: owner can disable a specific agent for a specific user
    is_active: bool = Field(default=True)
    # Target user-level toggle: user can opt out of this person's identity
    is_enabled: bool = Field(default=False)
    # If True, is_enabled starts as True (superuser-only)
    auto_enable: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class IdentityAgentBindingCreate(SQLModel):
    agent_id: uuid.UUID
    trigger_prompt: str
    message_patterns: str | None = None
    session_mode: str = "conversation"
    assigned_user_ids: list[uuid.UUID] = []
    auto_enable: bool = False  # superuser-only


class IdentityAgentBindingUpdate(SQLModel):
    trigger_prompt: str | None = None
    message_patterns: str | None = None
    session_mode: str | None = None
    is_active: bool | None = None


class IdentityBindingAssignmentPublic(SQLModel):
    id: uuid.UUID
    binding_id: uuid.UUID
    target_user_id: uuid.UUID
    target_user_name: str = ""
    target_user_email: str = ""
    is_active: bool
    is_enabled: bool
    created_at: datetime


class IdentityAgentBindingPublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str = ""
    trigger_prompt: str
    message_patterns: str | None
    session_mode: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    assignments: list[IdentityBindingAssignmentPublic] = []


class IdentityContactPublic(SQLModel):
    """Represents a person who has shared agents with the current user via identity."""

    owner_id: uuid.UUID
    owner_name: str
    owner_email: str
    is_enabled: bool  # target user's per-person toggle
    agent_count: int  # number of active bindings shared with this user
    assignment_ids: list[uuid.UUID]  # assignment IDs for bulk toggle
