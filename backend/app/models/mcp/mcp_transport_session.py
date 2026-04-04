import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field


class MCPTransportSession(SQLModel, table=True):
    """Tracks active MCP transport sessions across workers.

    The MCP SDK stores transport sessions in-memory per worker process.
    When running multiple uvicorn workers, a session created on worker A
    is invisible to worker B.  This table provides a shared registry so
    any worker can discover that a session exists and "warm" it locally
    instead of returning 404.

    Rows are inserted when a new MCP session is created and deleted when
    the transport is terminated (DELETE request or crash cleanup).
    Connector deletion cascades to all its transport sessions.
    """

    __tablename__ = "mcp_transport_session"

    session_id: str = Field(primary_key=True)
    connector_id: uuid.UUID = Field(
        foreign_key="mcp_connector.id", ondelete="CASCADE", index=True
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
