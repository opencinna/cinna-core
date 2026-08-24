"""Routing + binding lifecycle — plan §13 checklist.

Covers:
  - Pass 1 (the sender's own agents) match, on a standalone agent with no
    `AppAgentRoute` at all — the shape that used to be invisible to routing.
  - Pass 1 candidate scoping (`ChannelCandidateProvider`): a foreign agent is
    absent from the ballot rather than filtered off it afterwards, and an
    owned agent with nothing to classify on is a recorded skip.
  - Pass 1's two remaining post-classification guards, both unreachable
    through the real call graph and pinned by forging a candidate: the
    ownership postcondition that keeps an external Google Chat sender out of
    a stranger's workspace, and a candidate whose row has vanished.
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
from app.models import ChannelUserAgent, ChannelUserSetting, User
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import (
    create_agent_via_api,
    list_agents,
    set_router_trigger_prompt,
    update_agent,
)
from tests.utils.app_agent_route import (
    create_admin_route,
    create_user_route,
    list_user_routes,
)
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle, publish_bundle_and_make_public
from tests.utils.environment import activate_environment, create_environment
from tests.utils.identity import (
    create_identity_binding,
    list_identity_contacts,
    toggle_identity_contact,
)
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_channel_candidate,
    build_message_event,
    create_server_channel,
    post_webhook,
    route_installed,
    update_server_channel,
)
from tests.utils.routing import (
    classification,
    enter_classifier_patch,
    get_routing_trace,
    list_routing_traces,
)
from tests.utils.session import list_sessions
from tests.utils.message import list_messages
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_CANDIDATE_PROVIDER_TARGET = (
    "app.services.routing.channel_candidate_provider."
    "ChannelCandidateProvider.build"
)
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"
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
    """The reported case: a standalone agent with **no `AppAgentRoute` at all**.

    `create_agent_via_api` leaves `bundle_uuid` NULL, and a standalone agent
    deliberately never gets an auto-route — which is exactly why this sender's
    own agent used to be missing from the Pass-1 ballot. The only setup the
    channel path needs now is the agent's own `router_trigger_prompt`.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Pass1-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="hello", sender_email=user["email"])
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    assert sessions[0]["integration_type"] == "channel_google_chat"


# ---------------------------------------------------------------------------
# Pass 1 — the candidate set itself (`ChannelCandidateProvider`)
# ---------------------------------------------------------------------------


def _sender_user(client: TestClient, superuser_headers: dict[str, str], db: Session) -> User:
    """A real, persisted sender account — required by `_route_installed`
    (`route_installed` in tests.utils.server_channel), which reads `.id`."""
    sender, _ = create_random_user_with_headers(client)
    return db.get(User, uuid.UUID(sender["id"]))


def _pass1_candidates(client, superuser_headers, channel) -> list[dict]:
    """The `pass_1` candidate rows of this channel's single routing trace."""
    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_headers, page["data"][0]["id"])
    return [
        c
        for stage in detail["stages"]
        if stage["stage"] == "pass_1"
        for c in stage["candidates"]
    ]


