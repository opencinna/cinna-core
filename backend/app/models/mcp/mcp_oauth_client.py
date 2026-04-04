import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON


class MCPOAuthClient(SQLModel, table=True):
    __tablename__ = "mcp_oauth_client"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    client_id: str = Field(unique=True, index=True)
    client_secret_hash: str
    client_name: str = ""
    redirect_uris: list = Field(default_factory=list, sa_column=Column(JSON))
    grant_types: list = Field(default_factory=lambda: ["authorization_code", "refresh_token"], sa_column=Column(JSON))
    response_types: list = Field(default_factory=lambda: ["code"], sa_column=Column(JSON))
    # Nullable: MCP clients may register without specifying a resource (connector).
    # The connector binding happens during the authorize step instead.
    connector_id: uuid.UUID | None = Field(default=None, foreign_key="mcp_connector.id", nullable=True, ondelete="CASCADE")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MCPOAuthClientPublic(SQLModel):
    id: uuid.UUID
    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    connector_id: uuid.UUID | None
    created_at: datetime
