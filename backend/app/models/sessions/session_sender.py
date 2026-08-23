"""
SessionSender value type and supporting types.

This module is the *value layer* underneath the Channel Ingestion Service
(see `docs/drafts/channel-ingestion-service_plan.md`). It exists to give
every inbound entry point (A2A, App MCP, web-UI session create, internal
triggers) one shared vocabulary for "who is the sender" and "what access
policy applies".

Phase 0 deliverable: pure addition. No business logic changes, no DB
schema changes, no service modifications. The five constructors and the
`get_session_sender` reader are the only places where channel-specific
knowledge lives.

Design constraints (see plan §10 anti-goals):
- No ABCs, no Protocol, no registry.
- `SessionSender.kind` is a `Literal[...]`, NOT an Enum.
- Constructors never touch the DB.
- `get_session_sender` is the single place that derives a SessionSender
  from an existing Session row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from app.models.a2a.agent_access_token import A2ATokenPayload

if TYPE_CHECKING:
    from app.api.deps import GuestShareContext
    from app.models.sessions.session import Session
    from app.models.users.user import User


# Allowed sender kinds. See plan §3.1 for semantics.
#
# - "platform_user": catch-all for an authenticated platform user when the
#   inbound channel is unspecified (used by `get_session_sender` as the
#   best-effort fallback for unknown `integration_type` values).
# - "a2a_caller": external caller via an A2A access token. The
#   `platform_user_id` is the agent owner's user_id (sessions run in the
#   owner's space) and `external_id` identifies the access token.
# - "mcp_caller": authenticated platform user calling via App MCP.
# - "webui_user": explicit subkind of platform_user when the inbound is
#   the web-UI itself.
# - "task_executor": human-initiated task execution. Carries the real
#   `user_id` from the route layer — this is the corrected behavior per
#   Fix 1 in the plan revision (was previously conflated with
#   `system_trigger`, which would have widened trust silently).
# - "system_trigger": genuinely sender-less paths — cron schedules and
#   handover-fired sessions. `platform_user_id` is always `agent.owner_id`
#   by construction.
# - "anonymous": guest-share anonymous caller (no User row). `platform_user_id`
#   is always None; the route supplies `session_owner_id=agent.owner_id` so the
#   session is still created in the owner's space.
# - "channel_caller": external person reaching the platform through an
#   admin-configured server channel (Google Chat, …). The transport verified
#   their identity, and a platform `User` row was resolved (or auto-created)
#   from the verified email — so `platform_user_id` is that user and the
#   session is owned by them, exactly like `task_executor`. An external caller
#   can therefore only ever reach their own installs.
SessionSenderKind = Literal[
    "platform_user",
    "a2a_caller",
    "mcp_caller",
    "webui_user",
    "task_executor",
    "system_trigger",
    "anonymous",
    "channel_caller",
]


@dataclass(frozen=True)
class SessionSender:
    """
    Names the sender of an inbound message in a channel-agnostic way.

    Immutable value type. Two responsibilities only:
    1. Name the sender (`kind`, `external_id`, `display_name`).
    2. Carry the `platform_user_id` that downstream services need.

    Build via the per-channel constructors (`from_a2a`, `from_app_mcp`,
    `from_webui`, `from_task_execution`, `from_system_trigger`) rather
    than direct instantiation when possible — the constructors encode
    each channel's external_id convention in one place.
    """

    kind: SessionSenderKind
    external_id: str
    display_name: str | None
    platform_user_id: UUID | None

    @property
    def is_anonymous(self) -> bool:
        """True when the sender kind is the reserved `anonymous` slot."""
        return self.kind == "anonymous"

    @property
    def is_platform_user(self) -> bool:
        """
        True when the sender is bound to a real authenticated platform user.

        `a2a_caller` returns True as well — A2A sessions run in the agent
        owner's space and the owner's user_id is carried on the sender.
        """
        return self.platform_user_id is not None and self.kind != "anonymous"

    @property
    def is_system(self) -> bool:
        """True when the sender is a cron-fired / handover-spawned trigger."""
        return self.kind == "system_trigger"

    # ------------------------------------------------------------------
    # Per-channel constructors (plan §3.3).
    #
    # These never touch the DB. They take already-extracted fields from
    # the channel's auth context and stamp the `external_id` convention.
    # ------------------------------------------------------------------

    @classmethod
    def from_a2a(
        cls,
        access_token_id: UUID | None,
        default_user_id: UUID,
    ) -> SessionSender:
        """
        Build a SessionSender for an A2A inbound call.

        `default_user_id` is the agent owner's user_id — A2A sessions run
        in the owner's space, with the caller stamped via `access_token_id`.
        The `external_id` mirrors the `get_session_sender` reader's two-arm
        derivation (§3.2) so the constructor and the reader round-trip on
        the same Session row. When `access_token_id` is None the session has
        no scoped lineage and the fallback is the owner's user id.
        """
        external_id = (
            str(access_token_id) if access_token_id is not None else str(default_user_id)
        )

        return cls(
            kind="a2a_caller",
            external_id=external_id,
            display_name=None,
            platform_user_id=default_user_id,
        )

    @classmethod
    def from_app_mcp(
        cls,
        caller_user_id: UUID,
        identity_caller_user_id: UUID | None = None,
    ) -> SessionSender:
        """
        Build a SessionSender for an App MCP inbound call.

        For plain `app_mcp`, pass only `caller_user_id`. For `identity_mcp`,
        pass `identity_caller_user_id` — the kind stays `"mcp_caller"`;
        the distinction is carried by the integration_type / session
        stamping done by the service layer at session-create time.
        """
        platform_user_id = identity_caller_user_id or caller_user_id
        return cls(
            kind="mcp_caller",
            external_id=str(platform_user_id),
            display_name=None,
            platform_user_id=platform_user_id,
        )

    @classmethod
    def from_webui(cls, current_user: User) -> SessionSender:
        """
        Build a SessionSender for a web-UI inbound (e.g. POST /sessions).
        """
        return cls(
            kind="webui_user",
            external_id=str(current_user.id),
            display_name=current_user.full_name or current_user.email,
            platform_user_id=current_user.id,
        )

    @classmethod
    def from_guest_share(cls, context: GuestShareContext) -> SessionSender:
        """
        Build a SessionSender for a guest-share caller (anonymous or grant-based).

        - Anonymous guests have no `User` row (`context.user_id is None`) and
          surface as `kind="anonymous"` — `is_platform_user` is False, matching
          `platform_user_id=None`.
        - Grant-based guests carry the user_id of the authenticated user
          exercising the grant and surface as `kind="webui_user"`.

        Either way the session is created in the agent owner's space — the route
        passes `session_owner_id=agent.owner_id` as an `extra_session_kwargs`
        override; this constructor only captures *who is sending*.

        NOTE: external_id for guest-share senders does not round-trip via
        get_session_sender(session): the reader derives external_id from
        session.user_id (which is agent.owner_id for guest-share sessions),
        while this constructor uses the caller's user_id or guest_share_id.
        Harmless today (the sender object is never persisted), but flag for
        future callers that may rely on round-trip parity.
        """
        if context.user_id is None:
            return cls(
                kind="anonymous",
                external_id=str(context.guest_share_id),
                display_name=None,
                platform_user_id=None,
            )
        return cls(
            kind="webui_user",
            external_id=str(context.user_id),
            display_name=None,
            platform_user_id=context.user_id,
        )

    @classmethod
    def from_task_execution(
        cls,
        *,
        user_id: UUID,
        task_id: UUID,
        task_name: str | None,
    ) -> SessionSender:
        """
        Build a SessionSender for a human-initiated task execution.

        Per Fix 1 in the plan revision: `platform_user_id` is the real
        human `user_id` from the route layer (NOT `agent.owner_id`).
        This preserves the existing trust check at
        `session_service.py:1250` (`chat_session.user_id != user_id`)
        through the migration.
        """
        return cls(
            kind="task_executor",
            external_id=f"task:{task_id}",
            display_name=task_name,
            platform_user_id=user_id,
        )

    @classmethod
    def from_channel(
        cls,
        *,
        channel_type: str,
        external_user_id: str,
        platform_user_id: UUID,
        display_name: str | None = None,
    ) -> SessionSender:
        """
        Build a SessionSender for an inbound server-channel message.

        `platform_user_id` is the *sender's own* platform user (resolved
        from the transport-verified email, auto-registered when the channel
        allows it) — never the agent publisher. The session is created in
        that user's space, so a channel caller can only reach their own
        installs.

        `external_id` is namespaced by channel type
        (`"google_chat:users/1234"`) so the same person on two transports
        never collides.
        """
        return cls(
            kind="channel_caller",
            external_id=f"{channel_type}:{external_user_id}",
            display_name=display_name,
            platform_user_id=platform_user_id,
        )

    @classmethod
    def from_system_trigger(
        cls,
        *,
        owner_user_id: UUID,
        trigger_kind: Literal["schedule", "handover"],
        trigger_id: UUID,
        display_name: str | None = None,
    ) -> SessionSender:
        """
        Build a SessionSender for a cron-fired / handover-spawned session.

        `platform_user_id` is always `agent.owner_id` by construction —
        the constructor enforces this invariant. `assert_access` then
        asserts the structural property `policy.expected_owner_id ==
        agent.owner_id == sender.platform_user_id` at the service edge.
        """
        return cls(
            kind="system_trigger",
            external_id=f"{trigger_kind}:{trigger_id}",
            display_name=display_name,
            platform_user_id=owner_user_id,
        )


@dataclass
class ChannelAccessPolicy:
    """
    Per-call access policy expressing which gates apply at the service edge.

    The caller (each channel's entry point) picks the policy; the service
    (`ChannelIngestionService.assert_access`) interprets it and dispatches
    to existing access primitives. No new access logic lives here — see
    plan §4.3.
    """

    # The agent.owner_id the sender must match for the owner-match check.
    # Required for `task_executor` and `webui_user`. For `system_trigger`,
    # this must equal `sender.platform_user_id` (asserted as a structural
    # invariant, not skipped).
    expected_owner_id: UUID | None = None

    # When True, `assert_access` accepts the `system_trigger` kind after
    # asserting the owner invariant. Only the cron-fired / handover paths
    # set this.
    allow_system_trigger_fastpath: bool = False

    # When True, enforce the owner-match check for the `webui_user` kind.
    # Set to False for guest-share session creation, where the caller is
    # explicitly not the owner.
    require_owner_match: bool = True

    # When set, the A2A token payload to validate scope against (via
    # existing `AccessTokenService.can_access_session`).
    require_access_token_scope: A2ATokenPayload | None = None

    # When True, the App MCP routing layer must have verified the caller
    # has a route to the resolved agent. The service does not re-check
    # routing — it delegates to existing `AppMCPRoutingService` logic.
    require_caller_in_route: bool = False


@dataclass(frozen=True)
class IngestionResult:
    """
    Result of `ChannelIngestionService.ingest_inbound_message`.

    Mirrors the shape of `SessionService.send_session_message`'s return
    dict so the per-channel migration is a near-textual swap (see
    plan §3.4 and §5).
    """

    session: "Session"
    message_id: UUID | None
    # True when the session was newly created by this call; False when an
    # existing session was resumed via `thread_key`.
    is_new_session: bool
    # True when stream initiation was kicked off as part of this call.
    # Mirrors the existing `send_session_message` action mapping:
    # "streaming" / "pending" -> True; "queued" / "command_executed" /
    # "error" -> False.
    streaming_initiated: bool
    # The mapped action string returned by `send_session_message`. Kept
    # explicit so callers can preserve channel-specific reporting without
    # the service growing channel awareness. The Literal mirrors every
    # value `SessionService.send_session_message` (and the helpers it
    # delegates to, including `initiate_stream`) can return — verified by
    # grepping `"action":` across `backend/app/services/sessions/`.
    action: Literal[
        "streaming",
        "pending",
        "queued",
        "command_executed",
        "error",
        "setup_required",
        "no_pending_messages",
        "message_created",
    ] | None = None
    # Pass-through of the `"message"` key returned by
    # `SessionService.send_session_message`. Populated unconditionally —
    # carries the error description when `action == "error"` and the command
    # response text when `action == "command_executed"` (the synchronous
    # slash-command path writes the agent's reply both as a SessionMessage
    # row and as this field). `None` when the underlying primitive does not
    # return a message field for the action.
    message: str | None = None


def get_session_sender(session: "Session") -> SessionSender:
    """
    Derive a `SessionSender` from an existing `Session` row.

    Pure read; never writes. The single place where the
    `integration_type` -> `kind` mapping (plan §3.2) lives. Used for
    surfacing the sender on API responses, structured logging, and
    debugging — never for access control (the channels build their own
    `SessionSender` via the constructors above).

    Forward-compatible: unknown `integration_type` values fall back to
    a best-effort `"platform_user"` mapping rather than raising. Channels
    not migrated in this plan (email, webhook, webapp) hit this fallback.
    """
    integration_type = session.integration_type

    # Branch order mirrors the table in plan §3.2.

    # A2A (covers all A2A subtypes, including external variants).
    if integration_type is not None and integration_type.startswith("a2a"):
        external_id = str(session.access_token_id or session.user_id)
        return SessionSender(
            kind="a2a_caller",
            external_id=external_id,
            display_name=None,
            platform_user_id=session.user_id,
        )

    # App MCP — plain.
    if integration_type == "app_mcp":
        caller = session.caller_id
        return SessionSender(
            kind="mcp_caller",
            external_id=str(caller) if caller is not None else str(session.user_id),
            display_name=None,
            platform_user_id=caller,
        )

    # App MCP — identity routing.
    if integration_type == "identity_mcp":
        caller = session.identity_caller_id
        return SessionSender(
            kind="mcp_caller",
            external_id=str(caller) if caller is not None else str(session.user_id),
            display_name=None,
            platform_user_id=caller,
        )

    # Human-initiated task execution (new integration_type, used by
    # input_task_service after Phase 5 migration).
    if integration_type == "task":
        metadata = session.session_metadata or {}
        task_id = metadata.get("task_id")
        external_id = f"task:{task_id}" if task_id else str(session.user_id)
        return SessionSender(
            kind="task_executor",
            external_id=external_id,
            display_name=None,
            platform_user_id=session.user_id,
        )

    # Cron-fired / handover-spawned (new integration_type, used by
    # agent_schedule_scheduler after Phase 5 migration).
    if integration_type == "schedule":
        metadata = session.session_metadata or {}
        schedule_id = metadata.get("schedule_id")
        external_id = (
            f"schedule:{schedule_id}" if schedule_id else str(session.user_id)
        )
        return SessionSender(
            kind="system_trigger",
            external_id=external_id,
            display_name=None,
            platform_user_id=session.user_id,
        )

    # Server channels — `integration_type` is `channel_<channel_type>`
    # (e.g. "channel_google_chat"). The session is owned by the external
    # sender's own platform user, so `platform_user_id` is `session.user_id`.
    # `external_id` is best-effort from the metadata stamped at create time
    # by the channel inbound pipeline.
    if integration_type is not None and integration_type.startswith("channel_"):
        metadata = session.session_metadata or {}
        sender_external_id = metadata.get("sender_external_id")
        return SessionSender(
            kind="channel_caller",
            external_id=(
                str(sender_external_id)
                if sender_external_id
                else str(session.user_id)
            ),
            display_name=None,
            platform_user_id=session.user_id,
        )

    # Default (None / unknown integration_type — web-UI created today, or
    # any channel not migrated in this plan). Best-effort: surface as a
    # platform user with the session owner as the bound identity. Web-UI
    # sessions specifically would return `kind="webui_user"` once the
    # channel adopts the typed integration_type convention.
    if integration_type is None:
        return SessionSender(
            kind="webui_user",
            external_id=str(session.user_id),
            display_name=None,
            platform_user_id=session.user_id,
        )

    # Unknown integration_type (e.g., "email", "webhook", "webapp").
    return SessionSender(
        kind="platform_user",
        external_id=str(session.user_id),
        display_name=None,
        platform_user_id=session.user_id,
    )


__all__ = [
    "SessionSender",
    "SessionSenderKind",
    "ChannelAccessPolicy",
    "IngestionResult",
    "get_session_sender",
]
