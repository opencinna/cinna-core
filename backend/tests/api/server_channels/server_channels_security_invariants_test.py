"""Security invariants for the Server Channels inbound pipeline.

These are the three properties the feature review called out as having zero
regression coverage and being the most likely to rot silently, plus one
deliberate, documented deviation from the plan. If anything here goes red,
treat it as more serious than a normal test failure.

  1. Cross-user thread gate (`test_cross_user_thread_gate_*`) — a sender who
     is not the binding's owner must never reach the bound agent/session,
     for an ACTIVE binding or a FAILED one (the failed check must not be
     reachable by a non-owner either — a non-owner can't trigger the
     self-heal delete).
  2. Lost-race ownership refusal (`test_lost_race_*`) — two different callers
     racing to create the binding for a brand-new thread: the loser is
     declined, never delivered into (`_handle_lost_race`'s "ingest" branch)
     and never parked onto (`_handle_lost_race`'s "park" branch) the
     winner's binding.
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
import types
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_user_route
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
from tests.utils.session import get_session, list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"


# ---------------------------------------------------------------------------
# Local setup helpers
# ---------------------------------------------------------------------------


def _make_pass1_user(client: TestClient, superuser_headers: dict[str, str]):
    """A user who owns exactly one agent with one personal app-mcp route.

    `AppMCPRoutingService.route_message` takes the deterministic `only_one`
    path for this user — no LLM classification call needed.
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
    create_user_route(
        client, headers, agent["id"], trigger_prompt="Handle anything this user sends"
    )
    return user, headers, agent


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _post(client, channel, signer, event, *, stream_stub=None):
    """POST a verified webhook event, draining background tasks."""
    token = signer.token(audience=channel["config"]["project_number"])
    stub = stream_stub or StubAgentEnvConnector(response_text="On it.")
    with signer.patched(), patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock:
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp, send_mock


# ---------------------------------------------------------------------------
# 1. Cross-user thread gate
# ---------------------------------------------------------------------------


