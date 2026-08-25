"""
Pure-Python unit tests for the Phase 0 value types:
``SessionSender``, ``IngestionResult``, ``ChannelAccessPolicy``,
and the ``get_session_sender`` reader.

**Authorized exception to the API-only test convention.**
``docs/drafts/channel-ingestion-service_plan.md §8`` (Phase 0 DoD) explicitly
authorises pure-Python tests against synthetic ``Session`` instances for the
value-type module because it has zero DB interaction.  No ``TestClient``, no
``DBSession``, no Docker dependency — this file runs with a plain
``python -m pytest`` invocation.

Run:
    cd backend && python -m pytest tests/unit/models/test_session_sender.py -v
"""
from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from app.api.deps import GuestShareContext
from app.models.a2a.agent_access_token import A2ATokenPayload
from app.models.sessions.session import Session
from app.models.sessions.session_sender import (
    ChannelAccessPolicy,
    IdentityGrant,
    IngestionResult,
    SessionSender,
    SessionSenderKind,
    get_session_sender,
)
from app.models.users.user import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Derived from the Literal itself, so adding a kind to `SessionSenderKind`
# without covering it here fails loudly instead of silently under-testing.
_VALID_KINDS: list[SessionSenderKind] = list(get_args(SessionSenderKind))

# The kinds each property parametrization below must cover. Kept as an
# explicit set so the drift guard names the missing kind.
_EXPECTED_KINDS = {
    "platform_user",
    "a2a_caller",
    "mcp_caller",
    "webui_user",
    "task_executor",
    "system_trigger",
    "anonymous",
    "channel_caller",
}


def test_kind_literal_has_not_drifted() -> None:
    """Every declared sender kind is covered by this file's parametrizations."""
    assert set(_VALID_KINDS) == _EXPECTED_KINDS


def _make_token_payload(agent_id: uuid.UUID | None = None) -> A2ATokenPayload:
    """Build a minimal A2ATokenPayload for testing."""
    return A2ATokenPayload(
        sub=str(uuid.uuid4()),
        agent_id=str(agent_id or uuid.uuid4()),
        mode="conversation",
        scope="limited",
        exp=9999999999,
    )


def _make_user(
    *,
    full_name: str | None = "Test User",
    email: str = "test@example.com",
) -> User:
    """Build a synthetic User without DB round-trip."""
    return User(
        id=uuid.uuid4(),
        email=email,
        full_name=full_name,
        is_active=True,
        is_superuser=False,
    )


def _make_session(**kwargs) -> Session:
    """
    Build a synthetic Session row.

    Only ``user_id`` is required; everything else uses sensible defaults.
    Critically, no DB session or commit is involved.
    """
    defaults: dict = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "mode": "conversation",
        "status": "active",
        "interaction_status": "",
        "pending_messages_count": 0,
        "session_metadata": {},
    }
    defaults.update(kwargs)
    return Session(**defaults)


# ===========================================================================
# SessionSender — construction and kind coverage
# ===========================================================================


class TestSessionSenderConstruction:
    """SessionSender can be constructed for every valid kind."""

    @pytest.mark.parametrize("kind", _VALID_KINDS)
    def test_constructs_with_valid_kind(self, kind: SessionSenderKind) -> None:
        sender = SessionSender(
            kind=kind,
            external_id="test-external",
            display_name=None,
            platform_user_id=None,
        )
        assert sender.kind == kind

    def test_fields_are_accessible(self) -> None:
        uid = uuid.uuid4()
        sender = SessionSender(
            kind="webui_user",
            external_id=str(uid),
            display_name="Alice",
            platform_user_id=uid,
        )
        assert sender.external_id == str(uid)
        assert sender.display_name == "Alice"
        assert sender.platform_user_id == uid


# ===========================================================================
# SessionSender — frozen invariant
# ===========================================================================