def test_pass1_candidates_are_only_agents_the_sender_owns(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An admin route assigned to the sender, pointing at ANOTHER user's agent,
    is **absent from the ballot** — not skipped after the classifier saw it.

    The setup is the incident's own shape, and it is what makes this a test
    rather than a tautology: the foreign agent is reachable by the sender
    through an active, enabled, `channel_app_mcp` admin route, so
    `get_effective_routes_for_user` DOES return it and the old implementation
    put it in front of the classifier. It also has a trigger prompt, so
    eligibility is not what excludes it. Ownership is — applied when the
    candidate set is built, not to the classifier's answer.

    Naming no classifier answer is the second half of the assertion: once the
    ballot is scoped to the sender's own agents it is empty, so nothing should
    classify, and a classifier that runs anyway fails at the call.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    sender, _sender_headers = create_random_user_with_headers(client)

    other_owner, other_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, other_owner["id"])
    create_random_ai_credential(client, other_headers, set_default=True)
    foreign_agent = create_agent_via_api(
        client, other_headers, name=f"Foreign-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, other_headers, foreign_agent["id"], "Handle anything")

    # Reachable BY THE SENDER over App MCP: assigned and auto-enabled (an
    # assignment made for anyone but the route's creator otherwise lands
    # `is_enabled=False`, which would make this a different scenario).
    create_admin_route(
        client,
        superuser_token_headers,
        foreign_agent["id"],
        trigger_prompt="Handle anything",
        assigned_user_ids=[sender["id"]],
        auto_enable_for_users=True,
    )
    # Precondition, asserted rather than assumed: the sender really can reach
    # this foreign agent over App MCP. Without it this test would pass against
    # ANY implementation — including the one it exists to pin — because the
    # sender owns nothing and the ballot is empty either way.
    shared = list_user_routes(client, _sender_headers)["shared_routes"]
    reachable = [r for r in shared if r["agent_name"] == foreign_agent["name"]]
    assert len(reachable) == 1, shared
    assert reachable[0]["is_active"] is True and reachable[0]["is_enabled"] is True

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=sender["email"],
    )
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    assert any(
        "couldn't find an assistant" in c.args[-1] for c in send_mock.await_args_list
    )

    candidates = _pass1_candidates(client, superuser_token_headers, channel)
    assert [c for c in candidates if c["ref_id"] == foreign_agent["id"]] == [], candidates


