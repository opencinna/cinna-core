"""
CLI Setup Token model.

Short-lived, single-use token embedded in the setup command URL.
Created when user clicks "Setup" in the UI, consumed when the setup script runs.
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel


class CLISetupTokenBase(SQLModel):
    agent_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    owner_id: uuid.UUID


class CLISetupToken(CLISetupTokenBase, table=True):
    __tablename__ = "cli_setup_token"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Random URL-safe string (32 chars), unique and indexed
    token: str = Field(max_length=64, index=True, unique=True)
    # Nullable so account setup tokens (kind="account") have no single agent.
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", nullable=True, ondelete="CASCADE"
    )
    environment_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="agent_environment.id",
        nullable=True,
        ondelete="SET NULL",
    )
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    # "agent" = per-agent setup token (default, existing).
    # "account" = account bootstrap setup token (agent_id is NULL).
    kind: str = Field(default="agent", max_length=20)
    is_used: bool = Field(default=False)
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CLISetupTokenPublic(CLISetupTokenBase):
    id: uuid.UUID
    token: str
    is_used: bool
    expires_at: datetime
    created_at: datetime


class CLISetupTokenCreate(SQLModel):
    agent_id: uuid.UUID


class CLISetupTokenCreated(SQLModel):
    """Returned when a setup token is created — includes the setup command."""
    id: uuid.UUID
    token: str
    agent_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    expires_at: datetime
    created_at: datetime
    setup_command: str  # The full curl | python3 oneliner