class TestSessionSenderFrozen:
    """SessionSender is a frozen dataclass — mutations raise FrozenInstanceError."""

    def test_cannot_assign_kind(self) -> None:
        sender = SessionSender(
            kind="a2a_caller", external_id="x", display_name=None, platform_user_id=None
        )
        with pytest.raises(FrozenInstanceError):
            sender.kind = "mcp_caller"  # type: ignore[misc]

    def test_cannot_assign_external_id(self) -> None:
        sender = SessionSender(
            kind="webui_user", external_id="original", display_name=None, platform_user_id=None
        )
        with pytest.raises(FrozenInstanceError):
            sender.external_id = "mutated"  # type: ignore[misc]

    def test_cannot_assign_display_name(self) -> None:
        sender = SessionSender(
            kind="task_executor", external_id="t", display_name="before", platform_user_id=None
        )
        with pytest.raises(FrozenInstanceError):
            sender.display_name = "after"  # type: ignore[misc]

    def test_cannot_assign_platform_user_id(self) -> None:
        uid = uuid.uuid4()
        sender = SessionSender(
            kind="system_trigger", external_id="s", display_name=None, platform_user_id=uid
        )
        with pytest.raises(FrozenInstanceError):
            sender.platform_user_id = uuid.uuid4()  # type: ignore[misc]


# ===========================================================================
# SessionSender — helper properties
# ===========================================================================


class TestSessionSenderProperties:
    """is_anonymous / is_platform_user / is_system return expected booleans."""

    # ── is_anonymous ─────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "kind, expected",
        [
            ("anonymous", True),
            ("channel_caller", False),
            ("webui_user", False),
            ("a2a_caller", False),
            ("system_trigger", False),
            ("task_executor", False),
            ("mcp_caller", False),
            ("platform_user", False),
        ],
    )
    def test_is_anonymous(self, kind: SessionSenderKind, expected: bool) -> None:
        sender = SessionSender(
            kind=kind,
            external_id="e",
            display_name=None,
            platform_user_id=uuid.uuid4() if kind != "anonymous" else None,
        )
        assert sender.is_anonymous is expected

    # ── is_platform_user ─────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "kind, platform_user_id, expected",
        [
            # Bound to a real user — True for most kinds.
            ("webui_user", uuid.uuid4(), True),
            ("a2a_caller", uuid.uuid4(), True),   # A2A owner is a real user
            ("mcp_caller", uuid.uuid4(), True),
            ("task_executor", uuid.uuid4(), True),
            ("system_trigger", uuid.uuid4(), True),
            ("platform_user", uuid.uuid4(), True),
            ("channel_caller", uuid.uuid4(), True),
            # anonymous — always False regardless of platform_user_id presence.
            ("anonymous", None, False),
            # platform_user_id None — no bound user.
            ("webui_user", None, False),
        ],
    )
    def test_is_platform_user(
        self,
        kind: SessionSenderKind,
        platform_user_id: uuid.UUID | None,
        expected: bool,
    ) -> None:
        sender = SessionSender(
            kind=kind,
            external_id="e",
            display_name=None,
            platform_user_id=platform_user_id,
        )
        assert sender.is_platform_user is expected

    # ── is_system ────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "kind, expected",
        [
            ("system_trigger", True),
            ("webui_user", False),
            ("a2a_caller", False),
            ("task_executor", False),
            ("mcp_caller", False),
            ("anonymous", False),
            ("platform_user", False),
            ("channel_caller", False),
        ],
    )
    def test_is_system(self, kind: SessionSenderKind, expected: bool) -> None:
        sender = SessionSender(
            kind=kind,
            external_id="e",
            display_name=None,
            platform_user_id=None,
        )
        assert sender.is_system is expected


# ===========================================================================
# IngestionResult — construction and accessor
# ===========================================================================


class TestIngestionResult:
    """IngestionResult constructs, exposes is_new_session, and is frozen."""

    def _make_result(self, *, is_new_session: bool = True) -> IngestionResult:
        session = _make_session()
        return IngestionResult(
            session=session,
            message_id=uuid.uuid4(),
            is_new_session=is_new_session,
            streaming_initiated=True,
        )

    def test_constructs_with_required_fields(self) -> None:
        result = self._make_result(is_new_session=True)
        assert result.is_new_session is True
        assert result.streaming_initiated is True
        assert result.action is None     # default
        assert result.message is None    # default

    def test_is_new_session_false(self) -> None:
        result = self._make_result(is_new_session=False)
        assert result.is_new_session is False

    def test_old_created_kwarg_does_not_exist(self) -> None:
        """The field is named `is_new_session`, not `created` — old name must fail."""
        session = _make_session()
        with pytest.raises(TypeError):
            IngestionResult(  # type: ignore[call-arg]
                session=session,
                message_id=None,
                created=True,           # wrong name — must raise
                streaming_initiated=False,
            )

    def test_frozen_raises_on_assignment(self) -> None:
        result = self._make_result()
        with pytest.raises(FrozenInstanceError):
            result.is_new_session = False  # type: ignore[misc]

    def test_optional_action_and_message(self) -> None:
        session = _make_session()
        result = IngestionResult(
            session=session,
            message_id=None,
            is_new_session=False,
            streaming_initiated=False,
            action="error",
            message="something went wrong",
        )
        assert result.action == "error"
        assert result.message == "something went wrong"


