"""Identity over channels — the HR story, end to end (plan phase 3 §4).

> HR shares their identity and exposes one agent behind it. A colleague enables
> that identity and switches identity routing on for the channel. From Google
> Chat they write "hey, ask HR what is my time-off status?". Routing finds
> nothing among the sender's own agents, finds HR on the ballot, hands off to
> Stage 2, and opens a session **on HR's agent, in HR's workspace** — with the
> reply going back to the sender's own Google Chat thread.

What this file pins, and why each one is here rather than assumed:

  - **The route and the reply, as one story.** The reply half looks redundant
    next to "it routed" and is not: ``ChannelOutboundService
    ._resolve_channel_session`` gates on ``integration_type.startswith
    ("channel_")``, so an identity-routed session stamped ``identity_mcp``
    would route correctly, answer correctly, and silently never deliver. That
    is a failure that looks like success everywhere except in this assertion.
  - **The one-candidate short-circuit**, which is the HR story's simplest
    shape and a separate code path from the classifier branch: a sender who
    owns no agents and can reach exactly one identity owner routes with **no
    classifier call at all** (``_route_only_candidate`` dispatches Stage 2
    directly). Every test below names no classifier answer, so the suite's
    refusal stub fails loudly if that short-circuit ever stops firing.
  - **Every later message in the thread.** The second message is what
    ``_verify_resume_sender``'s identity exception was relaxed for, and the
    scenario that regresses to "permanently refused" if the exception is ever
    tightened back.
  - **That exception's own condition**, forged rather than driven, because no
    production path can reach it — see ``verify_resume_sender``'s docstring in
    ``tests/utils/server_channel.py``.
  - **Session visibility, per surface**, pinned on all three so the divergence
    (``GET /sessions/`` is owner-scoped, ``GET /external/sessions`` matches
    ``identity_caller_id`` too) is a decision on record rather than a future
    bug report.
  - **Thread ownership is unchanged** — the binding still belongs to the
    sender even though the session does not — on both the synchronous and the
    lost-race path.
  - **An absent grant still bites.** The recovery branch in
    ``ChannelInboundService._ingest`` deliberately re-creates a session with no
    grant, and on a foreign agent that must be refused rather than repaired.

Revocation, consent withdrawal and the audit row live in
``server_channels_identity_revocation_test.py``; what the trace does and does
not record lives in ``server_channels_identity_trace_test.py``.
"""
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import Session as ChatSession, SessionSender
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import share_identity_agent
from tests.utils.message import list_messages
from tests.utils.routing import enter_classifier_patch, post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
    post_webhook,
    verify_resume_sender,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_channel import update_my_channel
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

_REPLY_THREAD_OWNED_SNIPPET = "belongs to someone else"
_REPLY_SETUP_FAILED_SNIPPET = "setting up your assistant failed"


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _agent_owner(client, superuser_headers) -> tuple[dict, dict[str, str]]:
    """A user who can create agents: developer role + a default AI credential."""
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


def _hr_story(client, superuser_headers, *, channel: dict) -> dict:
    """The whole cast: HR with a shared agent, a sender who owns nothing.

    The sender deliberately owns **no** agent, which is the reported case —
    ``ChannelCandidateProvider`` contributes nothing, so the identity candidate
    is the only one on the ballot and Pass 1 short-circuits to Stage 2 without
    a model call.

    ``allow_identity_routing`` is switched on through the real route
    (``PUT /users/me/channels/{id}``), which is also the only thing in the
    codebase that creates a ``channel_user_setting`` row. It never inherits
    from a channel default, so there is no admin-side way to set it.
    """
    owner, owner_headers = _agent_owner(client, superuser_headers)
    hr_agent = _agent(client, owner_headers, "HRTimeOff")
    sender, sender_headers = create_random_user_with_headers(client)

    binding = share_identity_agent(
        client,
        owner_headers,
        sender_headers,
        agent_id=hr_agent["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Answer questions about time off and HR policy.",
        prompt_examples="what is my time-off status",
    )

    settings_row = update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )
    # Asserted rather than assumed: with this False the scenario silently
    # becomes "identity is not a candidate", which passes a routing test for
    # entirely the wrong reason.
    assert settings_row["allow_identity_routing"] is True, settings_row

    return {
        "owner": owner,
        "owner_headers": owner_headers,
        "hr_agent": hr_agent,
        "sender": sender,
        "sender_headers": sender_headers,
        "binding": binding,
    }


