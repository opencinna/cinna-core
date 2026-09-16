"""Security invariants for the Server Channels inbound pipeline.

These are the three properties the feature review called out as having zero
regression coverage and being the most likely to rot silently, plus one
deliberate, documented deviation from the plan. If anything here goes red,
treat it as more serious than a normal test failure.

  1. Per-asker isolation — another sender routes independently and never
     reaches an existing asker's ACTIVE or FAILED binding.
  2. Concurrent askers — two people opening one thread each get their own
     session or pending install. Same-asker races still park both messages.
  3. Malformed-JWT handling on the public webhook
     (`test_malformed_jwt_probe_family_returns_403_not_500`) — a bearer
     token with an unknown/garbage/oversized `kid`, or none at all, must
     return 403, never 500. Regression-guards the real bug: Authlib raises a
     bare `ValueError` (not `JoseError`) for an unknown `kid`, which used to
     escape every handler as an unhandled exception and skip the
     verification audit trail.
  4. `critical_state` must NOT fail a pending binding
     (`test_critical_state_does_not_fail_a_pending_binding`) — a deliberate
     deviation from plan §8: an environment that is `running` with
     `critical_state=True` still proceeds to `active`; only
     `status == "error"` is terminal.

See `tests/api/server_channels/README.md` for the park-branch design notes
(no true concurrency is available in `drain_tasks()`, so it is driven
deterministically instead).
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.environment import set_environment_status
from tests.utils.message import list_messages
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
    flush_pending_bindings,
    post_webhook,
)
from tests.utils.routing import (
    as_classifier_answer,
    classification,
    enter_classifier_patch,
)
from tests.utils.session import get_session, list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify_answer"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"


# ---------------------------------------------------------------------------
# Local setup helpers
# ---------------------------------------------------------------------------


def _make_pass1_user(client: TestClient, superuser_headers: dict[str, str]):
    """A user who owns exactly one agent, eligible for channel routing.

    Eligible means the agent's own `router_trigger_prompt` — channel Pass 1
    routes over the sender's OWN agents and reads no `AppAgentRoute` at all.

    One eligible agent plus this suite's empty auto-install list is exactly
    Pass 1's conditional `only_one` case, so deliveries here route with no
    classifier at all and `_post` names no answer. That is deliberate rather
    than incidental: the helper's default stub raises, so any of these tests
    would fail loudly if the short-circuit stopped firing.
    """
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Pass1Agent-{random_lower_string()[:6]}")
    drain_tasks()
    # Re-fetch: environment provisioning is a background task, so the agent
    # dict returned by create_agent_via_api (captured before drain_tasks())
    # still has active_environment_id=None.
    agent = client.get(f"{API}/agents/{agent['id']}", headers=headers).json()
    set_router_trigger_prompt(
        client, headers, agent["id"], "Handle anything this user sends"
    )
    return user, headers, agent


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _post(client, channel, signer, event, *, stream_stub=None, classify_result=None):
    """POST a verified webhook event, draining background tasks.

    ``classify_result`` names what `AgentClassifier.classify` answers. Omit it
    only when the delivery must not classify at all — the sender is declined
    before routing, or owns nothing eligible — in which case a classifier that
    runs anyway fails loudly at the call.
    """
    token = signer.token(audience=channel["config"]["project_number"])
    stub = stream_stub or StubAgentEnvConnector(response_text="On it.")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        send_mock = stack.enter_context(
            patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id"))
        )
        enter_classifier_patch(stack, classify_result=classify_result)
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp, send_mock


def _classify_the_only_candidate(candidates, message, **kwargs):
    """A classifier that picks whatever is on the ballot — either pass.

    Pass 1's ballot holds agent ids and Pass 2's holds bundle ids, and the
    lost-race scenarios below drive both in one delivery sequence. A fixed
    ``return_value`` can only be right for one of them: a Pass-1 answer naming
    a bundle id is rejected as "not among the candidates", which silently turns
    the park branch under test into a plain no-match.
    """
    return as_classifier_answer(classification(candidates[0].ref_id))


# ---------------------------------------------------------------------------
# 1. Per-asker session isolation
# ---------------------------------------------------------------------------


def test_second_asker_without_an_agent_is_declined_without_touching_first_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A second asker with no eligible agent gets their own detail-free decline.

    The first asker's session remains unchanged and inaccessible to the second.
    """
    signer = GoogleChatJWTSigner()
    channel = _channel(client, superuser_token_headers)
    user_a, headers_a, agent_a = _make_pass1_user(client, superuser_token_headers)
    user_b, headers_b = create_random_user_with_headers(client)

    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    event_a = build_message_event(
        thread_key=thread_key, text="Hello from A", sender_email=user_a["email"]
    )
    resp_a, _ = _post(client, channel, signer, event_a)
    assert resp_a.status_code == 200

    # A now has exactly one session, one user message.
    sessions_a = [s for s in list_sessions(client, headers_a) if s["agent_id"] == agent_a["id"]]
    assert len(sessions_a) == 1
    session_a = sessions_a[0]
    assert session_a["integration_type"] == "channel_google_chat"
    messages_before = list_messages(client, headers_a, session_a["id"])
    user_messages_before = [m for m in messages_before if m["role"] == "user"]
    assert len(user_messages_before) == 1

    # B posts into the same thread.
    event_b = build_message_event(
        thread_key=thread_key, text="Hijack attempt from B", sender_email=user_b["email"]
    )
    resp_b, send_b = _post(client, channel, signer, event_b)

    assert resp_b.status_code == 200
    replies = [call.args[-1] or "" for call in send_b.await_args_list]
    assert any("couldn't find an assistant" in reply for reply in replies), replies
    assert all(user_a["email"] not in reply for reply in replies)


    # No route was granted, and A's session is not exposed to B.
    assert list_sessions(client, headers_b) == []

    # A's session is untouched — B's message never reached it.
    messages_after = list_messages(client, headers_a, session_a["id"])
    user_messages_after = [m for m in messages_after if m["role"] == "user"]
    assert len(user_messages_after) == len(user_messages_before)
    assert all("Hijack" not in (m["content"] or "") for m in messages_after)


    assert client.get(f"{API}/sessions/{session_a['id']}", headers=headers_b).status_code in (400, 403, 404)