# ===========================================================================
# ChannelAccessPolicy — construction and mutability
# ===========================================================================


class TestChannelAccessPolicy:
    """ChannelAccessPolicy defaults and is intentionally mutable (not frozen)."""

    def test_all_defaults(self) -> None:
        policy = ChannelAccessPolicy()
        assert policy.expected_owner_id is None
        assert policy.allow_system_trigger_fastpath is False
        assert policy.require_owner_match is True
        assert policy.require_access_token_scope is None
        assert policy.identity_grant is None

    def test_constructs_with_explicit_values(self) -> None:
        uid = uuid.uuid4()
        token = _make_token_payload()
        grant = IdentityGrant(
            owner_id=uuid.uuid4(),
            binding_id=uuid.uuid4(),
            assignment_id=uuid.uuid4(),
        )
        policy = ChannelAccessPolicy(
            expected_owner_id=uid,
            allow_system_trigger_fastpath=True,
            require_owner_match=False,
            require_access_token_scope=token,
            identity_grant=grant,
        )
        assert policy.expected_owner_id == uid
        assert policy.allow_system_trigger_fastpath is True
        assert policy.require_owner_match is False
        assert policy.require_access_token_scope is token
        assert policy.identity_grant is grant

    def test_not_frozen_allows_mutation(self) -> None:
        """ChannelAccessPolicy is intentionally NOT frozen (per §3.4)."""
        uid = uuid.uuid4()
        policy = ChannelAccessPolicy()
        policy.expected_owner_id = uid     # must NOT raise
        assert policy.expected_owner_id == uid

        policy.require_owner_match = False
        assert policy.require_owner_match is False


# ===========================================================================
# Constructor: SessionSender.from_a2a
# ===========================================================================


class TestFromA2A:
    """SessionSender.from_a2a — kind, external_id derivation, platform_user_id."""

    def test_kind_is_a2a_caller(self) -> None:
        uid = uuid.uuid4()
        tok_id = uuid.uuid4()
        sender = SessionSender.from_a2a(
            access_token_id=tok_id,
            default_user_id=uid,
        )
        assert sender.kind == "a2a_caller"

    def test_external_id_uses_access_token_id_when_set(self) -> None:
        owner_id = uuid.uuid4()
        tok_id = uuid.uuid4()
        sender = SessionSender.from_a2a(
            access_token_id=tok_id,
            default_user_id=owner_id,
        )
        assert sender.external_id == str(tok_id)

    def test_external_id_falls_back_to_default_user_id_when_no_token_id(self) -> None:
        """
        When access_token_id is None, external_id must be str(default_user_id).

        The constructor mirrors `get_session_sender`'s two-arm derivation
        exactly so that the constructor and the reader produce the same
        external_id for the same Session row.
        """
        owner_id = uuid.uuid4()
        sender = SessionSender.from_a2a(
            access_token_id=None,
            default_user_id=owner_id,
        )
        assert sender.external_id == str(owner_id)

    def test_platform_user_id_is_default_user_id(self) -> None:
        owner_id = uuid.uuid4()
        sender = SessionSender.from_a2a(
            access_token_id=uuid.uuid4(),
            default_user_id=owner_id,
        )
        assert sender.platform_user_id == owner_id

    def test_display_name_is_none(self) -> None:
        sender = SessionSender.from_a2a(
            access_token_id=uuid.uuid4(),
            default_user_id=uuid.uuid4(),
        )
        assert sender.display_name is None


# ===========================================================================
# Constructor: SessionSender.from_app_mcp
# ===========================================================================


