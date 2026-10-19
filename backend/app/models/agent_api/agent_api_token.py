"""
Agent REST API token model.

Scoped, opaque tokens that authenticate consumer calls to a producer agent's
REST API. Mirrors the security shape of ``AgentAccessToken`` (A2A) and the
webapp share token: only a SHA256 hash is stored; the first 8 chars are kept as
a display prefix.

Unlike A2A tokens, these are NOT JWTs — they are plain ``secrets.token_urlsafe``
opaque tokens validated by hash lookup. A token may only ever *narrow* the
producer's policy (``read_only_override``), never widen it.

``kind`` is the single source of truth for the two modes this table serves:

- ``connection`` — the machine credential behind an ``agent_api`` *connection*
  credential. Minted by the "Connect Agent API" helper, never shown to a human,
  never expires, anonymous by default (the caller identity rides the separate
  ``owner_identity_token`` header).
- ``external`` — an **issued key**. The producer's owner (the *issuer*,
  ``owner_id``) gives API access to a platform user (the *recipient*,
  ``subject_user_id``; the issuer themself on a self-issue). The key's
  ``agent_api`` credential is OWNED BY THE RECIPIENT, so only the recipient can
  reveal, rotate, or link it; the issuer manages access (scopes on the grant,
  revoke, kill switch) from the producer side.

Lifecycle of an issued key:

- **Soft revoke.** The issuer revoking sets ``is_active=False``, ``revoked_at``,
  ``revoked_reason``; the row and the recipient's credential survive so the
  recipient sees *why* the key stopped working. Only the recipient deleting the
  credential removes the row (``credential_id`` ON DELETE CASCADE).
- **Producer / issuer removed.** ``agent_id`` and ``owner_id`` are ON DELETE
  SET NULL; a NULL there is a terminal status (``producer_removed`` /
  ``issuer_removed``), never a match at the proxy.
- **Rotation grace.** The recipient rotating swaps ``token_hash`` in place and
  keeps the old hash in ``previous_token_hash`` until
  ``previous_token_expires_at`` (``AGENT_API_KEY_ROTATION_GRACE``), so a running
  script picks up the new value without a hard cut.

The single status function is ``agent_api_key_status.key_status``.
"""
import uuid
from datetime import datetime, UTC
from enum import Enum
from typing import Literal

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
    # The producer. SET NULL when the producer is deleted: the row survives so
    # the recipient sees "producer_removed"; a NULL never matches at the proxy.
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", nullable=True, ondelete="SET NULL"
    )
    # The issuer (the producer owner who minted the row). SET NULL when that
    # user is deleted ("issuer_removed").
    owner_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    # The agent_api credential this token is the secret for. Deleting the
    # credential (disconnecting the agents / the recipient deleting their copy)
    # cascade-deletes the token. Nullable
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
    # The recipient of an EXTERNAL key: the platform user it is issued to and
    # acts as (owner of its credential). NULL for connection tokens (anonymous
    # by construction). Immutable once issued.
    subject_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="CASCADE", index=True
    )
    # Optional expiry for external keys. NULL = never expires (always NULL for
    # connection tokens). Enforced in AgentApiTokenService.validate_token.
    expires_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True), nullable=True
    )
    is_active: bool = Field(default=True)
    # Soft revoke by the issuer. Only "revoked_by_issuer" is ever stored.
    revoked_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True), nullable=True
    )
    revoked_reason: str | None = Field(default=None, max_length=32)
    # Rotation grace: the hash replaced by the last rotate stays valid until
    # previous_token_expires_at. A second rotate replaces it.
    previous_token_hash: str | None = Field(
        default=None, max_length=64, index=True
    )
    previous_token_expires_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True), nullable=True
    )
    rotated_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True), nullable=True
    )
    # True when the issuer was shown the current value (legacy keys minted to
    # someone else before the recipient owned them). Cleared by a rotate.
    issuer_saw_value: bool = Field(
        default=False, sa_column_kwargs={"server_default": sa.false()}
    )
    last_used_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Properties to return via API (never the value)
class AgentApiTokenPublic(AgentApiTokenBase):
    id: uuid.UUID
    agent_id: uuid.UUID | None
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
    # True when the requesting user owns the producer agent: the connection's
    # Sharing card then points at the producer's "Give API access" (plan D10).
    producer_owned_by_viewer: bool = False
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


# ── Issued keys (producer → a platform user) ─────────────────────────────────


