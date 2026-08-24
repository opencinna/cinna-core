"""Routing + binding lifecycle — plan §13 checklist.

Covers:
  - Pass 1 (installed agents) match, routing to the sender's OWN agent.
  - Pass 1 ownership filter (`_route_installed`): identity route rejected,
    foreign-owned agent rejected, deleted agent rejected, router exception
    never propagates. This is the filter that keeps an external Google Chat
    sender out of a stranger's workspace — pinned directly rather than only
    incidentally covered by the full-flow Pass 1 test above.
  - Pass 2 candidate filtering: visibility (a private/ungranted bundle on the
    auto-install list is never a candidate), already-installed exclusion,
    missing-trigger-prompt exclusion.
  - No-match reply when neither pass finds anything.
  - Binding self-heal: a `failed` binding is deleted and re-routed by the
    next message from its own owner.
  - Session-deleted recovery: `session_id` SET NULL -> next message opens a
    fresh session on the same bound agent.
"""
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import User
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle, publish_bundle_and_make_public
from tests.utils.environment import activate_environment, create_environment
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    build_routing_result,
    create_server_channel,
    post_webhook,
    route_installed,
)
from tests.utils.routing import enter_classifier_patch
from tests.utils.session import list_sessions
from tests.utils.message import list_messages
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_ROUTE_MESSAGE_TARGET = "app.services.app_mcp.app_mcp_routing_service.AppMCPRoutingService.route_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _post(
    client,
    channel,
    signer,
    event,
    *,
    stub=None,
    classify_result=None,
    classify_no_match=False,
    classify_side_effect=None,
    classify_via_provider=False,
):
    """One verified webhook delivery, background work drained.

    The classifier stub comes from `enter_classifier_patch`, the same seam
    `tests.utils.routing`'s two helpers use, rather than a fourth inline copy
    of the same decision. That copy is how this helper ended up with the
    pre-fix default — name no answer and `AgentClassifier.classify` was left
    live, i.e. calling a real model. Naming an answer is now mandatory; a
    scenario that must NOT classify says so by naming nothing, and finds out
    loudly if it does.
    """
    token = signer.token(audience=channel["config"]["project_number"])
    stream_stub = stub or StubAgentEnvConnector(response_text="ok")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stream_stub))
        send_mock = stack.enter_context(patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id")))
        enter_classifier_patch(
            stack,
            classify_result=classify_result,
            classify_no_match=classify_no_match,
            classify_side_effect=classify_side_effect,
            classify_via_provider=classify_via_provider,
        )
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp, send_mock


def _publish_public_bundle(client, publisher_headers, *, trigger_prompt: str | None, name_prefix: str) -> dict:
    agent = create_agent_via_api(client, publisher_headers, name=f"{name_prefix}-{random_lower_string()[:6]}")
    drain_tasks()
    if trigger_prompt is not None:
        r = client.patch(
            f"{API}/agents/{agent['id']}/router-trigger-prompt",
            headers=publisher_headers,
            json={"router_trigger_prompt": trigger_prompt},
        )
        assert r.status_code == 200, r.text
        fresh = publish_bundle_and_make_public(client, publisher_headers, agent["id"])
        bundle_uuid = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()["bundle_uuid"]
    else:
        fresh = publish_bundle(client, publisher_headers, agent["id"])
        bundle_uuid = fresh["bundle_uuid"]
    return {"bundle_uuid": bundle_uuid, "agent_id": agent["id"]}


# ---------------------------------------------------------------------------
# Pass 1 — installed agents
# ---------------------------------------------------------------------------


def test_pass1_routes_to_senders_own_installed_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Pass1-{random_lower_string()[:6]}")
    drain_tasks()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="hello", sender_email=user["email"])
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    assert sessions[0]["integration_type"] == "channel_google_chat"


# ---------------------------------------------------------------------------
# Pass 1 — ownership filter (`_route_installed`), pinned directly
# ---------------------------------------------------------------------------


def _sender_user(client: TestClient, superuser_headers: dict[str, str], db: Session) -> User:
    """A real, persisted sender account — required by `_route_installed`
    (`route_installed` in tests.utils.server_channel), which reads `.id`."""
    sender, _ = create_random_user_with_headers(client)
    return db.get(User, uuid.UUID(sender["id"]))


