"""ServerChannel — one row per admin-configured, server-wide channel instance.

A *channel* is an inbound transport that lets people outside the platform
(e.g. company employees on Google Chat) talk to platform agents. Each row
owns its transport trust model (``config`` + ``encrypted_secrets``), an
email-pattern whitelist, and an auto-registration toggle.

``channel_type`` must resolve in the adapter registry
(``app.services.server_channels.adapters.registry``); validation happens at
create/update time in the service layer, not here — the model stays a plain
value container so new adapters need no schema change.

Secrets discipline: ``encrypted_secrets`` is Fernet-encrypted at rest and is
**write-only**. No response DTO carries it; ``ServerChannelPublic`` exposes
only ``has_outbound_credentials``.

Availability discipline: the four policy columns on ``ServerChannelBase``
(``visibility``, ``default_enabled_for_users``, ``default_agent_scope``,
``allow_auto_install``) are the admin-owned **defaults**. What a given person
actually gets is the *resolution* of those defaults against their optional
``channel_user_setting`` row, and that resolution lives in exactly one place:
``app.services.server_channels.channel_policy_service.ChannelPolicyService``.
Nothing else may re-derive it — a second copy of the inherit rules is how a
channel ends up on for a user the admin switched off.

``ServerChannelPublic`` is the **admin** projection and carries
``webhook_token``, ``config`` and ``email_whitelist``. The user-facing routes
must never return it; see ``UserChannelPublic`` in ``channel_user_setting.py``.
"""
import uuid
from datetime import UTC, datetime

from pydantic import model_validator
from sqlalchemy import JSON, Text, UniqueConstraint
from sqlmodel import Column, Field, SQLModel


# ---------------------------------------------------------------------------
# Policy value strings
#
# Plain ``VARCHAR`` columns, not Postgres enums — the same status-string
# convention ``channel_type``, ``ChannelThreadBinding.status`` and
# ``RoutingDecision.origin`` already follow. Adding a value is a code change,
# not a migration, and a reader that meets an unrecognised value must degrade
# conservatively rather than raise. See ``ChannelPolicyService`` for the one
# place those degradation rules live.
# ---------------------------------------------------------------------------

#: Every platform user may use the channel.
CHANNEL_VISIBILITY_PUBLIC = "public"
#: Only users with a ``server_channel_user_grant`` row may use the channel.
CHANNEL_VISIBILITY_RESTRICTED = "restricted"

CHANNEL_VISIBILITIES = (
    CHANNEL_VISIBILITY_PUBLIC,
    CHANNEL_VISIBILITY_RESTRICTED,
)

#: Every agent the user owns is a routing candidate.
CHANNEL_AGENT_SCOPE_ALL = "all"
#: Only the agents on the user's ``channel_user_agent`` list are candidates.
CHANNEL_AGENT_SCOPE_LIST = "list"
#: No agent is a candidate — the user must opt agents in before anything routes.
CHANNEL_AGENT_SCOPE_NONE = "none"

CHANNEL_AGENT_SCOPES = (
    CHANNEL_AGENT_SCOPE_ALL,
    CHANNEL_AGENT_SCOPE_LIST,
    CHANNEL_AGENT_SCOPE_NONE,
)


class ServerChannelBase(SQLModel):
    """Shared, non-secret channel properties (create / update / read)."""

    # Adapter key, e.g. "google_chat". Indexed on the table subclass only —
    # ``index=`` is inert on non-table SQLModel classes.
    channel_type: str = Field(min_length=1, max_length=64, index=True)
    name: str = Field(min_length=1, max_length=255)
    # A disabled channel's webhook answers 404 — no existence leak.
    enabled: bool = Field(default=True)
    # Create a passwordless, confirmed account for a whitelisted sender that
    # has no platform user yet. Off by default.
    auto_register_users: bool = Field(default=False)

    # --- Admin-owned availability policy -------------------------------
    #
    # These four are the *defaults* a user with no ``channel_user_setting``
    # row inherits. They are not the user's stored settings: the inherit
    # rules live in ``ChannelPolicyService.resolve`` and nowhere else.
    #
    # Every default below reproduces the behaviour this feature had before
    # the policy model existed, so an existing channel is unchanged by the
    # migration that adds them.

    #: ``"public"`` (default) or ``"restricted"``. When restricted, a
    #: ``server_channel_user_grant`` row is required. Any value that is not
    #: exactly ``"public"`` is treated as restricted — the permissive branch
    #: is the narrow one, so an unrecognised value fails closed.
    visibility: str = Field(default=CHANNEL_VISIBILITY_PUBLIC, max_length=32)
    #: Whether a user who has never touched their settings is switched on.
    default_enabled_for_users: bool = Field(default=True)
    #: ``"all"`` / ``"list"`` / ``"none"`` — the agent scope inherited by a
    #: user with no explicit ``agent_scope``.
    default_agent_scope: str = Field(default=CHANNEL_AGENT_SCOPE_ALL, max_length=32)
    #: Whether routing Pass 2 (classify against the auto-install catalog and
    #: install the winner) may run for this channel. Google Chat did this
    #: unconditionally before the flag existed, hence the ``True`` default.
    allow_auto_install: bool = Field(default=True)