class TestFromAppMcp:
    """SessionSender.from_app_mcp — plain and identity variants."""

    def test_plain_kind_and_external_id(self) -> None:
        caller = uuid.uuid4()
        sender = SessionSender.from_app_mcp(caller_user_id=caller)
        assert sender.kind == "mcp_caller"
        assert sender.external_id == str(caller)
        assert sender.platform_user_id == caller

    def test_plain_display_name_is_none(self) -> None:
        sender = SessionSender.from_app_mcp(caller_user_id=uuid.uuid4())
        assert sender.display_name is None

    def test_with_identity_caller_uses_identity_id(self) -> None:
        """
        For identity_mcp: identity_caller_user_id takes precedence.

        per §3.3 — platform_user_id and external_id are both derived from
        identity_caller_user_id (the identity that owns the binding),
        not from the raw caller.
        """
        caller = uuid.uuid4()
        identity_caller = uuid.uuid4()
        sender = SessionSender.from_app_mcp(
            caller_user_id=caller,
            identity_caller_user_id=identity_caller,
        )
        assert sender.kind == "mcp_caller"
        assert sender.external_id == str(identity_caller)
        assert sender.platform_user_id == identity_caller

    def test_identity_caller_none_falls_back_to_caller(self) -> None:
        caller = uuid.uuid4()
        sender = SessionSender.from_app_mcp(
            caller_user_id=caller,
            identity_caller_user_id=None,
        )
        assert sender.external_id == str(caller)
        assert sender.platform_user_id == caller


# ===========================================================================
# Constructor: SessionSender.from_webui
# ===========================================================================


class TestFromWebUI:
    """SessionSender.from_webui — kind, display_name fallback, platform_user_id."""

    def test_kind_is_webui_user(self) -> None:
        user = _make_user()
        sender = SessionSender.from_webui(user)
        assert sender.kind == "webui_user"

    def test_external_id_is_user_id(self) -> None:
        user = _make_user()
        sender = SessionSender.from_webui(user)
        assert sender.external_id == str(user.id)

    def test_platform_user_id_is_user_id(self) -> None:
        user = _make_user()
        sender = SessionSender.from_webui(user)
        assert sender.platform_user_id == user.id

    def test_display_name_is_full_name_when_set(self) -> None:
        user = _make_user(full_name="Alice Smith")
        sender = SessionSender.from_webui(user)
        assert sender.display_name == "Alice Smith"

    def test_display_name_falls_back_to_email_when_full_name_is_none(self) -> None:
        user = _make_user(full_name=None, email="alice@example.com")
        sender = SessionSender.from_webui(user)
        assert sender.display_name == "alice@example.com"


# ===========================================================================
# Constructor: SessionSender.from_task_execution
# ===========================================================================


class TestFromTaskExecution:
    """SessionSender.from_task_execution — Fix 1 behavior: real user_id, not owner."""

    def test_kind_is_task_executor(self) -> None:
        sender = SessionSender.from_task_execution(
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_name="Do something",
        )
        assert sender.kind == "task_executor"

    def test_external_id_is_task_prefixed(self) -> None:
        task_id = uuid.uuid4()
        sender = SessionSender.from_task_execution(
            user_id=uuid.uuid4(),
            task_id=task_id,
            task_name=None,
        )
        assert sender.external_id == f"task:{task_id}"

    def test_platform_user_id_is_executing_user_not_owner(self) -> None:
        """
        Fix 1 correction: platform_user_id must be the executing human user_id,
        NOT agent.owner_id or a fast-path system identity.  The route layer
        passes the real authenticated user_id here.
        """
        executing_user = uuid.uuid4()
        agent_owner = uuid.uuid4()   # deliberately different
        sender = SessionSender.from_task_execution(
            user_id=executing_user,
            task_id=uuid.uuid4(),
            task_name=None,
        )
        assert sender.platform_user_id == executing_user
        assert sender.platform_user_id != agent_owner

    def test_display_name_reflects_task_name(self) -> None:
        sender = SessionSender.from_task_execution(
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_name="Deploy to production",
        )
        assert sender.display_name == "Deploy to production"

    def test_display_name_is_none_when_task_name_none(self) -> None:
        sender = SessionSender.from_task_execution(
            user_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_name=None,
        )
        assert sender.display_name is None


# ===========================================================================
# Constructor: SessionSender.from_system_trigger
# ===========================================================================


