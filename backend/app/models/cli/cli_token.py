"""
CLI Token model.

Long-lived JWT session token stored on the user's machine.
Created by exchanging a setup token. Supports revocation from the UI.
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel
from sqlalchemy import DateTime, Column


class CLITokenBase(SQLModel):
    name: str = Field(max_length=100)


class CLIToken(CLITokenBase, table=True):
    __tablename__ = "cli_token"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    name: str = Field(max_length=100)
    # SHA-256 hash of the JWT token
    token_hash: str = Field(unique=True, index=True)
    # First 12 chars of the JWT for identification
    prefix: str = Field(max_length=12)
    is_revoked: bool = Field(default=False)
    last_used_at: datetime | None = Field(default=None)
    # Optional: OS/hostname from setup script
    machine_info: str | None = Field(default=None, max_length=200)
    # Renewed on each use; expires after 7 days of inactivity
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # CLI live sync: last time a sync WebSocket connected from this token
    last_sync_connected_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class CLITokenCreate(SQLModel):
    agent_id: uuid.UUID
    name: str
    machine_info: str | None = None


class CLITokenPublic(CLITokenBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID
    prefix: str
    is_revoked: bool
    last_used_at: datetime | None
    machine_info: str | None
    expires_at: datetime
    created_at: datetime
    last_sync_connected_at: datetime | None


class CLITokenCreated(CLITokenPublic):
    """Returned only on token creation — includes the actual JWT value shown once."""
    token: str


class CLITokensPublic(SQLModel):
    data: list[CLITokenPublic]
    count: int


class CLITokenPayload(SQLModel):
    """JWT payload for CLI tokens."""
    sub: str  # Token ID (UUID)
    agent_id: str  # Agent UUID
    owner_id: str  # User UUID
    token_type: str = "cli"  # Differentiates from regular user tokens
    exp: int  # Expiration timestamp
