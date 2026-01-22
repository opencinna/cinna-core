"""
Agent Access Token model for A2A authentication.

These tokens provide scoped access to agents for external A2A clients,
separate from regular user authentication.
"""
import uuid
from datetime import datetime
from enum import Enum
import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class AccessTokenMode(str, Enum):
    """Access mode for the token - determines what operations are allowed."""
    CONVERSATION = "conversation"  # Can only use agent in conversation mode
    BUILDING = "building"  # Can use agent in building mode (includes conversation)


class AccessTokenScope(str, Enum):
    """Scope for the token - determines session visibility."""
    LIMITED = "limited"  # Can only access sessions created by this token
    GENERAL = "general"  # Can access all sessions for the agent


# Shared properties
class AgentAccessTokenBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    mode: AccessTokenMode = Field(default=AccessTokenMode.CONVERSATION, sa_type=sa.String(20))
    scope: AccessTokenScope = Field(default=AccessTokenScope.LIMITED, sa_type=sa.String(20))


# Properties to receive on token creation
class AgentAccessTokenCreate(AgentAccessTokenBase):
    agent_id: uuid.UUID


# Database model
class AgentAccessToken(AgentAccessTokenBase, table=True):
    __tablename__ = "agent_access_tokens"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    # Store hash of the token, not the token itself
    token_hash: str = Field(max_length=255, index=True)
    # Token prefix for identification (first 8 chars of token)
    token_prefix: str = Field(max_length=8)
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = None
    is_revoked: bool = Field(default=False)


# Properties to return via API (without sensitive data)
class AgentAccessTokenPublic(AgentAccessTokenBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    token_prefix: str
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime | None
    is_revoked: bool


# Properties to return when creating a token (includes the actual token once)
class AgentAccessTokenCreated(AgentAccessTokenPublic):
    """Returned only on token creation - includes the actual token value."""
    token: str  # The actual JWT token - only shown once on creation


# Properties to return via API with list of tokens
class AgentAccessTokensPublic(SQLModel):
    data: list[AgentAccessTokenPublic]
    count: int


# Properties for updating token (only name and revoked status can be updated)
class AgentAccessTokenUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    is_revoked: bool | None = None


# JWT Token payload for agent access tokens
class A2ATokenPayload(SQLModel):
    """JWT payload for agent access tokens."""
    sub: str  # Token ID (not user ID)
    agent_id: str  # Agent UUID this token is for
    mode: str  # "conversation" or "building"
    scope: str  # "limited" or "general"
    token_type: str = "agent"  # Differentiates from regular user tokens
    exp: int  # Expiration timestamp