class AgentApiKeyStatus(str, Enum):
    """The one status vocabulary of an issued key (plan D4).

    Precedence top-down; computed only by ``agent_api_key_status.key_status``.
    The first four are terminal; the last two recover by themselves.
    """

    ACTIVE = "active"
    REVOKED_BY_ISSUER = "revoked_by_issuer"
    PRODUCER_REMOVED = "producer_removed"
    ISSUER_REMOVED = "issuer_removed"
    EXPIRED = "expired"
    API_DISABLED = "api_disabled"
    EXTERNAL_ACCESS_PAUSED = "external_access_paused"


AgentApiKeyUnavailableReason = Literal["key_revoked", "key_expired", "key_paused"]


class AgentApiKeyCreate(SQLModel):
    """Request to issue an API key on a producer agent.

    ``scopes`` is a convenience: it **upserts** the ``agent_api_access_grant``
    for ``(producer, recipient)``. Scopes live on the grant, never on the key —
    the same row the producer's API access surface edits.
    """

    label: str | None = Field(default=None, max_length=255)
    # Who the key is issued to (the recipient, who will own its credential).
    # None = the issuer themself. Immutable once issued.
    issued_to_user_id: uuid.UUID | None = None
    # Slot id stamped on the recipient's credential. None = the default
    # ``<slug(producer name)>-api`` (AgentApiKeyService.default_service_uri).
    service_uri: str | None = None
    # Optional scope set to write onto the (producer, recipient) grant. ``None``
    # leaves an existing grant untouched; ``[]`` clears it to "known user, no
    # scopes".
    scopes: list[str] | None = None
    read_only_override: bool = Field(default=False)
    # Optional expiry, in days from now. ``None`` = never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class AgentApiKeyUser(SQLModel):
    """A platform user shown next to a key (recipient or issuer)."""

    id: uuid.UUID
    email: str | None = None
    full_name: str | None = None


class AgentApiKeyPublic(SQLModel):
    """One issued key as the ISSUER sees it. Never the value."""

    id: uuid.UUID
    credential_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    label: str | None
    token_prefix: str
    issued_to: AgentApiKeyUser | None
    read_only: bool
    is_active: bool
    status: AgentApiKeyStatus
    # Convenience for the UI: ``status == active``. The proxy re-derives
    # everything on every call (validate_token) — this is display only.
    is_usable: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None = None
    rotated_at: datetime | None = None
    # How many of the recipient's agents the key is linked to. A count only:
    # the issuer never sees the recipient's agent names (plan D30).
    linked_agent_count: int = 0
    created_at: datetime


class AgentApiKeyCreated(AgentApiKeyPublic):
    """Returned on issue. ``token`` only when issued to the caller themself."""

    token: str | None = None
    base_url: str
    spec_url: str


class AgentApiAccessEntry(SQLModel):
    """One person with access to a producer: their grant and their keys."""

    user: AgentApiKeyUser
    grant_id: uuid.UUID | None = None
    scopes: list[str] = []
    keys: list[AgentApiKeyPublic] = []


class AgentApiAccessGroupEntry(SQLModel):
    """One user group holding API scopes on a producer (scopes only, no keys)."""

    grant_id: uuid.UUID
    group_id: uuid.UUID
    name: str
    # Active members only.
    member_count: int = 0
    scopes: list[str] = []


class AgentApiAccessList(SQLModel):
    """The producer's API access surface: grants and issued keys per person,
    plus the user groups holding scopes (sorted by name)."""

    data: list[AgentApiAccessEntry]
    count: int
    groups: list[AgentApiAccessGroupEntry] = []
    external_access_enabled: bool
    identity_enabled: bool


class AgentApiKeyProducer(SQLModel):
    """The producer an issued key calls, as the recipient sees it."""

    id: uuid.UUID
    name: str
    ui_color_preset: str | None = None


class AgentApiKeyHolderInfo(SQLModel):
    """An issued key as its RECIPIENT (the credential owner) sees it."""

    credential_id: uuid.UUID
    token_prefix: str
    status: AgentApiKeyStatus
    service_uri: str | None
    producer: AgentApiKeyProducer | None
    issuer: AgentApiKeyUser | None
    scopes: list[str]
    read_only: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    rotated_at: datetime | None
    # End of the rotation grace window for the previous value, if still open.
    previous_valid_until: datetime | None
    issuer_saw_value: bool
    base_url: str | None
    spec_url: str | None
    linked_agents: list[AgentApiConnectedAgent]


class AgentApiKeyRotateResponse(SQLModel):
    """The new value of a rotated key (returned once; reveal still works)."""

    token: str
    token_prefix: str
    previous_valid_until: datetime


class AgentApiKeyRevealResponse(SQLModel):
    """The value of an issued key, returned by the dedicated reveal endpoint.

    The way to read a key back after issue: ``with-data`` deliberately strips
    it, so every reveal goes through one audited call.
    """

    token: str