def _send(client, channel, signer, cast, text: str, *, thread_key: str, stub=None):
    """One webhook delivery from the sender, drained.

    No classifier answer is named anywhere in this file, deliberately: the
    sender owns nothing and can reach exactly one identity owner with exactly
    one reachable binding, so **neither** stage classifies. The helper's
    refusal stub is installed regardless and raises if either one does.
    """
    event = build_message_event(
        thread_key=thread_key, text=text, sender_email=cast["sender"]["email"]
    )
    return post_channel_message(client, channel, signer, event, stream_stub=stub)


def _hr_sessions(client, cast, agent_id: str) -> list[dict]:
    return [
        s for s in list_sessions(client, cast["owner_headers"])
        if s["agent_id"] == agent_id
    ]


def _external_sessions(client, headers: dict[str, str]) -> list[dict]:
    r = client.get(f"{API}/external/sessions", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1 + 7 + 13. The HR story, reply included
# ---------------------------------------------------------------------------


def test_hr_story_routes_into_the_owners_workspace_and_the_reply_comes_back(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The headline feature, end to end.

      1. HR shares one agent with the sender; the sender enables the contact
         and switches identity routing on for the channel.
      2. The sender writes to the channel. No classifier runs — one identity
         candidate, an empty auto-install list, so Pass 1's conditional
         `only_one` short-circuit dispatches Stage 2 directly.
      3. A session exists **in HR's list**, on HR's agent.
      4. Its `integration_type` is `channel_google_chat` — NOT `identity_mcp`.
      5. Its `identity_caller_id` is the sender (read back through
         `GET /external/sessions`, the one surface that projects the column).
      6. Its metadata carries `identity_caller_name`, which is what labels the
         stranger's message in HR's own session list.
      7. **The reply is delivered back to the sender's thread.** This is the
         `integration_type` trap: the gate in
         `ChannelOutboundService._resolve_channel_session` is a `channel_`
         prefix check, so a session stamped `identity_mcp` would pass every
         assertion above and fail only this one.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    answer = "You have 12 days of time off remaining."
    resp, send_mock = _send(
        client,
        channel,
        signer,
        cast,
        "hey, ask HR what is my time-off status?",
        thread_key=thread_key,
        stub=StubAgentEnvConnector(response_text=answer),
    )
    assert resp.status_code == 200

    # ── Phase 1: the session is HR's, on HR's agent ────────────────────────
    hr_sessions = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(hr_sessions) == 1, hr_sessions
    session = hr_sessions[0]
    assert session["user_id"] == cast["owner"]["id"], session

    # ── Phase 2: integration_type stays channel_* ──────────────────────────
    assert session["integration_type"] == "channel_google_chat", session
    assert session["integration_type"].startswith("channel_"), session

    # ── Phase 3: the sender is stamped as the identity caller ──────────────
    # `GET /sessions/` does not project `identity_caller_id`; the external
    # surface does, and it is the same row (matched by id).
    sender_external = _external_sessions(client, cast["sender_headers"])
    mine = [s for s in sender_external if s["id"] == session["id"]]
    assert len(mine) == 1, sender_external
    assert mine[0]["identity_caller_id"] == cast["sender"]["id"], mine[0]

    # ── Phase 4: attribution for the person who did not start it ───────────
    metadata = session.get("session_metadata") or {}
    assert metadata.get("identity_caller_name") == (
        (cast["sender"].get("full_name") or "").strip() or cast["sender"]["email"]
    ), metadata
    # The other half of that pair is deliberately NOT stamped here: on this
    # path the owner IS the session's user, so it would be telling the reader
    # their own name.
    assert "identity_owner_name" not in metadata, metadata

    # ── Phase 5: the sender's message really landed in that session ────────
    user_msgs = [
        m for m in list_messages(client, cast["owner_headers"], session["id"])
        if m["role"] == "user"
    ]
    assert len(user_msgs) == 1, user_msgs
    assert "time-off status" in user_msgs[0]["content"]

    # ── Phase 6: the reply comes back, to the SENDER's thread ──────────────
    # (channel, thread_key, text) — the binding was resolved from the session,
    # which only happens for a `channel_`-prefixed integration_type.
    delivered = [c.args for c in send_mock.await_args_list]
    assert any(answer in (args[-1] or "") for args in delivered), delivered
    assert any(
        args[-2] == thread_key and answer in (args[-1] or "") for args in delivered
    ), delivered


# ---------------------------------------------------------------------------
# 2. Every later message in the thread
# ---------------------------------------------------------------------------


def test_the_second_and_third_message_resume_the_same_identity_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The scenario `_verify_resume_sender`'s exception exists for.

    An identity-routed session is owned by the identity OWNER, so the ordinary
    resume check (`existing.user_id == sender.platform_user_id`) is false for
    every message after the first. Without the narrow exception the second
    message in an HR thread is refused, and stays refused forever — a thread
    that answers once and then dies.

    Three messages rather than two: the exception has to hold on the general
    case, not just on the first resume, and a third turn is where a
    "re-verified once and cached" implementation would show up.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    _send(client, channel, signer, cast, "first question", thread_key=thread_key)
    first = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(first) == 1, first
    session_id = first[0]["id"]

    for text in ("second question", "third question"):
        resp, send_mock = _send(
            client,
            channel,
            signer,
            cast,
            text,
            thread_key=thread_key,
            stub=StubAgentEnvConnector(response_text=f"answer to {text}"),
        )
        assert resp.status_code == 200
        # The refusal reply would be the tell: a PermissionError from the
        # resume check is caught by `_ingest_or_fail`, which fails the binding
        # and sends the generic setup-failed notice.
        texts = [c.args[-1] or "" for c in send_mock.await_args_list]
        assert not any(_REPLY_SETUP_FAILED_SNIPPET in t for t in texts), texts
        assert any(f"answer to {text}" in t for t in texts), texts

    # ── The same session, not a new one per message ────────────────────────
    after = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(after) == 1, after
    assert after[0]["id"] == session_id

    user_msgs = [
        m for m in list_messages(client, cast["owner_headers"], session_id)
        if m["role"] == "user"
    ]
    assert len(user_msgs) == 3, user_msgs
    assert [m["content"] for m in user_msgs] == [
        "first question",
        "second question",
        "third question",
    ]


# ---------------------------------------------------------------------------
# 3. The resume exception's own condition
# ---------------------------------------------------------------------------


def test_the_resume_exception_admits_the_identity_caller_and_refuses_a_third_party(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The exception is `identity_caller_id == this sender`, and nothing wider.

    Driven against a **real** identity-routed session row produced by the
    webhook above, then handed a forged sender — see `verify_resume_sender`'s
    docstring in `tests/utils/server_channel.py` for why this one condition
    cannot be reached through HTTP (`ChannelInboundService._ingest` refuses a
    mismatched `(binding, user)` pair at its own entry, so the third party is
    stopped a layer earlier by a *different* guard).

    Both directions, because only the pair says anything: the real identity
    caller must be admitted (or the exception is dead and every second message
    is refused) and a third party must not be (or the exception is a hole).
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    _send(client, channel, signer, cast, "open the thread", thread_key=thread_key)
    sessions = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(sessions) == 1, sessions
    row = db.get(ChatSession, uuid.UUID(sessions[0]["id"]))
    assert row is not None
    # The state the exception is about: owned by HR, caller-stamped as X.
    assert row.user_id == uuid.UUID(cast["owner"]["id"])
    assert row.identity_caller_id == uuid.UUID(cast["sender"]["id"])

    # ── The identity caller (X) is admitted ────────────────────────────────
    verify_resume_sender(
        row,
        SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/x",
            platform_user_id=uuid.UUID(cast["sender"]["id"]),
        ),
    )

    # ── A third party (Z) is refused ───────────────────────────────────────
    third_party, _ = create_random_user_with_headers(client)
    with pytest.raises(PermissionError):
        verify_resume_sender(
            row,
            SessionSender.from_channel(
                channel_type="google_chat",
                external_user_id="users/z",
                platform_user_id=uuid.UUID(third_party["id"]),
            ),
        )

    # ── And the owner themselves is admitted by the ORDINARY arm ───────────
    # Not the exception: `existing.user_id == sender.platform_user_id` is
    # simply true for HR. Asserted so a future tightening of the exception
    # cannot be mistaken for having broken this.
    verify_resume_sender(
        row,
        SessionSender.from_channel(
            channel_type="google_chat",
            external_user_id="users/hr",
            platform_user_id=uuid.UUID(cast["owner"]["id"]),
        ),
    )


# ---------------------------------------------------------------------------
# 10. Session visibility, pinned per surface
# ---------------------------------------------------------------------------


def test_session_visibility_is_pinned_on_all_three_surfaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Three answers, two of which disagree — on purpose.

      - `GET /sessions/` as the **sender**: absent. The session lives in HR's
        workspace and that list is owner-scoped; the sender seeing it would be
        the ownership rule not meaning anything.
      - `GET /sessions/` as **HR**: present. It is their session, containing a
        stranger's message, which is exactly what the consent copy warns about.
      - `GET /external/sessions` as the **sender**: present. That surface
        matches `identity_caller_id` as well as `user_id`, which is the
        pre-existing `identity_mcp` behaviour and is deliberately unchanged
        here — a native client restoring its thread list must still find the
        conversation it started.

    Pinned together, in one test, so the divergence is a decision on record.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    _send(client, channel, signer, cast, "ask HR about my leave", thread_key=thread_key)

    hr_sessions = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(hr_sessions) == 1, hr_sessions
    session_id = hr_sessions[0]["id"]

    # ── GET /sessions/ as the sender: absent ───────────────────────────────
    sender_sessions = list_sessions(client, cast["sender_headers"])
    assert [s for s in sender_sessions if s["id"] == session_id] == [], sender_sessions
    # Stronger than "this id is missing": the sender has no sessions at all,
    # so the assertion above cannot pass because of a filter that dropped
    # everything.
    assert sender_sessions == [], sender_sessions

    # ── GET /external/sessions as the sender: present ──────────────────────
    sender_external = _external_sessions(client, cast["sender_headers"])
    assert [s["id"] for s in sender_external] == [session_id], sender_external

    # ── GET /external/sessions as HR: present too (they own it) ────────────
    owner_external = _external_sessions(client, cast["owner_headers"])
    assert session_id in [s["id"] for s in owner_external], owner_external

    # ── A third party sees it on neither surface ───────────────────────────
    _, outsider_headers = create_random_user_with_headers(client)
    assert list_sessions(client, outsider_headers) == []
    assert _external_sessions(client, outsider_headers) == []


# ---------------------------------------------------------------------------
# 9. Thread ownership is still the sender's
# ---------------------------------------------------------------------------


def test_a_second_user_cannot_post_into_an_identity_routed_thread(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The binding belongs to the sender even though the session does not.

    That asymmetry is what this phase introduces, and it is exactly where a
    `binding.user_id == session.user_id` assumption would hide. Both gates are
    exercised:

      - **Synchronously**, in `handle_inbound`: a second whitelisted user
        posting into the bound thread is declined with `REPLY_THREAD_OWNED`.
      - **Via the lost-race path**, in `_handle_lost_race`: two users open the
        SAME brand-new thread before either background task runs; the loser is
        declined rather than delivered into the winner's session — which here
        is a session inside a THIRD person's workspace, the worst version of
        that leak.

    The lost-race half follows the deterministic recipe from
    `server_channels_security_invariants_test.py`: `drain_tasks()` runs
    collected tasks strictly sequentially, so queueing both deliveries before
    the drain reproduces the race without real concurrency.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)

    # ── Phase 1: synchronous decline on an already-bound thread ────────────
    bound_thread = f"spaces/AAA/threads/{random_lower_string()}"
    _send(client, channel, signer, cast, "ask HR about leave", thread_key=bound_thread)
    assert len(_hr_sessions(client, cast, cast["hr_agent"]["id"])) == 1

    intruder, intruder_headers = _agent_owner(client, superuser_token_headers)
    intruder_agent = _agent(client, intruder_headers, "IntruderOwn")
    set_router_trigger_prompt(
        client, intruder_headers, intruder_agent["id"], "Handle anything"
    )

    token = signer.token(audience=channel["config"]["project_number"])
    with signer.patched():
        resp = post_webhook(
            client,
            channel["webhook_token"],
            build_message_event(
                thread_key=bound_thread,
                text="me too please",
                sender_email=intruder["email"],
            ),
            bearer_token=token,
        )
    assert resp.status_code == 200
    assert _REPLY_THREAD_OWNED_SNIPPET in (resp.json().get("text") or ""), resp.json()
    # Nothing was created for the intruder, and HR still has exactly one.
    assert list_sessions(client, intruder_headers) == []
    assert len(_hr_sessions(client, cast, cast["hr_agent"]["id"])) == 1

    # ── Phase 2: the lost-race branch on a brand-new thread ────────────────
    race_thread = f"spaces/AAA/threads/{random_lower_string()}"
    stream_target = "app.services.sessions.message_service.agent_env_connector"
    send_target = (
        "app.services.server_channels.adapters.google_chat."
        "GoogleChatAdapter.send_message"
    )
    stub = StubAgentEnvConnector(response_text="On it.")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(stream_target, stub))
        send_mock = stack.enter_context(
            patch(send_target, AsyncMock(return_value="fake-ext-id"))
        )
        # No answer named: the sender's ballot is one identity candidate and
        # the intruder's is one owned agent — both take `only_one`.
        enter_classifier_patch(stack)
        r1 = post_webhook(
            client,
            channel["webhook_token"],
            build_message_event(
                thread_key=race_thread,
                text="ask HR about my leave",
                sender_email=cast["sender"]["email"],
            ),
            bearer_token=token,
        )
        r2 = post_webhook(
            client,
            channel["webhook_token"],
            build_message_event(
                thread_key=race_thread,
                text="and set up mine",
                sender_email=intruder["email"],
            ),
            bearer_token=token,
        )
        assert r1.status_code == 200 and r2.status_code == 200
        drain_tasks()

    # The sender won: a second session in HR's workspace, on HR's agent.
    assert len(_hr_sessions(client, cast, cast["hr_agent"]["id"])) == 2
    # The loser got nothing — not on their own agent, not on HR's.
    assert list_sessions(client, intruder_headers) == []
    declined = [c.args[-1] or "" for c in send_mock.await_args_list]
    assert any(_REPLY_THREAD_OWNED_SNIPPET in t for t in declined), declined


# ---------------------------------------------------------------------------
# 8. The three-way owner invariant still bites without a grant
# ---------------------------------------------------------------------------


def test_a_recovered_identity_thread_without_a_grant_is_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """No grant, foreign agent ⇒ `PermissionError` before anything is written.

    This is the recovery branch `ChannelInboundService._ingest` documents and
    deliberately does NOT repair: HR deletes the session, the FK nulls
    `binding.session_id`, and the next message arrives on an `active` binding
    naming HR's agent with **no routing decision behind it**. Re-deriving a
    grant from the binding would be inventing an authorization the routing
    layer never issued, so `assert_access` falls through to the three-way owner
    invariant (`agent.owner_id == expected_owner_id == sender.platform_user_id`)
    and refuses.

    The observable consequences, all three asserted: the generic setup-failed
    reply, no session created in HR's workspace, and a binding left in a state
    that self-heals rather than one that wedges — the message after it
    re-routes and opens a genuinely fresh, freshly-verified identity session.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    cast = _hr_story(client, superuser_token_headers, channel=channel)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    _send(client, channel, signer, cast, "ask HR about leave", thread_key=thread_key)
    sessions = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(sessions) == 1, sessions

    # HR deletes the session. The binding stays `active` with a NULL session.
    r = client.delete(
        f"{API}/sessions/{sessions[0]['id']}", headers=cast["owner_headers"]
    )
    assert r.status_code in (200, 204), r.text
    assert _hr_sessions(client, cast, cast["hr_agent"]["id"]) == []

    # ── The next message on that thread is refused ─────────────────────────
    resp, send_mock = _send(
        client, channel, signer, cast, "still there?", thread_key=thread_key
    )
    assert resp.status_code == 200
    texts = [c.args[-1] or "" for c in send_mock.await_args_list]
    assert any(_REPLY_SETUP_FAILED_SNIPPET in t for t in texts), texts
    # Nothing was written: no session anywhere, for anyone.
    assert _hr_sessions(client, cast, cast["hr_agent"]["id"]) == []
    assert list_sessions(client, cast["sender_headers"]) == []

    # ── And the failure self-heals rather than wedging the thread ──────────
    resp, _ = _send(
        client, channel, signer, cast, "trying again", thread_key=thread_key
    )
    assert resp.status_code == 200
    healed = _hr_sessions(client, cast, cast["hr_agent"]["id"])
    assert len(healed) == 1, healed