def test_pass1_candidates_never_include_an_identity_contact(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """§2.1's other failure mode, pinned directly.

    This is the incident's actual shape (plan §1): Gemini picked the identity
    route at confidence 0.8, and Pass 1 rejected it only *after* the decision
    had already been spent on it — an identity contact was on the ballot at
    all. `ChannelCandidateProvider.build` only ever queries
    `Agent.owner_id == user_id`, so an identity contact cannot enter the set
    by construction, but nothing pinned that until now.

    "Absent entirely" is checked two ways, both stronger than "skipped after
    the classifier ran": no `identity_stage2` stage exists on a channel trace
    at all (the Stage-2 handoff Pass 1 used to make is gone, not merely made
    to lose), and the identity agent never appears as a candidate — eligible
    or skipped — on any stage that does run.

    The identity binding is set up so that it is genuinely reachable on the
    identity surface: assigned to the sender by its owner, then switched on by
    the sender themself (`toggle_identity_contact` — there is no superuser-free
    switch on this surface, unlike the App MCP admin route above). The point
    of this test is that "reachable" on the identity surface must still mean
    "absent" on the channel one.

    **Read the precondition, which became load-bearing in phase 3 of the
    channels & identity unification and was merely incidental before it.** This
    sender has no `channel_user_setting` row, so `allow_identity_routing`
    resolves to its `false` default and `ChannelRoutingService` does not call
    `IdentityCandidateProvider` at all. Identity *can* now reach a channel
    ballot — that is the feature — and what stays true here is the narrower
    statement this test was always making: nothing on the identity surface
    alone puts a person on a channel ballot; only the sender's own
    channel-level opt-in does. The switched-on counterpart, and the trace-level
    proof that the switch is what decides it, are in
    `server_channels_identity_trace_test.py`.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    sender, sender_headers = create_random_user_with_headers(client)

    owner, owner_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, owner["id"])
    create_random_ai_credential(client, owner_headers, set_default=True)
    identity_agent = create_agent_via_api(
        client, owner_headers, name=f"IdentityContact-{random_lower_string()[:6]}"
    )
    drain_tasks()
    create_identity_binding(
        client,
        owner_headers,
        identity_agent["id"],
        trigger_prompt="Handle anything",
        assigned_user_ids=[sender["id"]],
    )
    toggle_identity_contact(client, sender_headers, owner["id"], True)

    # Precondition, asserted rather than assumed: the identity contact really
    # is available to the sender. Without it this test would pass against ANY
    # implementation — including the one it exists to pin — because an
    # unreachable contact is absent from the ballot for a trivial reason.
    contacts = list_identity_contacts(client, sender_headers)
    reachable = [c for c in contacts if c["owner_id"] == owner["id"]]
    assert len(reachable) == 1, contacts
    assert reachable[0]["is_enabled"] is True, reachable
    assert reachable[0]["agent_count"] >= 1, reachable

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=sender["email"],
    )
    # No classifier answer named: the sender owns nothing, so the correctly
    # scoped ballot is empty and neither pass should reach the classifier at
    # all. The default stub raises if it is reached.
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    assert any(
        "couldn't find an assistant" in c.args[-1] for c in send_mock.await_args_list
    )

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_token_headers, page["data"][0]["id"])

    # Absent entirely, first sense: no identity stage exists on a channel
    # trace at all.
    stage_names = {stage["stage"] for stage in detail["stages"]}
    assert "identity_stage2" not in stage_names, detail["stages"]

    # Absent entirely, second sense: the identity agent is not a candidate —
    # eligible or skipped — on any stage that did run.
    all_candidates = [c for stage in detail["stages"] for c in stage["candidates"]]
    assert [c for c in all_candidates if c["ref_id"] == identity_agent["id"]] == [], all_candidates
    assert [c for c in all_candidates if c["name"] == identity_agent["name"]] == [], all_candidates
    assert all(c["source"] != "identity" for c in all_candidates), all_candidates


def test_pass1_ignores_the_app_mcp_enablement_toggles(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Root cause §2.2: an MCP exposure switch must not gate a channel.

    The sender's own agent carries every App-MCP "off" switch at once — the
    route is inactive AND not exposed on the `app_mcp` channel — and still
    routes from the sender's own chat app. What a user exposes over MCP is not
    a statement about what they can reach from Google Chat, and inheriting
    those toggles made an owner's own agent silently unreachable.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"MCPOff-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    create_user_route(
        client,
        headers,
        agent["id"],
        trigger_prompt="Handle anything",
        channel_app_mcp=False,
        is_active=False,
    )

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=user["email"],
    )
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1


def test_pass1_admits_an_agent_whose_only_config_is_example_prompts(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Eligibility is trigger prompt **or** examples — the `or` half, pinned.

    `Agent.example_prompts` alone is enough to classify on, and the list is
    joined with newlines into the one `prompt_examples` string a candidate
    carries. Without this, inverting the `or` would leave the agent recorded
    as `SKIP_NO_TRIGGER_PROMPT` and nothing would catch it.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"ExOnly-{random_lower_string()[:6]}")
    drain_tasks()
    # No router_trigger_prompt at all — examples are the whole configuration.
    update_agent(
        client, headers, agent["id"], example_prompts=["book a meeting", "cancel my 3pm"]
    )

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="book a meeting",
        sender_email=user["email"],
    )
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1

    row = next(
        c
        for c in _pass1_candidates(client, superuser_token_headers, channel)
        if c["ref_id"] == agent["id"]
    )
    assert row["eligible"] is True, row
    assert row["prompt_examples"] == "book a meeting\ncancel my 3pm", row


def test_pass1_records_an_owned_agent_with_nothing_to_classify_on_as_a_skip(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An ineligible owned agent is a recorded skip, never a silent drop.

    "The expected agent was never a candidate at all" is the failure that
    actually bites, and a ballot listing only the finalists cannot explain it.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"NoTrigger-{random_lower_string()[:6]}")
    drain_tasks()  # deliberately no router_trigger_prompt and no example_prompts

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=user["email"],
    )
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    rows = [
        c
        for c in _pass1_candidates(client, superuser_token_headers, channel)
        if c["ref_id"] == agent["id"]
    ]
    assert len(rows) == 1, rows
    assert rows[0]["eligible"] is False
    assert rows[0]["skip_reason"] == "no_trigger_prompt"


def test_pass1_ownership_postcondition_rejects_a_foreign_agent(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The defence-in-depth guard, reached the only way it now can be.

    `agent.owner_id == user.id` is unreachable through the real call graph —
    every candidate comes out of `WHERE owner_id = sender`. It is kept because
    it is the same invariant `ChannelIngestionService.assert_access` asserts
    for `channel_caller` sessions, so it is pinned by forging the one state
    that would trip it: a candidate provider that returned a foreign agent.
    """
    other_owner, other_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, other_owner["id"])
    create_random_ai_credential(client, other_headers, set_default=True)
    foreign_agent = create_agent_via_api(
        client, other_headers, name=f"Foreign-{random_lower_string()[:6]}"
    )
    drain_tasks()

    sender = _sender_user(client, superuser_token_headers, db)
    forged = [build_channel_candidate(ref_id=foreign_agent["id"], name="Not yours")]

    with patch(_CANDIDATE_PROVIDER_TARGET, return_value=forged):
        with patch(_CLASSIFY_TARGET, return_value=classification(foreign_agent["id"])):
            agent = route_installed(db, sender, "hello")

    assert agent is None


