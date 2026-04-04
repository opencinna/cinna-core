import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field


class MCPAuthCode(SQLModel, table=True):
    __tablename__ = "mcp_auth_code"

    code: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    connector_id: uuid.UUID = Field(foreign_key="mcp_connector.id", ondelete="CASCADE")
    redirect_uri: str
    code_challenge: str
    scope: str = ""
    resource: str = ""
    expires_at: datetime
    used: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MCPAuthRequest(SQLModel, table=True):
    __tablename__ = "mcp_auth_request"

    nonce: str = Field(primary_key=True)
    connector_id: uuid.UUID = Field(foreign_key="mcp_connector.id", ondelete="CASCADE")
    client_id: str
    redirect_uri: str
    code_challenge: str = ""
    code_challenge_method: str = "S256"
    scope: str = ""
    state: str = ""
    resource: str = ""
    expires_at: datetime
    used: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
