"""
Agent REST API token model.

Scoped, opaque tokens that authenticate consumer calls to a producer agent's
REST API. Mirrors the security shape of ``AgentAccessToken`` (A2A) and the
webapp share token: the value is returned once at creation; only a SHA256 hash
is stored; the first 8 chars are kept as a display prefix.

Unlike A2A tokens, these are NOT JWTs — they are plain ``secrets.token_urlsafe``
opaque tokens validated by hash lookup. They are internal machine credentials
and never expire. A token may only ever *narrow* the producer's policy
(``read_only_override``), never widen it.

Tokens are never created manually. Each token is the secret behind one
``agent_api`` *connection* credential: it is minted by the "Connect Agent API"
helper and tied to the resulting credential via ``credential_id`` (ON DELETE
CASCADE). Deleting the credential — i.e. disconnecting the agents — removes the
token, which is the only way to revoke access.
"""
import uuid
from datetime import datetime, UTC

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


# Shared properties
class AgentApiTokenBase(SQLModel):
    label: str | None = Field(default=None, max_length=255)
    # A token may be MORE restrictive than the API's policy, never less.
    read_only_override: bool = Field(default=False)


# Properties to receive on token creation (internal — minted by the connect helper)
class AgentApiTokenCreate(AgentApiTokenBase):
    pass


# Database model
class AgentApiToken(AgentApiTokenBase, table=True):
    __tablename__ = "agent_api_token"
    __table_args__ = (
        sa.Index("ix_agent_api_token_agent_id", "agent_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE"
    )
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # The agent_api credential this token is the secret for. Deleting the
    # credential (disconnecting the agents) cascade-deletes the token. Nullable
    # only transiently: connect mints the token, creates the credential, then
    # back-fills this.
    credential_id: uuid.UUID | None = Field(
        default=None, foreign_key="credential.id", ondelete="CASCADE", index=True
    )
    # SHA256 hash of the opaque token value (never the value itself).
    token_hash: str = Field(max_length=64, unique=True, index=True)
    # First 8 chars of the token value, for display.
    token_prefix: str = Field(max_length=12)
    is_active: bool = Field(default=True)
    last_used_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Properties to return via API (never the value)
class AgentApiTokenPublic(AgentApiTokenBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    token_prefix: str
    is_active: bool
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


# Properties to return when creating a token (includes value + connection info once)
class AgentApiTokenCreated(AgentApiTokenPublic):
    """Returned only on creation — includes the actual token value once."""
    token: str          # the opaque token value (shown once)
    base_url: str       # absolute consumer-facing base URL for the proxy
    spec_url: str       # absolute consumer-facing OpenAPI spec URL


# ── "Connect to another agent" one-click helper ──────────────────────────────


class ConnectAgentApiRequest(SQLModel):
    """
    Request to wire a consumer to a producer's REST API in one action.

    Mints an ``agent_api`` token on the producer, creates an ``agent_api``
    credential pre-filled with {base_url, token, spec_url, label,
    producer_agent_id}, and optionally links it to a consumer agent.
    """
    # Optional friendly label for the token + credential (defaults to the
    # producer agent's name).
    credential_label: str | None = Field(default=None, max_length=255)
    # Token narrowing knob (mirror AgentApiTokenCreate).
    read_only_override: bool = Field(default=False)
    # Optional consumer agent to link the new credential to immediately.
    consumer_agent_id: uuid.UUID | None = None


class ConnectAgentApiResponse(SQLModel):
    """Result of the connect helper — IDs of what it created/linked."""
    credential_id: uuid.UUID
    token_id: uuid.UUID
    token_prefix: str
    base_url: str
    spec_url: str
    linked_consumer_agent_id: uuid.UUID | None = None


# ── Connection info (for the agent_api credential detail view) ────────────────


class AgentApiConnectedAgent(SQLModel):
    """A consumer agent that has the agent_api credential linked to it."""
    id: uuid.UUID
    name: str
    # Agent's UI colour preset, so the frontend renders the same Bot badge it
    # uses everywhere else for this agent.
    ui_color_preset: str | None = None
    # Owner's email — disambiguates identical agent names (e.g. multiple
    # bundle installs of the same agent owned by different users).
    owner_email: str | None = None


class AgentApiConnectionInfo(SQLModel):
    """
    What an ``agent_api`` credential connects to — surfaced on the credential
    detail page. ``producer_agent_name`` is best-effort (None if the producer
    agent is no longer accessible); ``consumer_agents`` are the agents the
    credential is currently linked to.
    """
    producer_agent_id: uuid.UUID | None
    producer_agent_name: str | None
    base_url: str
    spec_url: str
    read_only: bool
    consumer_agents: list[AgentApiConnectedAgent]


# ── Producer-side view: who is consuming my API ──────────────────────────────


class AgentApiProducerConnection(SQLModel):
    """
    One connection to a producer agent's API — surfaced on the producer's
    "Agent REST API" card (where the token list used to be). Each connection is
    one token (``token_id``) and, normally, the ``agent_api`` credential it
    backs plus the consumer agents that credential is linked to. Legacy tokens
    may have no credential (``credential_id`` is None) — they still expose
    ``token_id`` so they can be disconnected.
    """
    token_id: uuid.UUID
    credential_id: uuid.UUID | None
    credential_name: str | None
    token_prefix: str
    read_only: bool
    consumer_agents: list[AgentApiConnectedAgent]
    created_at: datetime


class AgentApiProducerConnections(SQLModel):
    data: list[AgentApiProducerConnection]
    count: int