def test_second_asker_routes_independently_of_first_askers_failed_binding(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A failed binding for A cannot prevent B from reaching B's own agent.

    B's routing neither repairs nor resumes A's failed session.
    """
    signer = GoogleChatJWTSigner()
    channel = _channel(client, superuser_token_headers)
    user_a, headers_a, agent_a = _make_pass1_user(client, superuser_token_headers)
    user_b, headers_b, agent_b = _make_pass1_user(client, superuser_token_headers)

    # Strip A's active environment so ingest fails with NoActiveEnvironmentError.
    env_id = agent_a["active_environment_id"]
    assert env_id is not None
    r = client.delete(f"{API}/environments/{env_id}", headers=headers_a)
    assert r.status_code == 200, r.text

    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    event_a = build_message_event(
        thread_key=thread_key, text="Hello from A", sender_email=user_a["email"]
    )
    resp_a, send_mock = _post(
        client, channel, signer, event_a
    )
    assert resp_a.status_code == 200
    # The pipeline notified the (now-failed) thread of the setup failure.
    assert any(
        "failed" in (call.args[-1] or "").lower() or "administrator" in (call.args[-1] or "")
        for call in send_mock.await_args_list
    )
    # A never got a session — ingest failed before session creation succeeded.
    assert list_sessions(client, headers_a) == []

    event_b = build_message_event(
        thread_key=thread_key, text="Hijack attempt from B", sender_email=user_b["email"]
    )
    resp_b, _ = _post(client, channel, signer, event_b)

    assert resp_b.status_code == 200
    sessions_b = [s for s in list_sessions(client, headers_b) if s["agent_id"] == agent_b["id"]]
    assert len(sessions_b) == 1
    contents_b = [m["content"] for m in list_messages(client, headers_b, sessions_b[0]["id"]) if m["role"] == "user"]
    assert contents_b == ["Hijack attempt from B"]
    assert list_sessions(client, headers_a) == []


# ---------------------------------------------------------------------------
# 2. Concurrent askers and same-asker races
# ---------------------------------------------------------------------------


def test_two_askers_opening_one_thread_receive_separate_sessions(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Queue both people before routing; each gets their own agent and session.

    Reusing a conversation key must not cause a unique-key conflict across users.
    """
    signer = GoogleChatJWTSigner()
    channel = _channel(client, superuser_token_headers)
    user_a, headers_a, agent_a = _make_pass1_user(client, superuser_token_headers)
    user_b, headers_b, agent_b = _make_pass1_user(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    token = signer.token(audience=channel["config"]["project_number"])
    event_a = build_message_event(
        thread_key=thread_key, text="A's opening message", sender_email=user_a["email"]
    )
    event_b = build_message_event(
        thread_key=thread_key, text="B's opening message", sender_email=user_b["email"]
    )

    stub = StubAgentEnvConnector(response_text="On it.")
    with signer.patched(), patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock, patch(_CLASSIFY_TARGET, side_effect=_classify_the_only_candidate):
        resp_a = post_webhook(client, channel["webhook_token"], event_a, bearer_token=token)
        resp_b = post_webhook(client, channel["webhook_token"], event_b, bearer_token=token)
        assert resp_a.status_code == 200 and resp_b.status_code == 200
        drain_tasks()

    # Each asker receives exactly one session on their own agent.
    sessions_a = [s for s in list_sessions(client, headers_a) if s["agent_id"] == agent_a["id"]]
    assert len(sessions_a) == 1
    user_msgs_a = [m for m in list_messages(client, headers_a, sessions_a[0]["id"]) if m["role"] == "user"]
    assert len(user_msgs_a) == 1
    assert "A's opening message" in user_msgs_a[0]["content"]

    sessions_b = [s for s in list_sessions(client, headers_b) if s["agent_id"] == agent_b["id"]]
    assert len(sessions_b) == 1
    assert sessions_b[0]["id"] != sessions_a[0]["id"]
    user_msgs_b = [m for m in list_messages(client, headers_b, sessions_b[0]["id"]) if m["role"] == "user"]
    assert [m["content"] for m in user_msgs_b] == ["B's opening message"]
    assert "B's opening message" not in user_msgs_a[0]["content"]
    assert client.get(f"{API}/sessions/{sessions_a[0]['id']}", headers=headers_b).status_code in (400, 403, 404)
    assert client.get(f"{API}/sessions/{sessions_b[0]['id']}", headers=headers_a).status_code in (400, 403, 404)


def test_same_asker_race_parks_both_messages_on_one_pending_install(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The SAME user's second message races their own first message into a
    brand-new thread while Pass 2 (auto-install) is still resolving. The
    loser is the rightful owner, so instead of a decline it is parked onto
    the winner's (their own) `pending_install` binding — never dropped, never
    delivered anywhere else.

    `drain_tasks()` runs background tasks strictly sequentially (see
    `tests/api/server_channels/README.md`), so true concurrent interleaving
    isn't available. Driven deterministically instead, exploiting a real
    consequence of install-time auto-routing (plan §5.3 / §10 — "install-time
    auto-routes ... mean freshly auto-installed agents route with zero extra
    wiring"):

      1. A single public bundle is on the server auto-install list. The
         consumer has no installs and no routes yet.
      2. Both webhook deliveries (msg1, msg2 — same user, same new thread)
         are queued before draining, so both see "no binding" synchronously.
      3. Task 1 (msg1) drains first: Pass 1 misses (owns nothing yet) → Pass 2
         (mocked classifier) picks the bundle → installs it (which restores
         the bundle's trigger prompt onto the consumer's own agent) →
         creates the `pending_install` binding →
         parks msg1.
      4. Task 2 (msg2) drains second: Pass 1 now hits — the install of step 3
         restored the bundle's trigger prompt onto the consumer's OWN agent,
         making it the only candidate on their ballot — so it resolves the
         SAME agent WITHOUT touching Pass 2 at all. It attempts
         `_upsert_binding(status=ACTIVE)` for the same thread → unique
         constraint → IntegrityError → re-reads the existing binding → same
         user, `pending_install` → PARK branch: msg2 is appended, not
         declined, not dropped.
      5. `flush_pending_bindings` (env forced `running`) proves both msg1
         and msg2 were parked onto the SAME binding and are delivered, in
         order, once the binding activates.
    """
    consumer, consumer_headers = make_user_and_headers(client)  # default AI credential included
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    agent = create_agent_via_api(client, publisher_headers, name=f"RaceBundle-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle race-branch test requests"},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()
    bundle_uuid = fresh["bundle_uuid"]

    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)

    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    token = signer.token(audience=channel["config"]["project_number"])
    event_1 = build_message_event(
        thread_key=thread_key,
        text="please help with thing one",
        sender_email=consumer["email"],
        message_name=f"spaces/AAA/messages/{uuid.uuid4()}",
    )
    event_2 = build_message_event(
        thread_key=thread_key,
        text="please help with thing two",
        sender_email=consumer["email"],
        message_name=f"spaces/AAA/messages/{uuid.uuid4()}",
    )

    stub = StubAgentEnvConnector(response_text="Sure, on it.")
    with signer.patched(), patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock, patch(_CLASSIFY_TARGET, side_effect=_classify_the_only_candidate):
        resp_1 = post_webhook(client, channel["webhook_token"], event_1, bearer_token=token)
        resp_2 = post_webhook(client, channel["webhook_token"], event_2, bearer_token=token)
        assert resp_1.status_code == 200 and resp_2.status_code == 200
        drain_tasks()

    installing_texts = [call.args[-1] for call in send_mock.await_args_list]
    # The install announced itself: the thread's nameplate names the bundle
    # being installed, and the setup narration follows it.
    assert any(
        "assigned to this thread" in (t or "") for t in installing_texts
    ), installing_texts
    assert any(
        "First-time setup" in (t or "") for t in installing_texts
    ), installing_texts
    # The second message joined this asker's existing pending install.
    assert any("Still setting up" in (t or "") for t in installing_texts), installing_texts

    consumer_agents = client.get(f"{API}/agents/", headers=consumer_headers).json()["data"]
    installed = [a for a in consumer_agents if a["bundle_uuid"] == bundle_uuid]
    assert len(installed) == 1, (
        f"Expected exactly one install for the consumer (both messages raced onto "
        f"the same bundle), got {len(installed)}"
    )
    agent_row = installed[0]

    # Flip the env to running and flush — both parked messages must land on
    # the SAME binding/session, in order.
    set_environment_status(db, agent_row["active_environment_id"], "running")
    db.commit()
    with patch(_STREAM_TARGET, stub):
        advanced = flush_pending_bindings(db)
        drain_tasks()
    assert advanced >= 1

    sessions = [s for s in list_sessions(client, consumer_headers) if s["agent_id"] == agent_row["id"]]
    assert len(sessions) == 1
    user_msgs = [m for m in list_messages(client, consumer_headers, sessions[0]["id"]) if m["role"] == "user"]
    contents = [m["content"] for m in user_msgs]
    assert any("thing one" in c for c in contents)
    assert any("thing two" in c for c in contents)


def test_two_askers_auto_install_independently_in_one_thread(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Two people summon the same catalog bundle into one shared thread.

    Both pending installs survive and deliver only their own parked message.
    """
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    agent = create_agent_via_api(
        client, publisher_headers, name=f"InstallRace-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(
        client, publisher_headers, agent["id"], "Handle install-race test requests"
    )
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()
    bundle_uuid = fresh["bundle_uuid"]

    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)

    # Neither consumer owns anything, so neither has a Pass 1 ballot at all —
    # which keeps BOTH on Pass 2 and creates one pending install per asker.
    consumer_a, headers_a = make_user_and_headers(client)
    consumer_b, headers_b = make_user_and_headers(client)

    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    token = signer.token(audience=channel["config"]["project_number"])
    event_a = build_message_event(
        thread_key=thread_key,
        text="A opens the thread",
        sender_email=consumer_a["email"],
        message_name=f"spaces/AAA/messages/{uuid.uuid4()}",
    )
    event_b = build_message_event(
        thread_key=thread_key,
        text="B piles into the same thread",
        sender_email=consumer_b["email"],
        message_name=f"spaces/AAA/messages/{uuid.uuid4()}",
    )

    stub = StubAgentEnvConnector(response_text="On it.")
    with signer.patched(), patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock, patch(_CLASSIFY_TARGET, side_effect=_classify_the_only_candidate):
        resp_a = post_webhook(client, channel["webhook_token"], event_a, bearer_token=token)
        resp_b = post_webhook(client, channel["webhook_token"], event_b, bearer_token=token)
        assert resp_a.status_code == 200 and resp_b.status_code == 200
        drain_tasks()

    texts = [call.args[-1] or "" for call in send_mock.await_args_list]

    # One nameplate and one setup narration per asker: the two installs are
    # announced independently, on the same thread.
    assert sum("assigned to this thread" in text for text in texts) >= 2, texts
    assert sum("First-time setup" in text for text in texts) >= 2, texts
    assert not any("setting up your assistant failed" in text for text in texts), texts
    assert list_sessions(client, headers_a) == []
    assert list_sessions(client, headers_b) == []

    installed_a = [
        a
        for a in client.get(f"{API}/agents/", headers=headers_a).json()["data"]
        if a["bundle_uuid"] == bundle_uuid
    ]
    assert len(installed_a) == 1, installed_a
    agent_a = installed_a[0]

    installed_b = [
        a for a in client.get(f"{API}/agents/", headers=headers_b).json()["data"]
        if a["bundle_uuid"] == bundle_uuid
    ]
    assert len(installed_b) == 1
    agent_b = installed_b[0]
    assert agent_b["id"] != agent_a["id"]
    set_environment_status(db, agent_b["active_environment_id"], "running")
    set_environment_status(db, agent_a["active_environment_id"], "running")
    db.commit()
    with patch(_STREAM_TARGET, stub):
        advanced = flush_pending_bindings(db)
        drain_tasks()
    assert advanced >= 1

    sessions_a = [
        s for s in list_sessions(client, headers_a) if s["agent_id"] == agent_a["id"]
    ]
    assert len(sessions_a) == 1
    contents = [
        m["content"]
        for m in list_messages(client, headers_a, sessions_a[0]["id"])
        if m["role"] == "user"
    ]
    assert any("A opens the thread" in c for c in contents), contents
    assert not any("B piles into" in c for c in contents), contents
    sessions_b = [s for s in list_sessions(client, headers_b) if s["agent_id"] == agent_b["id"]]
    assert len(sessions_b) == 1
    contents_b = [m["content"] for m in list_messages(client, headers_b, sessions_b[0]["id"]) if m["role"] == "user"]
    assert any("B piles into the same thread" in c for c in contents_b)
    assert not any("A opens the thread" in c for c in contents_b)



# ---------------------------------------------------------------------------
# 3. Malformed-JWT probe family — must be 403, never 500
# ---------------------------------------------------------------------------


def test_malformed_jwt_unknown_kid_returns_403_and_writes_the_audit_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The specific case the real bug was about, isolated so the audit-trail
    assertion is unambiguous: Authlib raises a bare ``ValueError`` (not
    ``JoseError``) when a JWT's ``kid`` header doesn't match any key in the
    JWKS. Unhandled, that ValueError propagated out of every layer as a 500
    AND skipped the verification-failure SecurityEvent entirely — a request
    asserting only the status code would still pass if someone reintroduced a
    path that 403s without auditing, and silent loss of rejection auditing on
    the platform's one unauthenticated route is the failure worth guarding
    against directly.
    """
    signer = GoogleChatJWTSigner()
    channel = _channel(client, superuser_token_headers)
    audience = channel["config"]["project_number"]
    event = build_message_event(
        thread_key="spaces/AAA/threads/probe-kid", text="probe", sender_email="probe@example.com"
    )

    with signer.patched():
        resp = post_webhook(
            client,
            channel["webhook_token"],
            event,
            bearer_token=signer.token(audience=audience, kid="unknown-kid-xyz"),
        )
    assert resp.status_code == 403
    assert resp.json().get("detail") == "Forbidden"

    events = client.get(
        f"{API}/security-events/",
        headers=superuser_token_headers,
        params={"event_type": "SERVER_CHANNEL_VERIFICATION_FAILED"},
    ).json()["data"]
    matching = [e for e in events if e["details"].get("server_channel_id") == channel["id"]]
    assert len(matching) == 1, (
        f"Expected exactly one SERVER_CHANNEL_VERIFICATION_FAILED audit row for "
        f"this channel, got {len(matching)}: {matching}"
    )
    assert matching[0]["severity"] == "high"


def test_malformed_jwt_probe_family_returns_403_not_500(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Table-driven coverage of the full malformed-JWT probe family an
    unauthenticated caller can actually send: garbage, empty, an oversized
    ``kid`` header, and no Authorization header at all (unknown ``kid`` is
    covered on its own, with the audit-row assertion, in
    ``test_malformed_jwt_unknown_kid_returns_403_and_writes_the_audit_row``
    above — kept separate rather than folded back into this table so that
    the mandatory audit-trail check has one unambiguous case to point at).
    Every case must come back 403, never 500, and the pipeline must never
    reach event parsing.
    """
    signer = GoogleChatJWTSigner()
    channel = _channel(client, superuser_token_headers)
    audience = channel["config"]["project_number"]
    event = build_message_event(
        thread_key="spaces/AAA/threads/probe", text="probe", sender_email="probe@example.com"
    )

    cases: list[tuple[str, dict[str, str] | None, str | None]] = [
        ("garbage token", None, "not-a-jwt-at-all"),
        ("empty bearer value", {"Authorization": "Bearer "}, None),
        (
            "oversized kid header (20KB)",
            None,
            signer.token(audience=audience, extra_claims=None, kid="k" * 20_000),
        ),
        ("no Authorization header at all", {}, None),
        ("expired token", None, signer.token(audience=audience, expired=True)),
        ("wrong audience", None, signer.token(audience="000000000000")),
        ("wrong issuer", None, signer.token(audience=audience, issuer="not-google-chat")),
    ]

    with signer.patched():
        for label, explicit_headers, bearer in cases:
            resp = post_webhook(
                client,
                channel["webhook_token"],
                event,
                bearer_token=bearer,
                headers=explicit_headers,
            )
            assert resp.status_code == 403, f"{label}: expected 403, got {resp.status_code} ({resp.text})"
            # The generic body — never a detailed reason (would be a probing oracle).
            assert resp.json().get("detail") == "Forbidden"

    # A well-formed, correctly-signed token against the same channel is NOT
    # rejected — proves the JWKS mock and the 403s above are about the token,
    # not about the channel/test wiring being broken.
    resp_ok, _ = _post(client, channel, signer, event)
    assert resp_ok.status_code == 200


# ---------------------------------------------------------------------------
# 4. critical_state deviation — must NOT fail a pending binding
# ---------------------------------------------------------------------------


def test_critical_state_does_not_fail_a_pending_binding(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Deliberate deviation from plan §8 ("env error/critical -> failed"):
    `critical_state` coexists with `status == "running"` — a degraded-but-up
    container that still answers. Only `status == "error"` is terminal for a
    `pending_install` binding. See the NOTE in
    `ChannelInboundService._flush_one`.

      1. Consumer with no Pass 1 routes; a single public auto-install bundle.
      2. Message routes via Pass 2 → binding parks the message
         (`pending_install`).
      3. The provisioned environment is forced to `status="running"` AND
         `critical_state=True`.
      4. `flush_pending_bindings` must advance the binding to `active` and
         deliver the parked message — NOT fail it.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    agent = create_agent_via_api(client, publisher_headers, name=f"CritStateBundle-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle critical-state test requests"},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()
    bundle_uuid = fresh["bundle_uuid"]

    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)

    classify_result = classification(bundle_uuid)
    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    event = build_message_event(
        thread_key=thread_key, text="please help", sender_email=consumer["email"]
    )
    stub = StubAgentEnvConnector(response_text="Sure.")
    # Through `_post`, not around it: `enter_classifier_patch` installs an
    # inner patch that shadows an outer one (deliberately, and loudly).
    resp, _ = _post(
        client, channel, signer, event, stream_stub=stub, classify_result=classify_result
    )
    assert resp.status_code == 200

    consumer_agents = client.get(f"{API}/agents/", headers=consumer_headers).json()["data"]
    installed = next(a for a in consumer_agents if a["bundle_uuid"] == bundle_uuid)
    env_id = installed["active_environment_id"]
    assert env_id is not None

    set_environment_status(db, env_id, "running")
    from tests.utils.environment import set_environment_critical_state

    set_environment_critical_state(db, env_id, True)
    db.commit()

    with patch(_STREAM_TARGET, stub), patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id")):
        advanced = flush_pending_bindings(db)
        drain_tasks()

    assert advanced == 1, "critical_state=True must not prevent the binding from advancing"

    sessions = [s for s in list_sessions(client, consumer_headers) if s["agent_id"] == installed["id"]]
    assert len(sessions) == 1
    messages = list_messages(client, consumer_headers, sessions[0]["id"])
    assert any(m["role"] == "user" and "please help" in (m["content"] or "") for m in messages)
    # No failure notice ever went out for this thread.
    fresh_env = client.get(f"{API}/environments/{env_id}", headers=consumer_headers).json()
    assert fresh_env["status"] == "running"
    assert fresh_env["critical_state"] is True


def test_flat_space_resumes_each_askers_session_across_transport_thread_ids(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A flat space is one conversation per person even as event thread ids vary.

    A and B each ask twice, then one webhook is retried. Each session retains
    only its own two questions and the retry creates no additional turn.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user_a, headers_a, agent_a = _make_pass1_user(client, superuser_token_headers)
    user_b, headers_b, agent_b = _make_pass1_user(client, superuser_token_headers)
    conversation = f"spaces/{random_lower_string()}"
    final_event = None
    for user, questions in (
        (user_a, ("A first", "A again")),
        (user_b, ("B first", "B again")),
    ):
        for question in questions:
            event = build_message_event(
                thread_key=f"{conversation}/threads/{random_lower_string()}",
                text=question, sender_email=user["email"],
            )
            event["space"] = {
                "name": conversation, "type": "SPACE",
                "spaceThreadingState": "UNTHREADED_MESSAGES",
            }
            response, _ = _post(client, channel, signer, event)
            assert response.status_code == 200
            final_event = event

    assert final_event is not None
    response, _ = _post(client, channel, signer, final_event)
    assert response.status_code == 200
    for headers, agent, expected in (
        (headers_a, agent_a, ["A first", "A again"]),
        (headers_b, agent_b, ["B first", "B again"]),
    ):
        sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
        assert len(sessions) == 1
        messages = list_messages(client, headers, sessions[0]["id"])
        assert [m["content"] for m in messages if m["role"] == "user"] == expected


def test_missing_history_access_persists_context_notice_without_losing_question(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A quoted summon still reaches the agent with an honest metadata-only notice.

    The transcript precedes the live question, reports limited access, and a
    webhook retry neither duplicates the question nor the context notice.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _make_pass1_user(client, superuser_token_headers)
    thread = f"spaces/AAA/threads/{random_lower_string()}"
    quote_id = f"spaces/AAA/messages/{random_lower_string()}"
    event = build_message_event(thread_key=thread, text="What does the earlier message mean?", sender_email=user["email"])
    event["space"] = {"name": "spaces/AAA", "type": "SPACE", "spaceThreadingState": "THREADED_MESSAGES"}
    event["message"]["threadReply"] = True
    event["message"]["quotedMessageMetadata"] = {"name": quote_id}
    stub = StubAgentEnvConnector(response_text="Please paste the earlier message.")
    with patch(
        "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.resolve_read_capabilities",
        AsyncMock(return_value={"supports_message_fetch": False, "supports_thread_history": False}),
    ):
        response, _ = _post(client, channel, signer, event, stream_stub=stub)
        assert response.status_code == 200
        sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
        assert len(sessions) == 1
        messages = list_messages(client, headers, sessions[0]["id"])
        notices = [m for m in messages if m["message_metadata"].get("channel_thread_context") is True]
        assert len(notices) == 1
        notice = notices[0]
        assert notice["role"] == "system"
        assert notice["message_metadata"]["included_count"] == 0
        assert notice["message_metadata"]["degraded_reason"] == "history_unavailable"
        assert quote_id in notice["content"]
        assert "text unavailable" in notice["content"]
        questions = [m for m in messages if m["role"] == "user"]
        assert len(questions) == 1
        assert questions[0]["content"] == event["message"]["text"]
        assert notice["sequence_number"] < questions[0]["sequence_number"]
        assert len(stub.stream_calls) == 1

        response, _ = _post(client, channel, signer, event, stream_stub=stub)
        assert response.status_code == 200
        assert list_messages(client, headers, sessions[0]["id"]) == messages
        assert len(stub.stream_calls) == 1


def test_quoted_chain_and_history_reach_agent_once_without_polluting_user_bubble(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A real ingestion transaction combines quoted and ambient context once.

    Adapter reads are external stubs. All context building, persistence, ingest
    ledger updates and stream payload composition execute through the webhook.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _make_pass1_user(client, superuser_token_headers)
    thread = f"spaces/AAA/threads/{random_lower_string()}"
    refs = []
    for index, text in enumerate(("Original schedule was Monday.", "Correction: schedule is Tuesday.", "The room is ready.")):
        refs.append(SimpleNamespace(
            message_id=f"spaces/AAA/messages/{random_lower_string()}",
            text=text,
            author_display_name=f"Colleague {index + 1}",
            author_external_id=f"users/colleague-{index + 1}",
            created_at=datetime(2026, 9, 12, 8, index, tzinfo=timezone.utc),
            attachments=(), is_platform_authored=False,
            quoted_message_id=refs[0].message_id if index == 1 else None,
        ))
    by_id = {ref.message_id: ref for ref in refs}
    question = "Which day should I attend?"
    event = build_message_event(thread_key=thread, text=question, sender_email=user["email"])
    event["space"] = {"name": "spaces/AAA", "type": "SPACE", "spaceThreadingState": "THREADED_MESSAGES"}
    event["message"]["threadReply"] = True
    event["message"]["quotedMessageMetadata"] = {"name": refs[1].message_id}
    stub = StubAgentEnvConnector(response_text="Tuesday.")

    async def fetch_message(_channel, message_id):
        return by_id[message_id]

    adapter = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter"
    with patch(
        f"{adapter}.resolve_read_capabilities",
        AsyncMock(return_value={"supports_message_fetch": True, "supports_thread_history": True}),
    ), patch(
        f"{adapter}.fetch_message", AsyncMock(side_effect=fetch_message),
    ) as fetch_quote, patch(
        f"{adapter}.fetch_thread_history", AsyncMock(return_value=list(reversed(refs))),
    ) as fetch_history:
        response, _ = _post(client, channel, signer, event, stream_stub=stub)
        assert response.status_code == 200
        sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
        assert len(sessions) == 1
        messages = list_messages(client, headers, sessions[0]["id"])
        notices = [m for m in messages if m["message_metadata"].get("channel_thread_context") is True]
        assert len(notices) == 1
        context = notices[0]
        assert context["message_metadata"]["included_count"] == 3
        assert context["message_metadata"]["degraded_reason"] is None
        assert context["message_metadata"]["truncated"] is False
        assert len(context["content"]) <= settings.CHANNEL_CONTEXT_CHAR_BUDGET
        assert fetch_quote.await_count == 2
        assert fetch_history.await_count == 1
        assert len(stub.stream_calls) == 1
        sent = stub.stream_calls[0]["payload"]["message"]
        assert sent.endswith(question)
        for ref in refs:
            assert sent.count(ref.text) == 1
            assert context["content"].count(ref.text) == 1
        assert sent.index(refs[0].text) < sent.index(refs[1].text) < sent.index(refs[2].text)
        user_messages = [m for m in messages if m["role"] == "user"]
        assert [m["content"] for m in user_messages] == [question]
        assert context["sequence_number"] < user_messages[0]["sequence_number"]

        # A fresh turn quoting the same message reuses the ingest ledger and
        # successful-backfill marker; no repeat network read or system notice.
        followup = build_message_event(thread_key=thread, text="Thanks, confirm the room.", sender_email=user["email"])
        followup["space"] = event["space"]
        followup["message"]["quotedMessageMetadata"] = {"name": refs[1].message_id}
        response, _ = _post(client, channel, signer, followup, stream_stub=stub)
        assert response.status_code == 200
        assert fetch_quote.await_count == 2
        assert fetch_history.await_count == 1
        messages = list_messages(client, headers, sessions[0]["id"])
        assert sum(m["message_metadata"].get("channel_thread_context") is True for m in messages) == 1
        assert len(stub.stream_calls) == 2
        assert stub.stream_calls[1]["payload"]["message"] == "Thanks, confirm the room."


def test_queued_turns_keep_thread_and_reply_here_destinations_separate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Two accepted turns queued before streaming retain separate reply targets.

    A bound asker posts normally, then requests an in-place reply before the
    background queue drains. Each question must produce one agent call and one
    final external message at its own destination, with no stranded notice.
    """
    channel = _channel(
        client,
        superuser_token_headers,
        config={"project_number": "123456789012", "thread_backfill_enabled": False},
        secrets='{"client_email": "bot@test.iam.gserviceaccount.com", "private_key": "test-only"}',
    )
    signer = GoogleChatJWTSigner()
    user, headers, agent = _make_pass1_user(client, superuser_token_headers)
    thread = f"spaces/AAA/threads/{random_lower_string()}"
    token = signer.token(audience=channel["config"]["project_number"])
    adapter = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter"

    class TurnResponseStub(StubAgentEnvConnector):
        async def stream_chat(self, base_url, auth_headers, payload):
            from tests.stubs.agent_env_stub import build_simple_response_events

            self.stream_calls.append({"base_url": base_url, "payload": payload})
            for event in build_simple_response_events(
                f"Final answer for turn {len(self.stream_calls)}."
            ):
                yield event

    stub = TurnResponseStub()
    # Model the external messages: editing a message cannot move its physical
    # location. Inspect the destination where send created each message, not
    # only the potentially stale target passed to an update later.
    visible: dict[str, tuple[object, str]] = {}
    next_id = 0

    async def send(_channel, target, text):
        nonlocal next_id
        next_id += 1
        message_id = f"spaces/AAA/messages/queued-{next_id}"
        visible[message_id] = (target, text)
        return message_id

    async def update(_channel, _target, message_id, text):
        original_target, _ = visible[message_id]
        visible[message_id] = (original_target, text)

    async def replace(_channel, target, message_id, text):
        await update(_channel, target, message_id, text)
        return SimpleNamespace(message_id=message_id, replaced=True)

    async def delete(_channel, _target, message_id):
        visible.pop(message_id, None)

    def event(text):
        result = build_message_event(
            thread_key=thread, text=text, sender_email=user["email"],
            sender_name="users/queued-asker",
        )
        result["space"] = {
            "name": "spaces/AAA", "type": "SPACE",
            "spaceThreadingState": "THREADED_MESSAGES",
        }
        result["message"]["threadReply"] = True
        return result

    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(f"{adapter}.send_message", AsyncMock(side_effect=send)))
        stack.enter_context(patch(f"{adapter}.update_message", AsyncMock(side_effect=update)))
        stack.enter_context(patch(f"{adapter}.replace_message", AsyncMock(side_effect=replace)))
        stack.enter_context(patch(f"{adapter}.delete_message", AsyncMock(side_effect=delete)))
        stack.enter_context(patch(
            f"{adapter}.resolve_read_capabilities",
            AsyncMock(return_value={"supports_message_fetch": False, "supports_thread_history": False}),
        ))
        enter_classifier_patch(stack)

        response = post_webhook(client, channel["webhook_token"], event("Open my session"), bearer_token=token)
        assert response.status_code == 200 and response.json() == {}
        drain_tasks()
        assert len(stub.stream_calls) == 1

        # No draining between these requests: the first turn is queued but
        # active_streaming_manager has not yet marked the session as running.
        normal = post_webhook(client, channel["webhook_token"], event("Answer inside the thread"), bearer_token=token)
        here = post_webhook(client, channel["webhook_token"], event("Answer in the space\nreply here"), bearer_token=token)
        assert normal.status_code == 200 and normal.json() == {}
        assert here.status_code == 200 and here.json() == {}
        assert len(stub.stream_calls) == 1
        drain_tasks()

    assert [call["payload"]["message"] for call in stub.stream_calls] == [
        "Open my session", "Answer inside the thread", "Answer in the space",
    ]
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    messages = list_messages(client, headers, sessions[0]["id"])
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "Open my session", "Answer inside the thread", "Answer in the space",
    ]
    # Four messages stand on the thread: the nameplate the first turn settled,
    # and one per turn. The nameplate is not a turn, so it is set aside here —
    # its own behaviour is `server_channels_status_notice_test.py`'s subject.
    nameplates = [m for m, (_, text) in visible.items() if "assigned to this thread" in text]
    assert len(nameplates) == 1, visible
    for message_id in nameplates:
        visible.pop(message_id)
    assert len(visible) == 3, visible
    for turn, expected_mode in ((1, "thread_reply"), (2, "thread_reply"), (3, "conversation_post")):
        replies = [(target, text) for target, text in visible.values() if text == f"Final answer for turn {turn}."]
        assert len(replies) == 1, visible
        target, _ = replies[0]
        assert target.mode == expected_mode
        assert target.conversation_key == "spaces/AAA"
        assert target.thread_key == (thread if expected_mode == "thread_reply" else None)
        assert target.reply_to_message_id is None
