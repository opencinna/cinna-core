"""
Agent REST API token model.

Scoped, opaque tokens that authenticate consumer calls to a producer agent's
REST API. Mirrors the security shape of ``AgentAccessToken`` (A2A) and the
webapp share token: the value is returned once at creation; only a SHA256 hash
is stored; the first 8 chars are kept as a display prefix.

Unlike A2A tokens, these are NOT JWTs — they are plain ``secrets.token_urlsafe``
opaque tokens validated by hash lookup. A token may only ever *narrow* the
producer's policy (``read_only_override``), never widen it.

``kind`` is the single source of truth for the two modes this table serves
(plan §2):

- ``connection`` — the machine credential behind an ``agent_api`` *connection*
  credential. Minted by the "Connect Agent API" helper, never shown to a human,
  never expires, anonymous by default (the caller identity rides the separate
  ``owner_identity_token`` header).
- ``external`` — an **external key** a human copies into a laptop script, server,
  or cron job. Revealable, identity-bound (``subject_user_id``), optionally
  expiring (``expires_at``). Minted only from the producer's key routes and only
  while ``Agent.agent_api_external_access_enabled`` is on.

Both modes bind to their ``agent_api`` credential via ``credential_id``
(ON DELETE CASCADE), so deleting the credential is the single revocation path
for either. ``owner_id`` is always the producer owner who minted the row; for an
external key the issuer (``owner_id``) and the identity (``subject_user_id``) are
frequently different people.
"""
import uuid
from datetime import datetime, UTC
from enum import Enum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class AgentApiTokenKind(str, Enum):
    """Which of the two products (plan §2) a token row belongs to.

    Stored as a plain ``VARCHAR(20)`` — deliberately NOT a Postgres native enum:
    ``ALTER TYPE`` is non-transactional and adding a value later is a migration
    hazard this repo has been bitten by.
    """

    CONNECTION = "connection"
    EXTERNAL = "external"


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
    # "connection" | "external" — see AgentApiTokenKind. Plain varchar.
    kind: str = Field(
        default=AgentApiTokenKind.CONNECTION.value, max_length=20, nullable=False
    )
    # The platform user an EXTERNAL key acts as. NULL for connection tokens
    # (they are anonymous by construction). Immutable once issued (plan D6):
    # changing who a key represents means revoke + re-issue.
    subject_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="CASCADE", index=True
    )
    # Optional expiry for external keys. NULL = never expires (always NULL for
    # connection tokens). Enforced in AgentApiTokenService.validate_token.
    expires_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True), nullable=True
    )
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
    # Owner's name + email — disambiguate identical agent names (e.g. multiple
    # bundle installs of the same agent owned by different users).
    owner_name: str | None = None
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
    # Producer's UI colour preset, so the frontend renders the same Bot badge
    # it uses for any other agent.
    producer_ui_color_preset: str | None = None
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


# ── External keys (producer → outside world) ─────────────────────────────────


class AgentApiKeyCreate(SQLModel):
    """Request to mint an external API key on a producer agent (plan D9).

    ``scopes`` is a convenience: it **upserts** the ``agent_api_access_grant``
    for ``(producer, subject_user_id)``. Scopes live on the grant, never on the
    key (plan D5) — the same row the producer's Access & Scopes card edits.
    """

    label: str | None = Field(default=None, max_length=255)
    # The platform user this key acts as. Immutable once issued (plan D6).
    subject_user_id: uuid.UUID
    # Optional scope set to write onto the (producer, subject) grant. ``None``
    # leaves an existing grant untouched; ``[]`` clears it to "known user, no
    # scopes".
    scopes: list[str] | None = None
    read_only_override: bool = Field(default=False)
    # Optional expiry, in days from now. ``None`` = never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class AgentApiKeySubject(SQLModel):
    """The platform user an external key is bound to."""

    id: uuid.UUID
    email: str | None = None
    full_name: str | None = None


class AgentApiKeyPublic(SQLModel):
    """One external key as listed on the producer card. Never the value."""

    id: uuid.UUID
    credential_id: uuid.UUID | None
    agent_id: uuid.UUID
    label: str | None
    token_prefix: str
    subject: AgentApiKeySubject | None
    read_only: bool
    is_active: bool
    # Convenience for the UI: is_active AND not past expires_at AND the
    # producer's agent_api_external_access_enabled. That third term is the one
    # worth remembering — it is why a key can be active and unexpired yet still
    # unusable, and it is what the detail page's "Blocked" badge keys off. The
    # proxy re-derives all of it on every call (validate_token) — this is
    # display only.
    is_usable: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class AgentApiKeyCreated(AgentApiKeyPublic):
    """Returned on mint — carries the token value plus where to call it."""

    token: str
    base_url: str
    spec_url: str


class AgentApiKeysPublic(SQLModel):
    data: list[AgentApiKeyPublic]
    count: int


class AgentApiKeyRevealResponse(SQLModel):
    """The value of an external key, returned by the dedicated reveal endpoint.

    The **only** way to read a key back after mint (plan D4): ``with-data``
    deliberately strips it, so every reveal goes through one audited call.
    """

    token: str