def test_cross_user_thread_gate_declines_non_owner_on_active_binding(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A thread already bound to user A's session must decline a message from a
    different (also-whitelisted) user B outright — synchronously, in the
    webhook's own HTTP response — never dispatching to A's agent/session.

      1. User A sends the first message into a brand-new thread → Pass 1
         match on A's own agent → binding created + session created for A.
      2. User B (different account, also whitelisted) posts into the SAME
         thread_key → must get REPLY_THREAD_OWNED synchronously, not
         REPLY_WORKING (which would mean B fell through to routing).
      3. B never gets a session. A's session/message count is unaffected —
         B's text never reached A's agent.
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
    resp_b, _ = _post(client, channel, signer, event_b)

    assert resp_b.status_code == 200
    assert resp_b.json().get("text") == (
        "This conversation belongs to someone else. Please start a new thread and "
        "I'll set you up with your own assistant."
    )

    # B never got a session of their own for A's agent (or anywhere else).
    assert list_sessions(client, headers_b) == []

    # A's session is untouched — B's message never reached it.
    messages_after = list_messages(client, headers_a, session_a["id"])
    user_messages_after = [m for m in messages_after if m["role"] == "user"]
    assert len(user_messages_after) == len(user_messages_before)
    assert all("Hijack" not in (m["content"] or "") for m in messages_after)


def test_cross_user_thread_gate_declines_non_owner_even_on_failed_binding(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The cross-user ownership check runs BEFORE the status branch — including
    the `failed` self-heal branch. A non-owner posting into a thread whose
    binding is `failed` must still be declined, not trigger (or benefit from)
    the delete-and-reroute self-heal.

      1. User A's agent has no active environment when A's first message
         arrives → Pass 1 still matches the route, the binding is created,
         but ingest raises NoActiveEnvironmentError → binding → `failed`.
      2. User B posts into the same thread → must still get
         REPLY_THREAD_OWNED, not a fresh routing attempt (which would
         manifest as REPLY_WORKING/REPLY_NO_MATCH instead, and would mean a
         non-owner reached the self-heal delete).
    """
    signer = GoogleChatJWTSigner()
    channel = _channel(client, superuser_token_headers)
    user_a, headers_a, agent_a = _make_pass1_user(client, superuser_token_headers)
    user_b, headers_b = create_random_user_with_headers(client)

    # Strip A's active environment so ingest fails with NoActiveEnvironmentError.
    env_id = agent_a["active_environment_id"]
    assert env_id is not None
    r = client.delete(f"{API}/environments/{env_id}", headers=headers_a)
    assert r.status_code == 200, r.text

    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    event_a = build_message_event(
        thread_key=thread_key, text="Hello from A", sender_email=user_a["email"]
    )
    resp_a, send_mock = _post(client, channel, signer, event_a)
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
    assert resp_b.json().get("text") == (
        "This conversation belongs to someone else. Please start a new thread and "
        "I'll set you up with your own assistant."
    )
    assert list_sessions(client, headers_b) == []


# ---------------------------------------------------------------------------
# 2. Lost-race ownership refusal
# ---------------------------------------------------------------------------


def test_lost_race_ingest_branch_declines_the_loser(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Two DIFFERENT users race to open the same brand-new thread. Both webhook
    deliveries land before either background routing task runs, so both see
    "no binding" synchronously and both schedule `_route_new_thread`. When
    drained, the loser's `_upsert_binding` hits the unique-constraint
    IntegrityError and re-reads the winner's (ACTIVE) binding —
    `_handle_lost_race` must decline the loser rather than ingesting their
    text into the winner's session.

      1. Users A and B each own their own single-route agent (deterministic
         Pass 1 match, different agents).
      2. Both POST into the SAME new thread_key before draining.
      3. Draining runs A's task first (webhook call order == drain order):
         A's binding is created ACTIVE with A's own agent + session.
      4. B's task loses the race: IntegrityError → reread A's binding → user
         mismatch → declined (REPLY_THREAD_OWNED, delivered as a message
         since this runs in a background task) — B's own agent's routing
         result is discarded, B gets no session anywhere.
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
    ) as send_mock:
        resp_a = post_webhook(client, channel["webhook_token"], event_a, bearer_token=token)
        resp_b = post_webhook(client, channel["webhook_token"], event_b, bearer_token=token)
        assert resp_a.status_code == 200 and resp_b.status_code == 200
        drain_tasks()

    # A won: exactly one session, on A's own agent.
    sessions_a = [s for s in list_sessions(client, headers_a) if s["agent_id"] == agent_a["id"]]
    assert len(sessions_a) == 1
    user_msgs_a = [m for m in list_messages(client, headers_a, sessions_a[0]["id"]) if m["role"] == "user"]
    assert len(user_msgs_a) == 1
    assert "A's opening message" in user_msgs_a[0]["content"]

    # B lost: no session anywhere — not on B's own agent, not on A's.
    assert list_sessions(client, headers_b) == []

    # B was told the thread belongs to someone else (delivered async).
    declined_texts = [call.args[-1] for call in send_mock.await_args_list]
    assert any("belongs to someone else" in (t or "") for t in declined_texts)


def test_lost_race_park_branch_declines_the_loser_but_parks_same_owner(
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
      3. Task 1 (msg1) drains first: Pass 1 misses (no routes yet) → Pass 2
         (mocked classifier) picks the bundle → installs it (which also
         auto-creates the consumer's personal app-mcp route from the
         bundle's trigger prompt) → creates the `pending_install` binding →
         parks msg1.
      4. Task 2 (msg2) drains second: Pass 1 now hits — the auto-route from
         step 3 makes this the deterministic `only_one` match — so it
         resolves the SAME agent WITHOUT touching Pass 2 at all. It attempts
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

    classify_result = types.SimpleNamespace(agent_id=bundle_uuid, transformed_message=None)

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
    ) as send_mock, patch(_CLASSIFY_TARGET, return_value=classify_result):
        resp_1 = post_webhook(client, channel["webhook_token"], event_1, bearer_token=token)
        resp_2 = post_webhook(client, channel["webhook_token"], event_2, bearer_token=token)
        assert resp_1.status_code == 200 and resp_2.status_code == 200
        drain_tasks()

    installing_texts = [call.args[-1] for call in send_mock.await_args_list]
    assert any("Setting up" in (t or "") for t in installing_texts), installing_texts
    # msg2 was parked (not declined): the loser's reply is the "still setting
    # up" text, never the cross-thread decline.
    assert any("Still setting up" in (t or "") for t in installing_texts), installing_texts
    assert not any("belongs to someone else" in (t or "") for t in installing_texts)

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

    classify_result = types.SimpleNamespace(agent_id=bundle_uuid, transformed_message=None)
    thread_key = f"spaces/AAA/threads/{uuid.uuid4()}"
    event = build_message_event(
        thread_key=thread_key, text="please help", sender_email=consumer["email"]
    )
    stub = StubAgentEnvConnector(response_text="Sure.")
    with patch(_CLASSIFY_TARGET, return_value=classify_result):
        resp, _ = _post(client, channel, signer, event, stream_stub=stub)
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