def test_pass1_declines_when_the_classifier_names_an_agent_that_is_gone(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A candidate whose row vanished mid-decision declines cleanly, not raises."""
    sender = _sender_user(client, superuser_token_headers, db)
    ghost_id = str(uuid.uuid4())  # never persisted
    forged = [build_channel_candidate(ref_id=ghost_id, name="Ghost")]

    with patch(_CANDIDATE_PROVIDER_TARGET, return_value=forged):
        with patch(_CLASSIFY_TARGET, return_value=classification(ghost_id)):
            agent = route_installed(db, sender, "hello")

    assert agent is None


def test_pass1_swallows_a_classifier_exception(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A provider outage (or any bug under `_route_installed`) must not 500 the
    webhook — Pass 1 catches, records the error on the trace, and returns None
    so the pipeline falls through to Pass 2 instead of propagating."""
    sender = _sender_user(client, superuser_token_headers, db)

    with patch(_CANDIDATE_PROVIDER_TARGET, side_effect=RuntimeError("router exploded")):
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

    # The install restored the bundle's `router_trigger_prompt` onto the
    # consumer's own agent, so Pass 1 will match it — which is itself the
    # CORRECT outcome: "already installed" means Pass 1 handles it and Pass 2
    # is never consulted. Pass 2 is what must not run here, and it cannot:
    # Pass 1 returning an agent is what gates it.
    installed_agent_id = install_resp.json()["id"]
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])
    # No classifier answer named, and that is the assertion: the bundle IS on
    # the auto-install list, but this consumer already installed it, so Pass 2
    # has nothing left to offer them and Pass 1's `only_one` short-circuit
    # fires. `_post`'s default stub raises if the classifier is reached.
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [s for s in list_sessions(client, consumer_headers) if s["agent_id"] == installed_agent_id]
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
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

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
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

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


# ---------------------------------------------------------------------------
# Pass 2 — the agent-scope gate (`_catalog_may_run`)
# ---------------------------------------------------------------------------


def _pass_2_stage(client, superuser_headers, channel) -> dict | None:
    """This channel's single trace's `pass_2` stage, or None if it has none."""
    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_headers, page["data"][0]["id"])
    stages = [st for st in detail["stages"] if st["stage"] == "pass_2"]
    return stages[0] if stages else None


