"""ChannelUserSetting — one person's overrides for one channel, and its agent list.

READ THIS BEFORE CHANGING A COLUMN'S NULLABILITY
------------------------------------------------
``is_enabled`` and ``agent_scope`` are **nullable with no server default, and
NULL means "inherit the channel default"**. They are not
``bool NOT NULL DEFAULT true`` / ``varchar NOT NULL DEFAULT 'all'``, and
"tidying" them into that shape is the single most damaging change that can be
made to this table.

Why: a stored value *freezes* the user against a later change of the admin
default. If a user who never expressed an opinion has ``is_enabled = true``
written into their row, an admin who later flips
``ServerChannel.default_enabled_for_users`` to ``false`` switches the channel
off for everyone **except** the people who happen to have a row — which is the
opposite of what "default" means, and is invisible in the UI because the row
looks exactly like a deliberate user choice. NULL follows the admin; a value
does not. Only an explicit user action ever writes a non-NULL value here.

The corollary is the rule the whole phase rests on (master plan §3.3):

    **The absence of a row means "the channel default applies."**

Never require a row to exist. Two populations will never have one:

- users the channel auto-registered (Google Chat / email senders) — they have
  no UI session in which a row could be created;
- every user created after the channel was configured.

The naive design — materialize a row per user when the channel is created —
is wrong for both. Rows are created **lazily, on the user's first edit**, in
``PUT /users/me/channels/{channel_id}`` and nowhere else. A read path that
creates a row here is a bug, not an optimisation.

``allow_identity_routing`` IS THE EXCEPTION
-------------------------------------------
It is ``NOT NULL DEFAULT false`` and **never inherits**. There is deliberately
no channel-level default for it, and an admin default must not be able to turn
it on for someone who never agreed (master plan §3.4).

**Read the direction carefully — it is the row owner's own switch, and this
row's owner is the *sender*.** Phase 3 resolves the policy for the person whose
message arrived and gates on ``policy.allow_identity_routing`` before identity
candidates join their ballot (``phase_3_identity_over_channels.md`` §2.1). So
this column says "I accept that a message I send on this channel may be routed
into somebody else's workspace, where they can read it" — the consent the §2.5
UI copy asks for. It is emphatically **not** the receiving person's control
over who may reach them; that is ``IdentityAgentBinding.is_active`` and
``IdentityBindingAssignment.is_active``, both written by the owner.

Its neighbour ``IdentityBindingAssignment.is_enabled`` is a *third* thing again,
and points the same way this column does: it is the **caller's** per-person
opt-out of addressing one identity owner (the row is keyed by
``target_user_id``, and ``IdentityService.toggle_identity_contact`` filters on
``target_user_id == current_user.id``). One person-level toggle, governing every
surface. So this column and that one are both the sender's switches, at
different granularity — the channel, and the person.

Resolving this from the receiver's row instead would be a real security change
wearing the clothes of a comment cleanup. Do not "fix" it in that direction.

This column is read when ``ChannelRoutingService._route_installed`` composes
the channel routing ballot, and it is re-read on every message of an
already-bound identity thread by ``ChannelInboundService._ingest`` — so
withdrawing consent takes effect on the sender's very next message, not only
on threads that have not started yet.

CASCADES
--------
- ``server_channel_id`` / ``user_id`` — CASCADE. The row is meaningless without
  either end, and the unique constraint on the pair is what makes the upsert an
  upsert.
- ``pinned_agent_id`` — SET NULL. Deleting the pinned agent *un-pins* the
  channel rather than deleting the user's whole settings row, which would also
  silently discard their ``allow_identity_routing`` opt-in.
- ``channel_user_agent.channel_user_setting_id`` — CASCADE, so
  ``DELETE /users/me/channels/{id}`` returns the user to pure inheritance in
  one statement.
- ``channel_user_agent.agent_id`` — CASCADE. A deleted agent leaves no entry
  behind; an id in the list that names an agent the user no longer *owns* is a
  different problem, and it is handled at resolution time rather than by the FK
  (the FK enforces existence, never ownership).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class ChannelUserSetting(SQLModel, table=True):
    """One user's explicit overrides for one channel. Optional by design."""

    __tablename__ = "channel_user_setting"
    __table_args__ = (
        UniqueConstraint(
            "server_channel_id", "user_id", name="uq_channel_user_setting"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # No separate ``index=True`` on ``server_channel_id``: the unique
    # constraint above already backs it with a btree index led by that column,
    # which is what the per-user lookup uses. ``user_id`` is not the leading
    # column of that index, so it gets its own — the settings UI lists every
    # channel for one user.
    server_channel_id: uuid.UUID = Field(
        foreign_key="server_channel.id", ondelete="CASCADE"
    )
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)

    #: NULL = inherit ``ServerChannel.default_enabled_for_users``. See the
    #: module docstring before making this NOT NULL.
    is_enabled: bool | None = Field(default=None, nullable=True)
    #: NULL = inherit ``ServerChannel.default_agent_scope``. One of
    #: ``CHANNEL_AGENT_SCOPES`` when set. See the module docstring before
    #: making this NOT NULL.
    agent_scope: str | None = Field(default=None, max_length=32, nullable=True)

    #: "Always use this agent of mine on this channel." When set, routing
    #: skips classification entirely for this user on this channel. Ownership
    #: is validated on write AND re-checked at resolution time — the FK only
    #: guarantees the agent exists.
    pinned_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )

    #: NOT NULL, defaults to off, and **never inherits** — master plan §3.4.
    #: The *sender's* consent to their message being routed into another
    #: person's workspace, not the receiver's control over who reaches them.
    #: See the module docstring; do not add a channel-level default for this.
    allow_identity_routing: bool = Field(default=False)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChannelUserAgent(SQLModel, table=True):
    """One agent on a user's per-channel agent list.

    Consulted only when the *resolved* ``agent_scope`` is ``"list"``. Rows may
    exist while the scope is ``"all"`` or ``"none"`` — the user's previous
    selection is kept so that toggling the scope back does not silently discard
    it.
    """

    __tablename__ = "channel_user_agent"
    __table_args__ = (
        UniqueConstraint(
            "channel_user_setting_id", "agent_id", name="uq_channel_user_agent"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Leading column of the unique constraint above, so no separate index.
    channel_user_setting_id: uuid.UUID = Field(
        foreign_key="channel_user_setting.id", ondelete="CASCADE"
    )
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE", index=True)


# ===========================================================================
# User-facing projections
#
# These are NOT ``ServerChannelPublic``. That model is the admin read and
# carries ``webhook_token``, ``config``, ``email_whitelist`` and
# ``has_outbound_credentials`` — a channel's whole trust configuration. A
# separate projection is the entire defence for the user routes: it cannot
# leak a field it does not declare, whereas an exclusion list on the admin
# model silently stops covering any field added later.
# ===========================================================================


class UserChannelPublic(SQLModel):
    """One channel as its user sees it: resolved policy plus provenance.

    Every value here is already resolved — the client never re-applies the
    inherit rules, because a second implementation of them is exactly how the
    UI and the router come to disagree about whether a channel is on.

    The ``*_inherited`` flags exist so the UI can be honest about *why* a value
    is what it is. A setting the user has never touched must render as
    "following the admin default (on)", not as a plain switch that looks
    user-owned; the corresponding ``channel_default_*`` field carries the value
    being followed, so the label can name it without a second request.
    """

    id: uuid.UUID
    channel_type: str
    name: str

    #: The full conjunction: channel enabled AND access AND the user's toggle.
    #: This is what routing asks; the fields below explain how it got here.
    #:
    #: **Always equal to ``is_enabled`` within this payload — by construction,
    #: not by coincidence.** The other two terms of the conjunction
    #: (``channel.enabled`` and access) are exactly what
    #: ``UserChannelService.list_for_user`` filters the channel list on before
    #: this projection is ever built, and what ``get_accessible_channel`` (the
    #: gate in front of ``PUT``/``DELETE``) requires before this projection is
    #: returned at all. A channel that fails either term never reaches a reader
    #: as a ``UserChannelPublic`` row, so within one that does, this field
    #: reduces to the user's own toggle. It carries its own name anyway, rather
    #: than being dropped in favour of ``is_enabled``, because it is what
    #: routing actually asks and a client should read the field with that
    #: meaning rather than infer the equivalence.
    #:
    #: This is deliberate, not an oversight to "fix" by widening the list: the
    #: alternative — listing channels the caller cannot use, so the UI can
    #: explain *why* one is missing — is precisely the enumeration-oracle shape
    #: this phase spent its whole decline path avoiding (see
    #: ``UserChannelService.list_for_user`` and ``ChannelInboundService``'s
    #: module docstring). The two-term filter stays as it is.
    is_available: bool

    #: The resolved user toggle on its own — what the switch in the UI shows.
    is_enabled: bool
    #: True when ``is_enabled`` came from the channel default, not the user.
    is_enabled_inherited: bool
    #: The admin default being followed (or overridden).
    channel_default_enabled: bool

    #: ``"all"`` / ``"list"`` / ``"none"``, resolved and normalised.
    agent_scope: str
    agent_scope_inherited: bool
    channel_default_agent_scope: str

    #: The user's saved agent selection. Returned whatever the scope is, so
    #: switching to "choose agents" shows the previous selection rather than an
    #: empty list. Contains only agents the caller currently owns.
    agent_ids: list[uuid.UUID] = Field(default_factory=list)

    #: NULL unless the user pinned one of their own agents to this channel.
    pinned_agent_id: uuid.UUID | None = None

    #: Never inherited; see ``ChannelUserSetting``.
    allow_identity_routing: bool = False

    #: Whether a settings row exists at all. Drives the "reset to defaults"
    #: affordance — ``DELETE`` is a no-op without one.
    has_settings: bool = False


class UserChannelUpdate(SQLModel):
    """User PUT body. Omitted field = unchanged; explicit ``null`` = inherit.

    The distinction is read with ``model_dump(exclude_unset=True)``, and it is
    the only way a nullable-meaning-inherit column can be *cleared* through an
    API whose "unset" marker is also ``None``. A body of ``{}`` changes
    nothing; ``{"is_enabled": null}`` reverts that one field to the channel
    default while keeping the rest of the row.

    ``allow_identity_routing`` has no inherited state (master plan §3.4), so an
    explicit ``null`` for it is rejected rather than quietly ignored.
    """

    is_enabled: bool | None = None
    agent_scope: str | None = Field(default=None, max_length=32)
    #: Replaces the whole selection. Explicit ``null`` clears it.
    agent_ids: list[uuid.UUID] | None = None
    #: Explicit ``null`` un-pins.
    pinned_agent_id: uuid.UUID | None = None
    allow_identity_routing: bool | None = None
