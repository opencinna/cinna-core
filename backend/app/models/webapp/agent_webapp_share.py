"""
Agent Webapp Share model for sharing webapp dashboards via public URLs.

Webapp shares provide token-based access to an agent's webapp content
(static files and optional data API endpoints) for unauthenticated viewers.
"""
import re
import uuid
from datetime import datetime, UTC
from pydantic import field_validator
from sqlmodel import Field, SQLModel


class AgentWebappShareBase(SQLModel):
    label: str | None = Field(default=None, max_length=255)


class AgentWebappShareCreate(AgentWebappShareBase):
    expires_in_hours: int | None = Field(default=None, ge=1, le=8760)  # None = never expires
    allow_data_api: bool = True
    require_security_code: bool = False


class AgentWebappShare(AgentWebappShareBase, table=True):
    __tablename__ = "agent_webapp_share"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True)
    token_hash: str = Field(nullable=False, index=True)
    token_prefix: str = Field(max_length=12)
    token: str | None = Field(default=None)
    is_active: bool = Field(default=True)
    allow_data_api: bool = Field(default=True)
    security_code_encrypted: str | None = Field(default=None)
    failed_code_attempts: int = Field(default=0)
    is_code_blocked: bool = Field(default=False)
    expires_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentWebappSharePublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    label: str | None
    token_prefix: str
    is_active: bool
    allow_data_api: bool
    expires_at: datetime | None
    created_at: datetime
    share_url: str | None = None
    security_code: str | None = None
    is_code_blocked: bool = False


class AgentWebappShareCreated(AgentWebappSharePublic):
    """Returned only on creation - includes the actual token and share URL."""
    token: str
    share_url: str
    security_code: str | None = None


class AgentWebappShareUpdate(SQLModel):
    label: str | None = None
    is_active: bool | None = None
    allow_data_api: bool | None = None
    security_code: str | None = Field(default=None, min_length=4, max_length=4)
    remove_security_code: bool | None = None

    @field_validator("security_code")
    @classmethod
    def validate_security_code(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^\d{4}$", v):
            raise ValueError("Security code must be exactly 4 digits")
        return v


class AgentWebappSharesPublic(SQLModel):
    data: list[AgentWebappSharePublic]
    count: int


class WebappShareTokenPayload(SQLModel):
    """JWT payload for webapp share tokens."""
    sub: str  # webapp_share_id as string
    role: str = "webapp-viewer"
    agent_id: str
    owner_id: str
    token_type: str = "webapp_share"
