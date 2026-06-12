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
    # Nullable so account tokens (token_type="cli-account") can omit a single
    # agent. Per-agent tokens (token_type="cli") still carry a concrete agent_id.
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", nullable=True, ondelete="CASCADE"
    )
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    name: str = Field(max_length=100)
    # SHA-256 hash of the JWT token
    token_hash: str = Field(unique=True, index=True)
    # First 12 chars of the JWT for identification
    prefix: str = Field(max_length=12)
    # "cli" = per-agent token (default, existing). "cli-account" = account token.
    token_type: str = Field(default="cli", index=True, max_length=20)
    # Child-token provenance: set on a per-agent token minted by an account
    # token. Self-referential FK with CASCADE — deleting the account row deletes
    # its children. Revocation is the primary teardown mechanism (see service).
    minted_by_account_token_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="cli_token.id",
        nullable=True,
        index=True,
        ondelete="CASCADE",
    )
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
    agent_id: uuid.UUID | None
    owner_id: uuid.UUID
    prefix: str
    token_type: str
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


class CLIAccountTokenPublic(SQLModel):
    """Public projection of an account CLI token, with synced-child count."""
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    prefix: str
    is_revoked: bool
    last_used_at: datetime | None
    machine_info: str | None
    expires_at: datetime
    created_at: datetime
    # Number of active (non-revoked, non-expired) per-agent child tokens this
    # account token has minted — i.e. how many agents are synced from it.
    child_count: int


class CLIAccountTokensPublic(SQLModel):
    data: list[CLIAccountTokenPublic]
    count: int


class CLITokenPayload(SQLModel):
    """JWT payload for CLI tokens."""
    sub: str  # Token ID (UUID)
    agent_id: str | None = None  # Agent UUID (None for account tokens)
    owner_id: str  # User UUID
    token_type: str = "cli"  # "cli" (per-agent) or "cli-account"
    exp: int  # Expiration timestamp