class TestFromSystemTrigger:
    """SessionSender.from_system_trigger — schedule and handover variants."""

    @pytest.mark.parametrize("trigger_kind", ["schedule", "handover"])
    def test_kind_is_system_trigger(self, trigger_kind: str) -> None:
        sender = SessionSender.from_system_trigger(
            owner_user_id=uuid.uuid4(),
            trigger_kind=trigger_kind,  # type: ignore[arg-type]
            trigger_id=uuid.uuid4(),
        )
        assert sender.kind == "system_trigger"

    @pytest.mark.parametrize("trigger_kind", ["schedule", "handover"])
    def test_external_id_format(self, trigger_kind: str) -> None:
        trigger_id = uuid.uuid4()
        sender = SessionSender.from_system_trigger(
            owner_user_id=uuid.uuid4(),
            trigger_kind=trigger_kind,  # type: ignore[arg-type]
            trigger_id=trigger_id,
        )
        assert sender.external_id == f"{trigger_kind}:{trigger_id}"

    def test_platform_user_id_is_owner_user_id(self) -> None:
        owner = uuid.uuid4()
        sender = SessionSender.from_system_trigger(
            owner_user_id=owner,
            trigger_kind="schedule",
            trigger_id=uuid.uuid4(),
        )
        assert sender.platform_user_id == owner

    def test_display_name_default_is_none(self) -> None:
        sender = SessionSender.from_system_trigger(
            owner_user_id=uuid.uuid4(),
            trigger_kind="schedule",
            trigger_id=uuid.uuid4(),
        )
        assert sender.display_name is None

    def test_display_name_when_provided(self) -> None:
        sender = SessionSender.from_system_trigger(
            owner_user_id=uuid.uuid4(),
            trigger_kind="handover",
            trigger_id=uuid.uuid4(),
            display_name="Handover from Agent X",
        )
        assert sender.display_name == "Handover from Agent X"


# ===========================================================================
# SessionSender.from_channel — server channels (Google Chat, ...)
# ===========================================================================


class TestFromChannel:
    """from_channel stamps the channel-namespaced external_id convention."""

    def test_kind_is_channel_caller(self) -> None:
        sender = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/123456",
            platform_user_id=uuid.uuid4(),
        )
        assert sender.kind == "channel_caller"

    def test_external_id_is_channel_namespaced(self) -> None:
        sender = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/123456",
            platform_user_id=uuid.uuid4(),
        )
        assert sender.external_id == "google_chat:users/123456"

    def test_same_external_user_on_two_channels_does_not_collide(self) -> None:
        """The channel_type prefix is what keeps two transports apart."""
        user_id = uuid.uuid4()
        a = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="u1",
            platform_user_id=user_id,
        )
        b = SessionSender.from_channel(
            channel_type="slack",
            external_user_id="u1",
            platform_user_id=user_id,
        )
        assert a.external_id != b.external_id

    def test_platform_user_id_is_the_sender_not_the_agent_owner(self) -> None:
        """
        Security-critical: channel sessions are owned by the external
        sender's own account, so an external caller can only ever reach
        their own installs.
        """
        sender_user_id = uuid.uuid4()
        sender = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/123456",
            platform_user_id=sender_user_id,
        )
        assert sender.platform_user_id == sender_user_id
        assert sender.is_platform_user is True
        assert sender.is_anonymous is False
        assert sender.is_system is False

    def test_display_name_default_is_none(self) -> None:
        sender = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/123456",
            platform_user_id=uuid.uuid4(),
        )
        assert sender.display_name is None

    def test_display_name_when_provided(self) -> None:
        sender = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/123456",
            platform_user_id=uuid.uuid4(),
            display_name="Jane Doe",
        )
        assert sender.display_name == "Jane Doe"


# ===========================================================================
# get_session_sender — integration_type → kind mapping (§3.2 table)
# ===========================================================================


