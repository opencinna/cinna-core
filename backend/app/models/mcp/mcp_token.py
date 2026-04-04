import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field


class MCPToken(SQLModel, table=True):
    __tablename__ = "mcp_token"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    token: str = Field(unique=True, index=True)
    token_type: str  # "access" | "refresh"
    client_id: str = Field(index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    connector_id: uuid.UUID = Field(foreign_key="mcp_connector.id", ondelete="CASCADE", index=True)
    scope: str = ""
    resource: str = ""
    expires_at: datetime
    revoked: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