class ServerChannel(ServerChannelBase, table=True):
    """Database model for a configured channel instance."""

    __tablename__ = "server_channel"
    __table_args__ = (
        UniqueConstraint("name", name="uq_server_channel_name"),
        UniqueConstraint("webhook_token", name="uq_server_channel_webhook_token"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Non-secret adapter config. Google Chat: {"project_number": "<GCP number>"}
    # (the JWT audience). Shape is validated by the adapter, not the model.
    # Plain JSON column: in-place mutation (``channel.config["k"] = v``) is NOT
    # dirty-tracked. Assign a new dict, or call
    # ``sqlalchemy.orm.attributes.flag_modified(channel, "config")`` before
    # committing — the convention used across this codebase.
    config: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    # Fernet-encrypted JSON blob of adapter secrets (Google Chat: the outbound
    # service-account JSON). Never returned by any endpoint, never logged.
    encrypted_secrets: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Comma-separated fnmatch patterns ("*@example.com, devops.*@support.com").
    # NULL/empty means DENY ALL — the whitelist fails closed. "*" allows any
    # sender the transport has verified. Matched via
    # ``app.services.common.email_patterns.match_email_pattern``.
    email_whitelist: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Unguessable path segment of the public webhook URL. Minted at create time
    # *only* for a transport that declares ``needs_webhook_token``; regenerable
    # via the update DTO's ``regenerate_webhook_token`` flag. A transport that
    # is not reached by a webhook (a polled one) keeps ``None`` here, and that
    # is the sole signal ``webhook_url`` reads to decide there is no URL to
    # show.
    #
    # ``None``, never ``""``: ``__table_args__`` carries a plain
    # ``UniqueConstraint`` on this column, and in PostgreSQL ``''`` is a value
    # — so the *second* tokenless channel would trip the constraint with an
    # unhandled IntegrityError, whereas UNIQUE permits any number of NULLs.
    #
    # No separate `index=True`: the unique constraint above already backs it
    # with a btree index, which is what the per-request token lookup uses.
    webhook_token: str | None = Field(default=None, max_length=64)

    created_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ServerChannelCreate(ServerChannelBase):
    """Admin create payload."""

    config: dict = Field(default_factory=dict)
    email_whitelist: str | None = None
    # Raw (unencrypted) adapter secrets as supplied by the admin — for Google
    # Chat, the service-account JSON. Encrypted by the service before storage
    # and never echoed back.
    secrets: str | None = None


class ServerChannelUpdate(SQLModel):
    """Admin patch payload — every field optional.

    ``secrets`` is only written when a non-empty value is supplied, so a form
    round-trip that leaves the write-only field untouched keeps the stored
    credential.
    """

    channel_type: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    auto_register_users: bool | None = None
    # Availability policy. All optional; an omitted field is left untouched,
    # like every other field on this DTO.
    visibility: str | None = Field(default=None, max_length=32)
    default_enabled_for_users: bool | None = None
    default_agent_scope: str | None = Field(default=None, max_length=32)
    allow_auto_install: bool | None = None
    config: dict | None = None
    email_whitelist: str | None = None
    secrets: str | None = None
    # Explicit action flag: mint a fresh webhook token (breaks the existing
    # channel-side configuration until the new URL is pasted back).
    regenerate_webhook_token: bool = False


class ServerChannelPublic(ServerChannelBase):
    """Admin read projection. Carries no secret material."""

    id: uuid.UUID
    config: dict = Field(default_factory=dict)
    email_whitelist: str | None = None
    # NULL for a channel whose transport is not reached by a webhook. Nullable
    # but still *required* (no default), like the two below: the point is that a
    # projection which forgets the field fails loudly rather than reporting a
    # plausible-looking absence.
    webhook_token: str | None
    # Both fields below are derived by the service, not columns. Deliberately
    # required (no default): a service method that forgets to populate them
    # fails loudly instead of returning a plausible ""/false.
    # Full public webhook URL, assembled from the configured backend host.
    # NULL exactly when ``webhook_token`` is — a URL built without a token
    # would point at an endpoint that can only ever refuse.
    webhook_url: str | None
    # True when outbound credentials are stored (never the credential itself).
    has_outbound_credentials: bool
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ChannelTypePublic(SQLModel):
    """One registered adapter, for the admin type picker *and its form*.

    Carries the transport shape as well as the label, because the admin form
    has to decide which controls exist at all — a transport with no webhook,
    no channel secret and no external senders must not be offered a secrets
    box, a sender whitelist or an auto-registration switch. Every one of those
    would be a value nothing reads, and the whitelist is worse than useless:
    it is fail-closed, so an empty one renders as "this channel denies
    everyone" on a channel where it denies nobody.

    Declared facts, projected — never re-derived. The frontend must branch on
    these fields and never on ``channel_type``, for the same reason nothing in
    the backend does: a type check is a rule that has to be found and edited
    again for the next transport, and the one that gets missed is the one that
    silently shows the wrong form.

    All four are required (no defaults), like the derived fields on
    ``ServerChannelPublic``: a projection that forgets one fails loudly rather
    than reporting a plausible-looking ``False``.
    """

    channel_type: str
    display_name: str
    #: ``"webhook"`` | ``"polled"`` | ``"authenticated"`` — see
    #: ``ChannelInboundMode``. An ``authenticated`` transport resolves no
    #: external sender, so it has nothing to whitelist and nobody to register.
    inbound_mode: str
    #: Whether channels of this type are reachable at a webhook URL.
    needs_webhook_token: bool
    #: Whether this type's outbound credential lives in ``encrypted_secrets``
    #: (and therefore whether the write-only secrets field means anything).
    needs_outbound_credentials: bool
    #: Whether at most one channel of this type may exist. The picker offers
    #: no second one, and the list offers no delete for it.
    is_singleton: bool


class ChannelTestOutboundRequest(SQLModel):
    """Admin "does the credential work?" probe.

    Exactly one target must be supplied:

    - ``email`` — a person the platform has already seen on this channel. It is
      resolved *locally*, to a thread we recorded from one of their inbound
      events, never handed to the provider. Google Chat's ``users/{email}``
      alias exists but is documented as user-authentication only, and this
      adapter authenticates as an app — so an email the platform has never
      observed cannot be turned into a destination at all. That is a real
      limit, surfaced as an actionable error rather than a silent failure.
    - ``thread_key`` — the channel-native identity (Google Chat: ``spaces/AAA``
      or ``spaces/AAA/threads/BBB``). The escape hatch, and what the debug
      panel's "reply here" action sends.
    """

    email: str | None = Field(default=None, max_length=255)
    thread_key: str | None = Field(default=None, min_length=1, max_length=512)
    text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "ChannelTestOutboundRequest":
        email = (self.email or "").strip()
        thread_key = (self.thread_key or "").strip()
        if bool(email) == bool(thread_key):
            raise ValueError(
                "Supply exactly one of 'email' or 'thread_key' as the test target."
            )
        return self


class ChannelDebugEventPublic(SQLModel):
    """One captured event in the admin debug feed.

    Read-only projection of the in-memory ring buffer — see
    ``services/server_channels/channel_debug_buffer.py`` for why this is not
    persisted.
    """

    id: str
    at: datetime
    direction: str
    kind: str
    summary: str
    sender_email: str | None = None
    sender_display_name: str | None = None
    thread_key: str | None = None
    text: str | None = None
    detail: dict[str, str] = Field(default_factory=dict)
    # Consecutive identical events collapse into one row; ``at`` is then the
    # most recent occurrence. Keeps a retry storm readable and stops a repeated
    # request from flushing the bounded buffer.
    repeat: int = 1


class ChannelDebugEventsPublic(SQLModel):
    """The debug feed plus the bound it is subject to."""

    events: list[ChannelDebugEventPublic] = Field(default_factory=list)
    # Surfaced so the panel can say "last N" honestly instead of implying the
    # list is everything that ever happened.
    buffer_size: int
    # When this backend process started capturing. A restart empties the
    # buffer, so without this an empty feed is indistinguishable from a
    # webhook that never fired — the most confusing failure mode the panel
    # has.
    capturing_since: datetime


class ChannelRecentSender(SQLModel):
    """A person this channel has seen, and the thread to reach them on.

    Sourced from thread bindings (durable) merged with the debug buffer (live),
    so someone who has only just messaged is selectable before their binding
    exists.
    """

    email: str
    display_name: str | None = None
    thread_key: str
    last_seen: datetime | None = None
    # True when the thread came from a persisted binding rather than the buffer.
    bound: bool = False


class ChannelTestOutboundResult(SQLModel):
    """Outcome of a test send. ``error`` is admin-facing, never the raw secret."""

    success: bool
    external_message_id: str | None = None
    error: str | None = None


class ChannelSetupInstructions(SQLModel):
    """Adapter-shaped setup guidance shown after create / on demand."""

    channel_type: str
    # NULL for a transport with no webhook. Required-but-nullable on purpose:
    # a caller that forgets to populate it fails loudly, while a channel that
    # genuinely has no inbound URL says so instead of being handed a
    # live-looking one that nothing will ever answer.
    webhook_url: str | None
    # Adapter-specific key/value reminders, e.g. {"audience": "<project number>"}.
    details: dict[str, str] = Field(default_factory=dict)
    # Ordered, human-readable configuration steps.
    steps: list[str] = Field(default_factory=list)