class TestGetSessionSender:
    """
    get_session_sender derives the correct SessionSender from Session rows.

    All Session instances are synthetic (in-memory, no DB).  Tests verify
    every row in the §3.2 table plus the unknown/fallback branch.
    """

    # ── a2a ──────────────────────────────────────────────────────────────

    def test_a2a_with_access_token_id(self) -> None:
        owner_id = uuid.uuid4()
        tok_id = uuid.uuid4()
        session = _make_session(
            user_id=owner_id,
            integration_type="a2a",
            access_token_id=tok_id,
        )
        sender = get_session_sender(session)
        assert sender.kind == "a2a_caller"
        assert sender.external_id == str(tok_id)
        assert sender.platform_user_id == owner_id

    def test_a2a_without_access_token_id_falls_back_to_user_id(self) -> None:
        owner_id = uuid.uuid4()
        session = _make_session(
            user_id=owner_id,
            integration_type="a2a",
            access_token_id=None,
        )
        sender = get_session_sender(session)
        assert sender.kind == "a2a_caller"
        assert sender.external_id == str(owner_id)

    def test_a2a_subtype_prefix_match(self) -> None:
        """Any integration_type starting with 'a2a' is treated as A2A."""
        for subtype in ("a2a_external", "a2a_v2", "a2a"):
            session = _make_session(
                user_id=uuid.uuid4(),
                integration_type=subtype,
            )
            sender = get_session_sender(session)
            assert sender.kind == "a2a_caller", f"failed for {subtype}"

    # ── app_mcp ──────────────────────────────────────────────────────────

    def test_app_mcp_with_caller_id(self) -> None:
        caller = uuid.uuid4()
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="app_mcp",
            caller_id=caller,
        )
        sender = get_session_sender(session)
        assert sender.kind == "mcp_caller"
        assert sender.external_id == str(caller)
        assert sender.platform_user_id == caller

    def test_app_mcp_without_caller_id_falls_back_to_user_id(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="app_mcp",
            caller_id=None,
        )
        sender = get_session_sender(session)
        assert sender.kind == "mcp_caller"
        assert sender.external_id == str(owner)

    # ── identity_mcp ─────────────────────────────────────────────────────

    def test_identity_mcp_with_identity_caller_id(self) -> None:
        identity_caller = uuid.uuid4()
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="identity_mcp",
            identity_caller_id=identity_caller,
        )
        sender = get_session_sender(session)
        assert sender.kind == "mcp_caller"
        assert sender.external_id == str(identity_caller)
        assert sender.platform_user_id == identity_caller

    def test_identity_mcp_without_identity_caller_id_falls_back_to_user_id(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="identity_mcp",
            identity_caller_id=None,
        )
        sender = get_session_sender(session)
        assert sender.kind == "mcp_caller"
        assert sender.external_id == str(owner)

    # ── None (web-UI created) ─────────────────────────────────────────────

    def test_none_integration_type_is_webui_user(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type=None,
        )
        sender = get_session_sender(session)
        assert sender.kind == "webui_user"
        assert sender.external_id == str(owner)
        assert sender.platform_user_id == owner

    # ── task ─────────────────────────────────────────────────────────────

    def test_task_with_task_id_in_metadata(self) -> None:
        task_id = str(uuid.uuid4())
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="task",
            session_metadata={"task_id": task_id},
        )
        sender = get_session_sender(session)
        assert sender.kind == "task_executor"
        assert sender.external_id == f"task:{task_id}"
        assert sender.platform_user_id == owner

    def test_task_without_metadata_falls_back_to_user_id(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="task",
            session_metadata={},
        )
        sender = get_session_sender(session)
        assert sender.kind == "task_executor"
        assert sender.external_id == str(owner)

    # ── schedule ─────────────────────────────────────────────────────────

    def test_schedule_with_schedule_id_in_metadata(self) -> None:
        schedule_id = str(uuid.uuid4())
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="schedule",
            session_metadata={"schedule_id": schedule_id},
        )
        sender = get_session_sender(session)
        assert sender.kind == "system_trigger"
        assert sender.external_id == f"schedule:{schedule_id}"
        assert sender.platform_user_id == owner

    def test_schedule_without_metadata_falls_back_to_user_id(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="schedule",
            session_metadata={},
        )
        sender = get_session_sender(session)
        assert sender.kind == "system_trigger"
        assert sender.external_id == str(owner)

    # ── channel_* (server channels) ──────────────────────────────────────

    def test_channel_with_sender_external_id_in_metadata(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="channel_google_chat",
            session_metadata={"sender_external_id": "google_chat:users/123"},
        )
        sender = get_session_sender(session)
        assert sender.kind == "channel_caller"
        assert sender.external_id == "google_chat:users/123"
        # The session owner IS the external sender's platform user.
        assert sender.platform_user_id == owner

    def test_channel_without_metadata_falls_back_to_user_id(self) -> None:
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="channel_google_chat",
            session_metadata={},
        )
        sender = get_session_sender(session)
        assert sender.kind == "channel_caller"
        assert sender.external_id == str(owner)

    @pytest.mark.parametrize(
        "integration_type",
        ["channel_google_chat", "channel_slack", "channel_telegram"],
    )
    def test_channel_prefix_match_covers_every_channel_type(
        self, integration_type: str
    ) -> None:
        """Any `channel_<type>` maps to channel_caller — no per-adapter wiring."""
        session = _make_session(
            user_id=uuid.uuid4(),
            integration_type=integration_type,
        )
        assert get_session_sender(session).kind == "channel_caller"

    # ── channel_* + identity routing (channels & identity unification, ph. 3) ──
    #
    # The API-observable end of this pair — an identity-routed Google Chat
    # session appearing in the OWNER's session list while the reply goes back
    # to the SENDER's thread — is covered in
    # `tests/api/server_channels/server_channels_identity_routing_test.py`.

    def test_channel_with_identity_caller_reports_the_caller_not_the_owner(
        self,
    ) -> None:
        """An identity-routed channel session is owned by the identity OWNER.

        `session.user_id` is HR; the human who actually wrote the message is
        `identity_caller_id`. This reader answers "who sent this?", and it has
        no second gate behind it to catch a wrong answer, so naming HR as the
        sender of a message HR never wrote is exactly the mistake it exists to
        prevent.
        """
        owner = uuid.uuid4()
        caller = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="channel_google_chat",
            identity_caller_id=caller,
            session_metadata={"sender_external_id": "google_chat:users/999"},
        )
        sender = get_session_sender(session)
        assert sender.kind == "channel_caller"
        assert sender.platform_user_id == caller
        assert sender.platform_user_id != owner
        # `external_id` already records the real sender at create time, so it
        # needs no identity branch of its own.
        assert sender.external_id == "google_chat:users/999"

    def test_channel_with_identity_caller_and_no_metadata_uses_the_caller_id(
        self,
    ) -> None:
        """The fallback follows the caller too, not the owner.

        With no `sender_external_id` stamped, `external_id` falls back to the
        same person `platform_user_id` names — which on this branch is the
        identity caller. Falling back to `session.user_id` here would surface
        HR's id as the external sender's identifier.
        """
        owner = uuid.uuid4()
        caller = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="channel_google_chat",
            identity_caller_id=caller,
            session_metadata={},
        )
        sender = get_session_sender(session)
        assert sender.kind == "channel_caller"
        assert sender.external_id == str(caller)
        assert sender.platform_user_id == caller

    def test_channel_without_identity_caller_still_reports_the_owner(self) -> None:
        """The other direction, so the branch above cannot be over-applied.

        An ordinary channel session leaves `identity_caller_id` NULL, and there
        the session owner IS the external sender's platform user. Asserted
        explicitly because the `or` fallback makes the two branches one
        expression: a change that reversed the operands would break only this
        case, and only this test would say so.
        """
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type="channel_google_chat",
            identity_caller_id=None,
            session_metadata={},
        )
        sender = get_session_sender(session)
        assert sender.kind == "channel_caller"
        assert sender.external_id == str(owner)
        assert sender.platform_user_id == owner

    def test_channel_round_trip_with_from_channel(self) -> None:
        """The constructor's external_id survives a round-trip through the reader."""
        owner = uuid.uuid4()
        built = SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/123",
            platform_user_id=owner,
        )
        session = _make_session(
            user_id=owner,
            integration_type="channel_google_chat",
            session_metadata={"sender_external_id": built.external_id},
        )
        read = get_session_sender(session)
        assert read.kind == built.kind
        assert read.external_id == built.external_id
        assert read.platform_user_id == built.platform_user_id

    # ── unknown integration_type (forward-compat fallback) ────────────────

    @pytest.mark.parametrize("integration_type", ["email", "webhook", "webapp"])
    def test_unknown_integration_type_falls_back_to_platform_user(
        self, integration_type: str
    ) -> None:
        """
        Channels not migrated in this plan (email, webhook, webapp) fall
        through to the best-effort `platform_user` kind rather than raising.
        """
        owner = uuid.uuid4()
        session = _make_session(
            user_id=owner,
            integration_type=integration_type,
        )
        sender = get_session_sender(session)
        assert sender.kind == "platform_user"
        assert sender.external_id == str(owner)
        assert sender.platform_user_id == owner