def test_pass2_does_not_run_when_the_channel_scope_is_restricted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A decided product semantic, not an implementation convenience.

    `agent_scope` restricted + `allow_auto_install=True` used to be a dead
    configuration: Pass 2 installed a bundle whose agent is out of scope **by
    construction** (`ChannelCandidateProvider._in_scope` admits an installed
    agent only under `"all"`, or under `"list"` once the sender adds it), so the
    first message installed and every later thread dead-ended.

    The assertion is the absence of the install, plus the trace saying so: a
    `pass_2` stage carrying `PASS_2_SCOPE_RESTRICTED_NOTE` and no candidate
    rows, which is the difference between "policy stopped it" and "the
    auto-install list was empty".
    """
    channel = _channel(client, superuser_token_headers)
    update_server_channel(
        client, superuser_token_headers, channel["id"], default_agent_scope="none"
    )
    signer = GoogleChatJWTSigner()

    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client,
        publisher_headers,
        trigger_prompt="Handle scope-gate requests",
        name_prefix="ScopeGate",
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    # No classifier answer named, so `classify` is patched to raise: neither
    # pass may reach it. Pass 1 has an empty ballot; Pass 2 must not run at all.
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    # Nothing was installed for the consumer — the whole point of the gate.
    owned = list_agents(client, consumer_headers)["data"]
    assert all(a["bundle_uuid"] != bundle["bundle_uuid"] for a in owned), owned

    stage = _pass_2_stage(client, superuser_token_headers, channel)
    assert stage is not None, "policy barred Pass 2 but the trace does not say so"
    assert stage["candidates"] == []
    assert "limited to an explicitly chosen set" in (stage["reason"] or "")


def _scope_list_containing(db: Session, channel: dict, user: dict, agent: dict) -> None:
    """The sender's own settings row: `agent_scope="list"`, holding one agent.

    Written through the session rather than through
    `PUT /users/me/channels/{channel_id}` — the only production creator of this
    row — as a focused setup shortcut: what this test needs is a routing input,
    not a round trip through the settings API, which owns its own coverage in
    `server_channels_user_settings_test.py`.

    This used to be a workaround rather than a choice: the route could not
    create a row under these fixtures at all while `_create_setting` wrapped
    its insert in `session.begin_nested()`. That is fixed — the insert is now
    a native upsert — so the shortcut stands on its own terms or not at all.
    """
    setting = ChannelUserSetting(
        server_channel_id=uuid.UUID(channel["id"]),
        user_id=uuid.UUID(user["id"]),
        agent_scope="list",
    )
    db.add(setting)
    db.flush()
    db.add(
        ChannelUserAgent(
            channel_user_setting_id=setting.id, agent_id=uuid.UUID(agent["id"])
        )
    )
    db.commit()


def test_only_one_short_circuits_when_the_scope_bars_pass_2(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The other reading of `_catalog_may_run`, and its behaviour change.

    `_catalog_may_run` is read in two places that must not disagree: `decide`
    (whether Pass 2 runs) and Pass 1's single-candidate probe (whether the
    `only_one` short-circuit may skip the classifier). With the scope term
    added, a sender with exactly one in-scope agent short-circuits even though
    the auto-install list is non-empty — correct under the short-circuit's own
    rule (*sound only when there is no alternative to choose between*), because
    Pass 2 provably cannot run here.

    The classifier is patched to raise, so "the short-circuit fired" is
    asserted by the delivery succeeding at all.
    """
    channel = _channel(client, superuser_token_headers)
    update_server_channel(
        client, superuser_token_headers, channel["id"], default_agent_scope="none"
    )
    signer = GoogleChatJWTSigner()

    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    create_random_ai_credential(client, consumer_headers, set_default=True)
    agent = create_agent_via_api(
        client, consumer_headers, name=f"OnlyOne-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent["id"], "Handle anything")

    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client,
        publisher_headers,
        trigger_prompt="Handle short-circuit requests",
        name_prefix="ShortCircuit",
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    # The sender's own list, containing exactly their one agent: scope is
    # restricted (so Pass 2 is barred) while Pass 1 still has one candidate.
    _scope_list_containing(db, channel, consumer, agent)

    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [
        s for s in list_sessions(client, consumer_headers) if s["agent_id"] == agent["id"]
    ]
    assert len(sessions) == 1, "the sole in-scope agent should have been routed to"

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    detail = get_routing_trace(client, superuser_token_headers, page["data"][0]["id"])
    assert detail["match_method"] == "only_one", detail
