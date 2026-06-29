"""
Agent Webhook models.

One configured webhook attached to an agent. Two flavors, mirroring the
``static_prompt`` / ``script_trigger`` split used by AgentSchedule:

- ``session``: start a new agent session seeded with the incoming HTTP payload.
- ``script``: run a shell command inside the agent environment with the
  payload exposed as a JSON env var (plus optional stdin).

Authentication on the public endpoint uses a Fernet-encrypted bearer token —
the same pattern as Task Triggers.
"""
import uuid
from datetime import datetime, UTC
from typing import Literal

from sqlalchemy import Text, Index
from sqlmodel import Field, SQLModel


class AgentWebhookType:
    """Webhook type constants."""
    SESSION = "session"
    SCRIPT = "script"
    # GitOps trigger: a git push provider (GitHub/GitLab) calls the webhook so
    # the agent's git source pulls the latest revision. Rides the same token /
    # log infra as the other types; carries no type-specific fields.
    GIT_SOURCE = "git_source"


# ============================== Database model ==============================


class AgentWebhook(SQLModel, table=True):
    """
    Agent webhook configuration.

    Relationship: Many AgentWebhook → One Agent (an agent can have multiple
    webhooks — e.g. a session webhook for GitHub push events + a script
    webhook for a health probe).
    """
    __tablename__ = "agent_webhook"
    __table_args__ = (
        Index("ix_agent_webhook_agent_id", "agent_id"),
        Index("ix_agent_webhook_owner_id", "owner_id"),
        Index("ix_agent_webhook_webhook_id", "webhook_id", unique=True),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")

    # Type discriminator. Immutable after creation.
    type: str = Field(nullable=False)  # "session" | "script"

    # Identity
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = Field(default=True)

    # Optional static payload template prepended/beside the dynamic payload.
    payload_template: str | None = Field(default=None, max_length=10000, sa_type=Text)

    # ---- Session-type fields ----
    # Custom starting prompt. NULL → fall back to agent.entrypoint_prompt,
    # then to a generic default at fire time.
    prompt: str | None = Field(default=None, sa_type=Text)
    # "conversation" or "building". NULL for script-type webhooks.
    session_mode: str | None = Field(default=None)

    # ---- Script-type fields ----
    # Single-line shell command to execute. Max 2000 chars enforced in schemas.
    command: str | None = Field(default=None, sa_type=Text)
    # Timeout in seconds (max 300 = /exec hard cap). NULL for session webhooks.
    command_timeout_seconds: int | None = Field(default=None)

    # Token / URL slug
    webhook_id: str = Field(nullable=False)  # short URL-safe slug
    webhook_token_encrypted: str = Field(nullable=False)
    webhook_token_prefix: str = Field(nullable=False, max_length=8)

    # Execution tracking
    last_execution: datetime | None = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ============================== Create schemas ==============================


class AgentWebhookCreateSession(SQLModel):
    """Create payload for a session-type webhook."""
    name: str = Field(min_length=1, max_length=255)
    type: Literal["session"] = "session"
    payload_template: str | None = Field(default=None, max_length=10000)
    prompt: str | None = Field(default=None)
    session_mode: Literal["conversation", "building"] = "conversation"


class AgentWebhookCreateScript(SQLModel):
    """Create payload for a script-type webhook."""
    name: str = Field(min_length=1, max_length=255)
    type: Literal["script"] = "script"
    payload_template: str | None = Field(default=None, max_length=10000)
    command: str = Field(min_length=1, max_length=2000)
    command_timeout_seconds: int = Field(default=120, ge=1, le=300)


class AgentWebhookCreateGitSource(SQLModel):
    """Create payload for a git-source (GitOps) webhook.

    A git-source webhook carries no type-specific fields — firing it simply
    triggers the agent's git source ``pull_update``. ``payload_template`` is
    accepted for parity / future use (e.g. asserting the pushed ref) but is not
    required.
    """
    name: str = Field(min_length=1, max_length=255)
    type: Literal["git_source"] = "git_source"
    payload_template: str | None = Field(default=None, max_length=10000)


# ============================== Update schema ==============================


class AgentWebhookUpdate(SQLModel):
    """
    Update payload. All fields optional.

    ``type`` is intentionally excluded — immutable after creation. Fields that
    don't match the webhook's actual type are rejected in the service layer.
    """
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    payload_template: str | None = Field(default=None, max_length=10000)

    # Session-type
    prompt: str | None = None
    session_mode: Literal["conversation", "building"] | None = None

    # Script-type
    command: str | None = Field(default=None, min_length=1, max_length=2000)
    command_timeout_seconds: int | None = Field(default=None, ge=1, le=300)


# ============================== Public schemas ==============================


class AgentWebhookPublic(SQLModel):
    """Public response model — never includes the full plaintext token."""
    id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID
    type: str
    name: str
    enabled: bool
    payload_template: str | None

    # Session-type fields
    prompt: str | None = None
    session_mode: str | None = None

    # Script-type fields
    command: str | None = None
    command_timeout_seconds: int | None = None

    # Token-related (non-sensitive)
    webhook_id: str
    webhook_token_prefix: str
    webhook_url: str | None = None  # computed from webhook_id

    # Execution tracking
    last_execution: datetime | None = None

    # Timestamps
    created_at: datetime
    updated_at: datetime


class AgentWebhookPublicWithToken(AgentWebhookPublic):
    """
    Returned only on creation or regenerate-token. Includes the full plaintext
    bearer token — UI must prompt the user to copy it immediately.
    """
    webhook_token: str


class AgentWebhooksPublic(SQLModel):
    """List response."""
    data: list[AgentWebhookPublic]
    count: int