# ===========================================================================
# Round-trip test: from_a2a constructor ↔ get_session_sender reader
# ===========================================================================


class TestA2ARoundTrip:
    """
    Critical regression guard: from_a2a and get_session_sender must agree
    on external_id for the same logical sender. Both arms use
    access_token_id when present, owner_id when absent.
    """

    def test_roundtrip_with_access_token_id(self) -> None:
        owner_id = uuid.uuid4()
        tok_id = uuid.uuid4()

        # Build via constructor.
        constructed = SessionSender.from_a2a(
            access_token_id=tok_id,
            default_user_id=owner_id,
        )

        # Reconstruct via the reader from a synthetic Session row stamped
        # with the same fields the channel would write.
        session = _make_session(
            user_id=owner_id,
            integration_type="a2a",
            access_token_id=tok_id,
        )
        read = get_session_sender(session)

        assert constructed.external_id == read.external_id
        assert constructed.kind == read.kind
        assert constructed.platform_user_id == read.platform_user_id

    def test_roundtrip_without_access_token_id(self) -> None:
        """
        When there is no access_token_id, both arms must fall back to
        owner_id.
        """
        owner_id = uuid.uuid4()

        constructed = SessionSender.from_a2a(
            access_token_id=None,
            default_user_id=owner_id,
        )

        session = _make_session(
            user_id=owner_id,
            integration_type="a2a",
            access_token_id=None,
        )
        read = get_session_sender(session)

        assert constructed.external_id == read.external_id
        assert constructed.external_id == str(owner_id)