def test_pass1_ownership_filter_rejects_an_identity_route(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """An identity route is, by construction, someone else's agent — reject
    outright regardless of what `agent_id` it names."""
    sender = _sender_user(client, superuser_token_headers, db)
    fake_result = build_routing_result(agent_id=uuid.uuid4(), is_identity=True)

    with patch(_ROUTE_MESSAGE_TARGET, return_value=fake_result):
        agent = route_installed(db, sender, "hello")

    assert agent is None


def test_pass1_ownership_filter_rejects_agent_owned_by_another_user(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The authoritative check: even a non-identity route naming a REAL agent
    is rejected if that agent isn't owned by the sender — this is what stops
    an admin route (or any router bug) from handing an external caller a
    session inside somebody else's workspace."""
    other_owner, other_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, other_owner["id"])
    create_random_ai_credential(client, other_headers, set_default=True)
    foreign_agent = create_agent_via_api(
        client, other_headers, name=f"Foreign-{random_lower_string()[:6]}"
    )
    drain_tasks()

    sender = _sender_user(client, superuser_token_headers, db)
    fake_result = build_routing_result(agent_id=uuid.UUID(foreign_agent["id"]), is_identity=False)

    with patch(_ROUTE_MESSAGE_TARGET, return_value=fake_result):
        agent = route_installed(db, sender, "hello")

    assert agent is None


def test_pass1_ownership_filter_rejects_a_deleted_or_nonexistent_agent(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """`db.get(Agent, result.agent_id)` returning None (the route named an
    agent that's gone — deleted, or never existed) must decline cleanly, not
    raise."""
    sender = _sender_user(client, superuser_token_headers, db)
    fake_result = build_routing_result(agent_id=uuid.uuid4(), is_identity=False)  # never persisted

    with patch(_ROUTE_MESSAGE_TARGET, return_value=fake_result):
        agent = route_installed(db, sender, "hello")

    assert agent is None


def test_pass1_ownership_filter_swallows_a_router_exception(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A router outage (or any bug in AppMCPRoutingService.route_message) must
    not 500 the webhook — `_route_installed` catches and returns None so the
    pipeline falls through to Pass 2 instead of propagating."""
    sender = _sender_user(client, superuser_token_headers, db)

    with patch(_ROUTE_MESSAGE_TARGET, side_effect=RuntimeError("router exploded")):
        agent = route_installed(db, sender, "hello")

    assert agent is None


# ---------------------------------------------------------------------------
# Pass 2 — candidate filtering
# ---------------------------------------------------------------------------


def test_pass2_excludes_private_bundle_from_candidates(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A bundle on the auto-install list that is NOT public/granted to the
    consumer must never be offered to the classifier — membership on the
    list is not an implicit grant. With the classifier mocked to answer
    "no match" (the only truthful answer once the private bundle is
    filtered out before the call), the message must get the no-match reply.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    # NOT made public — publish_bundle leaves default (non-public) visibility.
    private_agent = create_agent_via_api(client, publisher_headers, name=f"PrivateOnly-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{private_agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle private-only requests"},
    )
    assert r.status_code == 200, r.text
    revision = publish_bundle(client, publisher_headers, private_agent["id"])
    private_bundle_uuid = revision["bundle_uuid"]
    # Confirm it is NOT public (default visibility).
    bundle_row = client.get(f"{API}/bundles/{private_bundle_uuid}", headers=publisher_headers).json()
    assert bundle_row["visibility"] != "public"

    add_auto_install_bundle(client, superuser_token_headers, private_bundle_uuid)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])

    # No classifier answer is named, so the classifier is patched to RAISE. That
    # is the assertion: the private bundle never becomes a candidate, so with no
    # other candidate the pass short-circuits and the classifier is never asked.
    # If it ever is, this fails at the call with a message saying so.
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    # The no-match reply is delivered asynchronously (via _reply/adapter
    # .send_message), NOT in the webhook's own sync response — a new-thread
    # message always acks REPLY_WORKING synchronously regardless of how
    # routing eventually resolves.
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any("couldn't find an assistant" in t for t in reply_texts), reply_texts

    assert list_sessions(client, consumer_headers) == []


def test_pass2_excludes_already_installed_bundle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A bundle already installed by the consumer (even outside the channel
    flow, e.g. via the catalog directly) must be excluded from Pass 2
    candidates — Pass 1 would have handled it if it matched, and Pass 2
    installing it again would be wrong.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt="Handle already-installed requests", name_prefix="AlreadyInst"
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    # Consumer installs it directly via the catalog BEFORE any channel message.
    bundle_public = client.get(f"{API}/bundles/{bundle['bundle_uuid']}", headers=consumer_headers).json()
    install_resp = client.post(
        f"{API}/catalog/{bundle_public['bundle_id']}/install", headers=consumer_headers, json={}
    )
    assert install_resp.status_code == 200, install_resp.text
    drain_tasks()

    # This auto-created a personal app-mcp route too (install-time auto-route),
    # so Pass 1 will actually match now — which is itself the CORRECT outcome:
    # "already installed" means Pass 1 handles it, Pass 2 is never consulted.
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])
    # Naming no classifier answer patches it to raise, which is a stronger form
    # of the `assert_not_called()` this used to end with: it fails at the moment
    # of the call rather than afterwards. Pass 1 handles it via the `only_one`
    # short-circuit; Pass 2 never runs.
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [s for s in list_sessions(client, consumer_headers) if s["agent_id"] == install_resp.json()["id"]]
    assert len(sessions) == 1


def test_pass2_excludes_bundle_missing_trigger_prompt(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A public auto-install candidate with no router_trigger_prompt can never
    be offered to the classifier — it has nothing to classify against."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt=None, name_prefix="NoTrigger"
    )
    # Flip to public/listed manually (publish_bundle doesn't).
    client.patch(
        f"{API}/bundles/{bundle['bundle_uuid']}",
        headers=publisher_headers,
        json={"is_listed": True, "visibility": "public"},
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])
    # As above: no answer named means the classifier raises if it is reached.
    # A candidate with nothing to classify against must never be offered, so
    # the pass finds no candidates at all and short-circuits before the model.
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any("couldn't find an assistant" in t for t in reply_texts), reply_texts


def test_no_match_reply_when_pass1_and_pass2_both_miss(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers, auto_register_users=True)
    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="anybody home?",
        sender_email=f"nomatch-{random_lower_string()[:8]}@example.com",
    )
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any("couldn't find an assistant" in t for t in reply_texts), reply_texts


# ---------------------------------------------------------------------------
# Binding self-heal + session-deleted recovery
# ---------------------------------------------------------------------------


def test_failed_binding_self_heals_on_next_message_from_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A `failed` binding is deleted and re-routed from scratch the next time
    its OWNER messages the same thread — a transient failure doesn't wedge
    the thread permanently.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"SelfHeal-{random_lower_string()[:6]}")
    drain_tasks()
    # Re-fetch: environment provisioning is a background task, so the agent
    # dict returned by create_agent_via_api still has active_environment_id=None.
    agent = client.get(f"{API}/agents/{agent['id']}", headers=headers).json()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    # Strip the active environment so the first message's ingest fails.
    env_id = agent["active_environment_id"]
    assert client.delete(f"{API}/environments/{env_id}", headers=headers).status_code == 200

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event1 = build_message_event(thread_key=thread_key, text="first try", sender_email=user["email"])
    resp1, _ = _post(client, channel, signer, event1)
    assert resp1.status_code == 200
    assert list_sessions(client, headers) == []  # ingest failed, binding is `failed`

    # Give the agent a working environment again.
    new_env = create_environment(client, headers, agent["id"])
    activate_environment(client, headers, agent["id"], new_env["id"])

    event2 = build_message_event(thread_key=thread_key, text="second try", sender_email=user["email"])
    resp2, _ = _post(client, channel, signer, event2)
    assert resp2.status_code == 200

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    user_msgs = [m for m in list_messages(client, headers, sessions[0]["id"]) if m["role"] == "user"]
    assert any("second try" in (m["content"] or "") for m in user_msgs)


def test_session_deleted_recovers_with_a_fresh_session_on_next_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Deleting the bound session SET NULLs `session_id` on the binding; the
    next message opens a fresh session on the SAME bound agent rather than
    re-routing from scratch.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"SessDel-{random_lower_string()[:6]}")
    drain_tasks()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event1 = build_message_event(thread_key=thread_key, text="round one", sender_email=user["email"])
    resp1, _ = _post(client, channel, signer, event1)
    assert resp1.status_code == 200
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    old_session_id = sessions[0]["id"]

    # Superuser deletes the session (bypasses ownership check).
    r = client.delete(f"{API}/sessions/{old_session_id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/sessions/{old_session_id}", headers=headers).status_code == 404

    # Same thread, same owner, next message.
    event2 = build_message_event(thread_key=thread_key, text="round two", sender_email=user["email"])
    resp2, _ = _post(client, channel, signer, event2)
    assert resp2.status_code == 200

    sessions_after = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions_after) == 1
    assert sessions_after[0]["id"] != old_session_id  # fresh session, same agent
    user_msgs = [m for m in list_messages(client, headers, sessions_after[0]["id"]) if m["role"] == "user"]
    assert any("round two" in (m["content"] or "") for m in user_msgs)
