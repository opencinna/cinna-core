import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON


class MCPConnectorBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    mode: str = "conversation"  # "conversation" | "building"


class MCPConnectorCreate(MCPConnectorBase):
    allowed_emails: list[str] = []
    max_clients: int = 10


class MCPConnectorUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    mode: str | None = None
    is_active: bool | None = None
    allowed_emails: list[str] | None = None
    max_clients: int | None = None


class MCPConnector(MCPConnectorBase, table=True):
    __tablename__ = "mcp_connector"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    is_active: bool = Field(default=True)
    allowed_emails: list = Field(default_factory=list, sa_column=Column(JSON))
    max_clients: int = Field(default=10)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MCPConnectorPublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    mode: str
    is_active: bool
    allowed_emails: list[str]
    max_clients: int
    mcp_server_url: str | None = None  # Computed from MCP_SERVER_BASE_URL + id
    created_at: datetime
    updated_at: datetime


class MCPConnectorsPublic(SQLModel):
    data: list[MCPConnectorPublic]
    count: int
    mcp_server_base_url: str | None = None