# ===========================================================================
# Constructor: SessionSender.from_guest_share
# ===========================================================================


def _make_guest_share_context(
    *,
    user_id: uuid.UUID | None,
    guest_share_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
) -> GuestShareContext:
    """Build a minimal GuestShareContext for unit testing from_guest_share."""
    return GuestShareContext(
        guest_share_id=guest_share_id or uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        owner_id=owner_id or uuid.uuid4(),
        is_anonymous=(user_id is None),
        user_id=user_id,
    )


class TestFromGuestShare:
    """
    SessionSender.from_guest_share — anonymous and grant-based variants.

    Per §3.3 of the channel-ingestion-service plan and the docstring on
    from_guest_share: anonymous guests have no User row (context.user_id is
    None) and surface as kind="anonymous"; grant-based guests carry the
    user_id of the authenticated user exercising the grant and surface as
    kind="webui_user".

    For both variants, external_id and platform_user_id reflect the caller's
    identity, NOT the agent owner's identity.
    """

    def test_anonymous_guest(self) -> None:
        """Anonymous guest: kind='anonymous', external_id=guest_share_id, platform_user_id=None."""
        share_id = uuid.uuid4()
        context = _make_guest_share_context(user_id=None, guest_share_id=share_id)

        sender = SessionSender.from_guest_share(context)

        assert sender.kind == "anonymous"
        assert sender.external_id == str(share_id)
        assert sender.platform_user_id is None

    def test_grant_based_guest(self) -> None:
        """Grant-based guest: kind='webui_user', external_id=user_id, platform_user_id=user_id."""
        user_id = uuid.uuid4()
        context = _make_guest_share_context(user_id=user_id)

        sender = SessionSender.from_guest_share(context)

        assert sender.kind == "webui_user"
        assert sender.external_id == str(user_id)
        assert sender.platform_user_id == user_id

    def test_from_guest_share_anonymous_is_not_platform_user(self) -> None:
        """
        Anonymous guest sender has platform_user_id=None and kind='anonymous',
        so both is_platform_user is False and is_anonymous is True.
        """
        context = _make_guest_share_context(user_id=None)
        sender = SessionSender.from_guest_share(context)
        assert sender.is_platform_user is False
        assert sender.is_anonymous is True

    def test_from_guest_share_grant_based_is_platform_user(self) -> None:
        """
        Grant-based guest sender has a real user_id and kind='webui_user', so
        is_platform_user is True and is_anonymous is False.
        """
        context = _make_guest_share_context(user_id=uuid.uuid4())
        sender = SessionSender.from_guest_share(context)
        assert sender.is_platform_user is True
        assert sender.is_anonymous is False

    def test_from_guest_share_display_name_is_none(self) -> None:
        """from_guest_share always sets display_name=None (no user row to read from)."""
        for user_id in (None, uuid.uuid4()):
            context = _make_guest_share_context(user_id=user_id)
            sender = SessionSender.from_guest_share(context)
            assert sender.display_name is None, (
                f"Expected display_name=None for user_id={user_id}, "
                f"got {sender.display_name!r}"
            )

    def test_from_guest_share_anonymous_external_id_does_not_use_owner_id(self) -> None:
        """
        Anonymous guest: external_id must be str(guest_share_id), NOT str(owner_id).
        The owner_id is present on GuestShareContext but must not bleed into the
        sender identity — the sender is the guest, not the agent owner.
        """
        share_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        # Ensure owner_id and share_id are different so the assertion is meaningful.
        assert share_id != owner_id
        context = _make_guest_share_context(
            user_id=None,
            guest_share_id=share_id,
            owner_id=owner_id,
        )
        sender = SessionSender.from_guest_share(context)
        assert sender.external_id == str(share_id)
        assert sender.external_id != str(owner_id)
