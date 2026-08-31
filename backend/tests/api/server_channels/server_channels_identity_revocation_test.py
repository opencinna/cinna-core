"""Identity over channels — withdrawing permission (plan phase 3 §2.3, §4).

Identity routing is consent from **two** people, and either of them can take it
back. This file is about what happens when they do, and about the one fact the
system keeps afterwards.

  - **The owner revokes between the decision and the ingest.** The routing
    decision and the session creation are separated by a worker-thread hop; the
    grant that crosses that gap is a *claim*, and `assert_access` re-reads all
    six conditions behind it rather than trusting it. That re-read is only
    worth having if something can change in the window, so the window is
    reproduced deterministically and the refusal asserted.
  - **The owner revokes mid-thread.** Re-verification runs on *every* message,
    not once per thread, so a binding switched off after the conversation
    started stops the next turn — and the sender is told nothing about why.
  - **The sender withdraws their own consent mid-thread.** New in this phase:
    `policy.allow_identity_routing` is re-read per message from that message's
    single reading of the sender's settings, so switching the master toggle off
    stops the conversation it authorised rather than only the next new one.
    Switching it back on lets the sender through again. The per-person contact
    toggle is unchanged and is asserted alongside it.
  - **Every refusal has the same reply.** A revoked grant, a withdrawn consent
    and a disabled contact all produce `REPLY_SETUP_FAILED`, verbatim.
    Asserting that they are *identical* is the point: a reply that named the
    cause would be an oracle telling an unauthenticated external sender which
    gate closed.
  - **The audit row.** Turning the master toggle on is the one per-user channel
    setting that changes whose workspace a message can end up in, so the
    transition is recorded as a `SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED`
    security event attributed to the person who made it.

**On the scheduler-drain variant of consent withdrawal.** There isn't one, by
construction, and this file deliberately does not pretend otherwise. Parked
messages exist only on a binding that reached `pending_install`, and
`ChannelInboundService._install_and_park` (Pass 2's auto-install) is the only
place in the codebase that creates a binding in that status. Pass 2 never runs
on the identity branch — `ChannelRoutingService.decide` short-circuits on
`identity is not None`, so a sender whose Stage 1 chose a person is never
offered a catalog bundle — so an identity binding never holds a parked message
and `flush_pending_bindings` never delivers one. A test driving `_flush_one`
for this property would be asserting against a state the pipeline cannot
produce, which is worse than no test: it would go green on an implementation
that had lost the property entirely.
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.identity.identity_models import IdentityAgentBinding
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import (
    share_identity_agent,
    toggle_identity_contact,
    update_identity_binding,
)
from tests.utils.mfa import find_security_events
from tests.utils.message import list_messages
from tests.utils.routing import classification, post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_channel import delete_my_channel, update_my_channel
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

#: Verbatim, not a snippet. The whole property under test in three of the four
#: scenarios below is that these refusals are *indistinguishable*, and a
#: substring check would pass against a reply that appended the reason.
_REPLY_SETUP_FAILED = (
    "Sorry — setting up your assistant failed. Please contact your "
    "administrator."
)
_IDENTITY_AUDIT_EVENT = "SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED"


# ---------------------------------------------------------------------------
# Setup helpers (same cast as server_channels_identity_routing_test.py)
# ---------------------------------------------------------------------------


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _agent_owner(client, superuser_headers) -> tuple[dict, dict[str, str]]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _agent(client, headers, label: str) -> dict:
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    return agent


def _hr_story(client, superuser_headers, *, channel: dict, agents: int = 1) -> dict:
    """HR with ``agents`` shared agents, and a sender who owns none.

    ``agents=2`` is what forces Stage 2 to *classify* — one reachable binding
    takes Stage 2's own ``only_one`` shortcut and never reaches a model, which
    leaves the decision-to-ingest test with no seam to revoke in. Every other
    scenario here wants the single-binding shape, where nothing classifies at
    all and the refusal stub proves it.
    """
    owner, owner_headers = _agent_owner(client, superuser_headers)
    sender, sender_headers = create_random_user_with_headers(client)

    shared: list[dict] = []
    for index in range(agents):
        agent = _agent(client, owner_headers, f"HRAgent{index}")
        binding = share_identity_agent(
            client,
            owner_headers,
            sender_headers,
            agent_id=agent["id"],
            target_user_id=sender["id"],
            owner_id=owner["id"],
            trigger_prompt=f"Handle topic {index} questions",
            # The contact toggle is per-PERSON, not per-binding: one call
            # enables every assignment this owner has issued to this sender,
            # so repeating it across the loop is idempotent by design.
            enable=True,
        )
        shared.append({"agent": agent, "binding": binding})

    row = update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )
    assert row["allow_identity_routing"] is True, row

    return {
        "owner": owner,
        "owner_headers": owner_headers,
        "sender": sender,
        "sender_headers": sender_headers,
        "shared": shared,
    }


def _send(client, channel, signer, cast, text: str, *, thread_key: str, **kwargs):
    event = build_message_event(
        thread_key=thread_key, text=text, sender_email=cast["sender"]["email"]
    )
    return post_channel_message(client, channel, signer, event, **kwargs)


def _owner_sessions(client, cast) -> list[dict]:
    agent_ids = {entry["agent"]["id"] for entry in cast["shared"]}
    return [
        s for s in list_sessions(client, cast["owner_headers"])
        if s["agent_id"] in agent_ids
    ]


# States of the thread's *status notice* — the one message the pipeline
# rewrites while it works and deletes when the answer lands. Against the real
# adapter those are `patch` calls on a single message; against a mocked
# `send_message` (which cannot return a usable message id) each state falls
# back to a fresh post and shows up here.
#
# Filtered out below because every assertion in this file is about what the
# sender was TOLD — an answer, or a refusal — and a spinner is neither. The
# notice's own behaviour is covered in `server_channels_status_notice_test.py`.
_STATUS_NOTICE_TEXTS = {
    "🔎 Finding the right assistant for you…",
    "💬 Working on your message…",
}


def _texts(send_mock) -> list[str]:
    return [
        text
        for text in (c.args[-1] or "" for c in send_mock.await_args_list)
        if text not in _STATUS_NOTICE_TEXTS
    ]


# ---------------------------------------------------------------------------
# 4a. Revocation between the decision and the ingest
# ---------------------------------------------------------------------------


def test_revocation_between_the_decision_and_the_ingest_is_refused(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The reason `assert_access` re-reads instead of trusting the grant.

    The window is real: `ChannelRoutingService.decide` runs its passes on a
    worker thread and its verdict travels back as plain data, so the identity
    owner can revoke between Stage 2 producing a grant and `_ingest` using it.
    Reproduced deterministically by revoking from **inside** the Stage-2
    classifier call — `IdentityRoutingService._select` has already loaded the
    caller's accessible bindings by then, so Stage 2 still hands back a grant
    naming a binding that is no longer active. That is exactly the stale claim
    the re-read exists to catch.

    HR shares two agents so Stage 2 genuinely classifies; Pass 1 still sees a
    single identity candidate and short-circuits without a model, so the stub
    below is the only classifier call in the whole scenario.

      1. Stage 2 classifies → the stub deactivates the winning binding, then
         names it as the answer.
      2. Stage 2 hands back a grant for that (now inactive) binding.
      3. `assert_access` re-reads condition 1 and raises `PermissionError`.
      4. The sender gets the generic setup-failed reply, and **no session
         exists anywhere** — the refusal lands before any write.

    The revoke is written straight to the row rather than through
    `PUT /identity/bindings/{id}`: it executes inside the drained background
    task's own event loop (`drain_tasks` runs each coroutine under
    `asyncio.run`), where a nested `TestClient` request cannot run. Same
    documented shortcut shape as `_scope_list_containing` in
    `server_channels_routing_test.py` — a setup input, on a row whose own API
    is covered elsewhere.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel, agents=2)

    winner = cast["shared"][0]
    answer = classification(winner["agent"]["id"])
    revoked: list[bool] = []

    def _revoke_then_answer(*_args, **_kwargs):
        row = db.get(IdentityAgentBinding, uuid.UUID(winner["binding"]["id"]))
        assert row is not None
        row.is_active = False
        db.add(row)
        db.commit()
        revoked.append(True)
        return answer

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    resp, send_mock = _send(
        client,
        channel,
        signer,
        cast,
        "ask HR about topic 0",
        thread_key=thread_key,
        classify_side_effect=_revoke_then_answer,
    )

    assert resp.status_code == 200
    # The window was actually entered — without this the test would pass on an
    # implementation where Stage 2 never ran at all.
    assert revoked == [True]

    assert _texts(send_mock) == [_REPLY_SETUP_FAILED], _texts(send_mock)
    assert _owner_sessions(client, cast) == []
    assert list_sessions(client, cast["sender_headers"]) == []


# ---------------------------------------------------------------------------
# 4b. Revocation mid-thread
# ---------------------------------------------------------------------------


def test_the_owner_revoking_mid_thread_refuses_the_next_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Re-verification is per message, so the live conversation stops too.

    The grant is rebuilt from the session row on every turn and re-checked in
    full, which is what makes revocation take effect at the next message rather
    than at the next thread. Both revocation shapes the owner controls are
    exercised, in sequence, because each is a different one of the six
    conditions and each must produce the same silence:

      1. The thread is live and answering.
      2. The owner deactivates the binding → the next message is refused with
         the generic reply, and nothing is appended to the session.
      3. The owner re-activates it → the thread works again. (The refusal
         failed the binding, so this message re-routes and opens a *new*
         session rather than resuming the old one — the ordinary self-heal, and
         worth pinning because it is the observable difference between "refused
         a turn" and "lost the thread".)
      4. The **sender's** own per-person contact toggle, switched off, refuses
         identically — unchanged behaviour, asserted here so the new
         channel-level switch cannot be mistaken for having replaced it.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)
    binding_id = cast["shared"][0]["binding"]["id"]
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    # ── Phase 1: a live identity thread ────────────────────────────────────
    _send(
        client, channel, signer, cast, "first question", thread_key=thread_key,
        stream_stub=StubAgentEnvConnector(response_text="first answer"),
    )
    live = _owner_sessions(client, cast)
    assert len(live) == 1, live
    session_id = live[0]["id"]

    # ── Phase 2: the owner deactivates the binding ─────────────────────────
    update_identity_binding(client, cast["owner_headers"], binding_id, is_active=False)

    _, send_mock = _send(
        client, channel, signer, cast, "second question", thread_key=thread_key
    )
    assert _texts(send_mock) == [_REPLY_SETUP_FAILED], _texts(send_mock)
    # Refused before the write: the session is untouched.
    user_msgs = [
        m for m in list_messages(client, cast["owner_headers"], session_id)
        if m["role"] == "user"
    ]
    assert [m["content"] for m in user_msgs] == ["first question"], user_msgs

    # ── Phase 3: re-activating lets the sender back in ─────────────────────
    update_identity_binding(client, cast["owner_headers"], binding_id, is_active=True)
    _, send_mock = _send(
        client, channel, signer, cast, "third question", thread_key=thread_key,
        stream_stub=StubAgentEnvConnector(response_text="third answer"),
    )
    assert any("third answer" in t for t in _texts(send_mock)), _texts(send_mock)
    after = _owner_sessions(client, cast)
    # The failed binding self-healed by re-routing, so this is a NEW session on
    # the same agent rather than a resume of the refused one.
    assert len(after) == 2, after
    assert session_id in [s["id"] for s in after]

    # ── Phase 4: the sender's own per-person toggle, unchanged ─────────────
    toggle_identity_contact(
        client, cast["sender_headers"], cast["owner"]["id"], False
    )
    _, send_mock = _send(
        client, channel, signer, cast, "fourth question", thread_key=thread_key
    )
    assert _texts(send_mock) == [_REPLY_SETUP_FAILED], _texts(send_mock)


# ---------------------------------------------------------------------------
# 5. The sender withdraws their own consent mid-thread
# ---------------------------------------------------------------------------


def test_consent_withdrawn_mid_thread_declines_and_restoring_it_routes_again(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A consent switch that cannot be withdrawn on the conversation it
    authorised would be no consent at all.

    `_ingest` re-reads `policy.allow_identity_routing` on every message, from
    that message's single reading of the sender's settings, and short-circuits
    on `grant is None` so an ordinary channel thread never consults it. Four
    phases:

      1. The thread is live.
      2. The sender switches the master toggle off → the next message is
         refused with the same generic reply every other failure gets. There is
         no oracle: nothing in the reply distinguishes "I withdrew consent"
         from "HR revoked" from "the environment died".
      3. The sender switches it back on → they route again.
      4. `DELETE /users/me/channels/{id}` — dropping the settings row entirely,
         which returns the column to its `false` default — declines just as
         surely. That is the path a "reset to defaults" button takes, and it
         must not silently leave identity routing on.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    # ── Phase 1: live ──────────────────────────────────────────────────────
    _send(
        client, channel, signer, cast, "first question", thread_key=thread_key,
        stream_stub=StubAgentEnvConnector(response_text="first answer"),
    )
    assert len(_owner_sessions(client, cast)) == 1

    # ── Phase 2: consent withdrawn ─────────────────────────────────────────
    row = update_my_channel(
        client, cast["sender_headers"], channel["id"], allow_identity_routing=False
    )
    assert row["allow_identity_routing"] is False, row

    _, send_mock = _send(
        client, channel, signer, cast, "second question", thread_key=thread_key
    )
    assert _texts(send_mock) == [_REPLY_SETUP_FAILED], _texts(send_mock)

    # ── Phase 3: consent restored ──────────────────────────────────────────
    update_my_channel(
        client, cast["sender_headers"], channel["id"], allow_identity_routing=True
    )
    _, send_mock = _send(
        client, channel, signer, cast, "third question", thread_key=thread_key,
        stream_stub=StubAgentEnvConnector(response_text="third answer"),
    )
    assert any("third answer" in t for t in _texts(send_mock)), _texts(send_mock)
    assert len(_owner_sessions(client, cast)) == 2

    # ── Phase 4: resetting the whole row is a withdrawal too ───────────────
    reset = delete_my_channel(client, cast["sender_headers"], channel["id"])
    assert reset["allow_identity_routing"] is False, reset
    _, send_mock = _send(
        client, channel, signer, cast, "fourth question", thread_key=thread_key
    )
    assert _texts(send_mock) == [_REPLY_SETUP_FAILED], _texts(send_mock)


def test_an_ordinary_channel_thread_is_unaffected_by_the_identity_switch(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The consent check is gated on `grant is None`, and that gate matters.

    A sender routing to their **own** agent has no grant, so the master toggle
    is never consulted for them. Without the short-circuit, every channel user
    on the platform would be declined by default — the column's default is
    `false` and it never inherits.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    from tests.utils.agent import set_router_trigger_prompt

    user, headers = _agent_owner(client, superuser_token_headers)
    own_agent = _agent(client, headers, "MyOwn")
    set_router_trigger_prompt(client, headers, own_agent["id"], "Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key, text="hello", sender_email=user["email"]
    )
    # No settings row at all: `allow_identity_routing` resolves to its `false`
    # default, which must be irrelevant here.
    _, send_mock = post_channel_message(
        client,
        channel,
        signer,
        event,
        stream_stub=StubAgentEnvConnector(response_text="my own answer"),
    )
    assert any("my own answer" in t for t in _texts(send_mock)), _texts(send_mock)
    sessions = [
        s for s in list_sessions(client, headers) if s["agent_id"] == own_agent["id"]
    ]
    assert len(sessions) == 1, sessions


# ---------------------------------------------------------------------------
# 12. The audit row
# ---------------------------------------------------------------------------


def test_turning_identity_routing_on_writes_exactly_one_audit_event(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Transitions only, attributed to the person, at `medium`.

      1. No row, no events.
      2. Off → on writes exactly one `SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED`
         at severity `medium`, carrying the channel and both values.
      3. Saving the same value again writes nothing — a settings form that
         submits every field must not manufacture an audit trail.
      4. On → off writes a second one, so the record answers "when did this
         stop being true" as well as "when did it start".
      5. An explicit `null` is a 422, not a silent no-op: the field has no
         inherited state to revert to, and a silently dropped security-relevant
         write is worse than an error.
      6. The events are the sender's own — another user's feed does not carry
         them.
    """
    channel = _channel(client, superuser_token_headers)
    user, headers = create_random_user_with_headers(client)

    # ── Phase 1: nothing yet ───────────────────────────────────────────────
    assert find_security_events(client, headers, _IDENTITY_AUDIT_EVENT) == []

    # ── Phase 2: off → on ──────────────────────────────────────────────────
    update_my_channel(client, headers, channel["id"], allow_identity_routing=True)
    events = find_security_events(client, headers, _IDENTITY_AUDIT_EVENT)
    assert len(events) == 1, events
    event = events[0]
    assert event["severity"] == "medium", event
    details = event.get("details") or {}
    assert details.get("channel_id") == channel["id"], details
    assert details.get("channel_type") == "google_chat", details
    assert details.get("allow_identity_routing") is True, details
    assert details.get("previous") is False, details

    # ── Phase 3: a no-op save writes nothing ───────────────────────────────
    update_my_channel(client, headers, channel["id"], allow_identity_routing=True)
    assert len(find_security_events(client, headers, _IDENTITY_AUDIT_EVENT)) == 1

    # An unrelated field on the same row is not a transition either.
    update_my_channel(client, headers, channel["id"], is_enabled=True)
    assert len(find_security_events(client, headers, _IDENTITY_AUDIT_EVENT)) == 1

    # ── Phase 4: on → off is recorded too ──────────────────────────────────
    update_my_channel(client, headers, channel["id"], allow_identity_routing=False)
    events = find_security_events(client, headers, _IDENTITY_AUDIT_EVENT)
    assert len(events) == 2, events
    off = [
        e for e in events
        if (e.get("details") or {}).get("allow_identity_routing") is False
    ]
    assert len(off) == 1, events
    assert (off[0].get("details") or {}).get("previous") is True, off[0]

    # ── Phase 5: an explicit null is a 422 ─────────────────────────────────
    r = client.put(
        f"{API}/users/me/channels/{channel['id']}",
        headers=headers,
        json={"allow_identity_routing": None},
    )
    assert r.status_code == 422, r.text
    assert len(find_security_events(client, headers, _IDENTITY_AUDIT_EVENT)) == 2

    # ── Phase 6: attributed to this user and no other ──────────────────────
    _, other_headers = create_random_user_with_headers(client)
    assert find_security_events(client, other_headers, _IDENTITY_AUDIT_EVENT) == []
