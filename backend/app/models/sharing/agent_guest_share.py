"""
Agent Guest Share model for guest access to agents.

Guest shares provide time-limited, token-based access to agents
for unauthenticated or guest users. Each share generates a unique
URL that can be distributed to allow chat-only access.
"""
import re
import uuid
from datetime import datetime, UTC
from pydantic import field_validator
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


# Shared properties
class AgentGuestShareBase(SQLModel):
    label: str | None = Field(default=None, max_length=255)


# Properties to receive on guest share creation
class AgentGuestShareCreate(AgentGuestShareBase):
    expires_in_hours: int = Field(default=24, ge=1, le=720)
    allow_env_panel: bool = False


# Database model
class AgentGuestShare(AgentGuestShareBase, table=True):
    __tablename__ = "agent_guest_share"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True)
    token_hash: str = Field(nullable=False, index=True)
    token_prefix: str = Field(max_length=12)
    token: str | None = Field(default=None)
    expires_at: datetime = Field(nullable=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_revoked: bool = Field(default=False)
    security_code_encrypted: str | None = Field(default=None)
    failed_code_attempts: int = Field(default=0)
    is_code_blocked: bool = Field(default=False)
    allow_env_panel: bool = Field(default=False)


# Properties to return via API (without sensitive data)
class AgentGuestSharePublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    label: str | None
    token_prefix: str
    expires_at: datetime
    created_at: datetime
    is_revoked: bool
    session_count: int = 0  # Computed field, set by service layer
    share_url: str | None = None  # Computed from stored token, for owner to copy link
    security_code: str | None = None  # Decrypted by service for owner
    is_code_blocked: bool = False
    allow_env_panel: bool = False


# Properties to return when creating a guest share (includes the actual token once)
class AgentGuestShareCreated(AgentGuestSharePublic):
    """Returned only on creation - includes the actual token and share URL."""
    token: str  # The raw token - only shown once on creation
    share_url: str  # The full guest URL
    security_code: str  # The plaintext security code - shown on creation


# Properties to receive on guest share update
class AgentGuestShareUpdate(SQLModel):
    label: str | None = None
    security_code: str | None = Field(default=None, min_length=4, max_length=4)
    allow_env_panel: bool | None = None

    @field_validator("security_code")
    @classmethod
    def validate_security_code(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^\d{4}$", v):
            raise ValueError("Security code must be exactly 4 digits")
        return v


# Properties to return via API with list of guest shares
class AgentGuestSharesPublic(SQLModel):
    data: list[AgentGuestSharePublic]
    count: int


# Database model for tracking which users have activated a guest share
class GuestShareGrant(SQLModel, table=True):
    __tablename__ = "guest_share_grant"
    __table_args__ = (
        UniqueConstraint("user_id", "guest_share_id", name="uq_guest_share_grant_user_share"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    guest_share_id: uuid.UUID = Field(foreign_key="agent_guest_share.id", nullable=False, ondelete="CASCADE")
    activated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# JWT claims schema for guest share tokens
class GuestShareTokenPayload(SQLModel):
    """JWT payload for guest share tokens."""
    sub: str  # guest_share_id as string
    role: str = "chat-guest"
    agent_id: str
    owner_id: str
    token_type: str = "guest_share"
