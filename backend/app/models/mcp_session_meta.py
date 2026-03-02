import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field


class MCPSessionMeta(SQLModel, table=True):
    """Tracks the OAuth-authenticated MCP user for a session.

    When an MCP connector has ``allowed_emails``, the session's ``user_id``
    is the connector *owner*, but the person actually communicating may be
    a different user who authenticated via OAuth.  This table records that
    identity so it can be surfaced in the agent's session context.
    """

    __tablename__ = "mcp_session_meta"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID = Field(
        foreign_key="session.id", ondelete="CASCADE", unique=True, index=True
    )
    authenticated_user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE"
    )
    authenticated_user_email: str
    connector_id: uuid.UUID = Field(
        foreign_key="mcp_connector.id", ondelete="CASCADE"
    )
    oauth_client_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MCPSessionMetaPublic(SQLModel):
    id: uuid.UUID
    session_id: uuid.UUID
    authenticated_user_id: uuid.UUID
    authenticated_user_email: str
    connector_id: uuid.UUID
    oauth_client_id: str | None = None
    created_at: datetime
