import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field


class MCPToken(SQLModel, table=True):
    __tablename__ = "mcp_token"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    token: str = Field(unique=True, index=True)
    token_type: str  # "access" | "refresh" | "direct"
    client_id: str = Field(index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    connector_id: uuid.UUID = Field(foreign_key="mcp_connector.id", ondelete="CASCADE", index=True)
    scope: str = ""
    resource: str = ""
    # Human-readable name for direct tokens (OAuth tokens leave this null).
    label: str | None = Field(default=None)
    expires_at: datetime
    revoked: bool = Field(default=False)
    last_used_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Direct-token projections
#
# The full opaque token value lives in MCPToken.token (the verifier looks it
# up by exact match). One-time reveal is enforced here at the projection layer:
# public schemas never expose ``token`` — only the 8-char ``prefix``. The full
# value is returned exactly once, on creation, via MCPConnectorTokenCreated.
# ---------------------------------------------------------------------------


class MCPConnectorTokenCreate(SQLModel):
    label: str = Field(min_length=1, max_length=255)


class MCPConnectorTokenUpdate(SQLModel):
    revoked: bool | None = None


class MCPConnectorTokenPublic(SQLModel):
    id: uuid.UUID
    connector_id: uuid.UUID
    label: str | None
    prefix: str  # First 8 chars of the token value.
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool
    expires_at: datetime


class MCPConnectorTokenCreated(MCPConnectorTokenPublic):
    """Returned only on creation — includes the full token value once."""
    token: str


class MCPConnectorTokensPublic(SQLModel):
    data: list[MCPConnectorTokenPublic]
    count: int
