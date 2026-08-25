"""`ChannelPolicyService` observed through routing — plan §7 + §11's authorised
addition, minus what already lives elsewhere.

Covers, each proved through a real webhook delivery (or, where the API surface
genuinely has no other way to observe the claim — see the kill-switch test's
docstring — through the closest thing the API exposes):

  - No settings row ⇒ the channel's admin defaults apply, for a sender who by
    construction can never have one (master plan §3.3).
  - `visibility="restricted"` declines a sender with the same reply shape as a
    whitelist miss, and a grant turns the decline into a route.
  - `channel.enabled=False` overrides an explicit user `is_enabled=True`.
  - `agent_scope="list"` / `"none"` record out-of-scope owned agents as skips
    (`SKIP_NOT_IN_CHANNEL_SCOPE`), never as absences (master plan §3.5).
  - `pinned_agent_id` skips classification, routes to the pin, and still
    leaves a trace row (`match_method="pinned"`) — both when it resolves and
    when it cannot (the agent was deleted, or changed hands).
  - `allow_auto_install=False` bars Pass 2, and the trace says why
    (`PASS_2_NOT_ALLOWED_NOTE`); a pin bars it too
    (`PASS_2_PINNED_NOTE`), and neither note is written over a trace Pass 1
    already failed.
  - The decline gate (`policy.is_available`) applies to an already-bound
    thread, not only a new one — an explicitly authorised addition, not in
    plan §7's list.

Two existing files cover adjacent ground and are not duplicated here:
`server_channels_routing_test.py` (Pass 1 candidate construction, Pass 2
candidate filtering, the `PASS_2_SCOPE_RESTRICTED_NOTE` / conditional
`only_one` pair) and `server_channels_user_settings_test.py` (the settings
routes themselves — inherit/override provenance, `DELETE`, cross-user
isolation, the secret-adjacent-field defence).
"""
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import Agent
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.routing import (
    enter_classifier_patch,
    get_routing_trace,
    list_routing_traces,
    post_channel_message,
)
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
    post_webhook,
    replace_channel_grants,
    update_server_channel,
)
from tests.utils.session import list_sessions
from tests.utils.user import promote_to_developer
from tests.utils.user_channel import (
    delete_my_channel,
    find_my_channel,
    get_my_channels,
    update_my_channel,
)
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"

_REPLY_WORKING = "Got it — finding the right assistant for you…"
_REPLY_DENIED = (
    "Sorry, you don't have access to this assistant. "
    "Please contact your administrator."
)
_REPLY_NO_MATCH_SNIPPET = "couldn't find an assistant"


def _publish_public_bundle(client, publisher_headers, *, trigger_prompt: str, name_prefix: str) -> dict:
    """A public, listed bundle with a router trigger prompt — the Pass-2 shape."""
    agent = create_agent_via_api(
        client, publisher_headers, name=f"{name_prefix}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": trigger_prompt},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    bundle_uuid = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()[
        "bundle_uuid"
    ]
    return {"bundle_uuid": bundle_uuid, "agent_id": agent["id"]}


def _pass1_candidates(client, superuser_headers, channel) -> list[dict]:
    """The `pass_1` candidate rows of this channel's single routing trace."""
    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_headers, page["data"][0]["id"])
    return [
        c for stage in detail["stages"] if stage["stage"] == "pass_1" for c in stage["candidates"]
    ]


def _pass_2_stage(client, superuser_headers, channel) -> dict | None:
    """This channel's single trace's `pass_2` stage, or None if it has none."""
    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_headers, page["data"][0]["id"])
    stages = [st for st in detail["stages"] if st["stage"] == "pass_2"]
    return stages[0] if stages else None


def _post_then_mutate_before_drain(client, channel, signer, event, mutate_fn, **classify_kwargs):
    """Deliver the webhook synchronously, then run `mutate_fn` before draining.

    Exploits the real timing gap `ChannelRoutingService._catalog_may_run`'s
    docstring names explicitly: `handle_inbound` resolves and carries the
    policy synchronously, but the routing pass itself — including the pinned
    agent's re-fetch by id — runs later, inside `drain_tasks()`. Mutating the
    row in between reproduces that race deterministically instead of leaving
    it a documented-but-unexercised risk. `post_channel_message` couples the
    POST and the drain into one call and cannot express this; this is the
    same helper split by hand.
    """
    token = signer.token(audience=channel["config"]["project_number"])
    stub = StubAgentEnvConnector(response_text="ok")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        assert resp.status_code == 200
        mutate_fn()
        stack.enter_context(patch(_STREAM_TARGET, stub))
        send_mock = stack.enter_context(
            patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id"))
        )
        enter_classifier_patch(stack, **classify_kwargs)
        drain_tasks()
    return resp, send_mock


# ---------------------------------------------------------------------------
# 1. No settings row -> channel defaults apply (master plan §3.3)
# ---------------------------------------------------------------------------


def test_no_settings_row_channel_defaults_apply_to_an_auto_registered_sender(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The headline invariant, proved for the population it exists for.

    An auto-registered Google Chat sender never has a UI session in which a
    `channel_user_setting` row could be created — their account is created,
    if at all, INSIDE the very webhook request being decided, which makes a
    prior row a physical impossibility rather than merely an untested case.
    Both directions of the per-user toggle term are proved from that starting
    point: a channel whose admin default is on lets a brand-new sender
    straight through the policy gate on their first-ever contact; a channel
    whose admin default is off declines them just as surely, with nothing for
    them to have configured.
    """
    signer = GoogleChatJWTSigner()

    # --- Default ON (the model default) ----------------------------------
    channel_on = create_server_channel(
        client, superuser_token_headers, auto_register_users=True, email_whitelist="*"
    )
    event_on = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=f"{random_lower_string()}@example.com",
    )
    # No classifier answer named: this brand-new sender owns nothing and the
    # auto-install list is empty, so both passes' ballots are empty.
    resp_on, send_mock_on = post_channel_message(client, channel_on, signer, event_on)
    assert resp_on.status_code == 200
    # The synchronous reply is the tell: REPLY_WORKING means the policy gate
    # let them through to routing, despite no row ever having existed.
    assert resp_on.json().get("text") == _REPLY_WORKING, resp_on.json()
    # Routing itself finds nothing (empty catalog) — a later, async reply,
    # and not what this test is about.
    reply_texts = [c.args[-1] for c in send_mock_on.await_args_list]
    assert any(_REPLY_NO_MATCH_SNIPPET in t for t in reply_texts), reply_texts

    # --- Default OFF --------------------------------------------------------
    channel_off = create_server_channel(
        client, superuser_token_headers, auto_register_users=True, email_whitelist="*"
    )
    update_server_channel(
        client, superuser_token_headers, channel_off["id"], default_enabled_for_users=False
    )
    event_off = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=f"{random_lower_string()}@example.com",
    )
    resp_off, _ = post_channel_message(client, channel_off, signer, event_off)
    assert resp_off.status_code == 200
    assert resp_off.json().get("text") == _REPLY_DENIED, resp_off.json()


# ---------------------------------------------------------------------------
# 2. visibility="restricted"
# ---------------------------------------------------------------------------


def test_visibility_restricted_declines_without_grant_same_shape_as_whitelist_miss_then_routes_with_grant(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A restricted decline must not be an enumeration oracle: the reply has to
    be indistinguishable from a plain whitelist miss. Granting access is what
    turns the SAME sender's SAME kind of message into a route.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent = create_agent_via_api(
        client, consumer_headers, name=f"Restricted-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent["id"], "Handle anything")

    signer = GoogleChatJWTSigner()

    # A whitelist miss, for comparison.
    channel_whitelist_miss = create_server_channel(
        client, superuser_token_headers, email_whitelist="nomatch@only.example.com"
    )
    event_a = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp_a, _ = post_channel_message(client, channel_whitelist_miss, signer, event_a)
    assert resp_a.status_code == 200

    # A restricted channel the consumer is whitelisted for but not granted.
    channel_restricted = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_server_channel(
        client, superuser_token_headers, channel_restricted["id"], visibility="restricted"
    )
    event_b = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp_b, _ = post_channel_message(client, channel_restricted, signer, event_b)
    assert resp_b.status_code == 200

    # Same shape: status and body are byte-identical, so a client cannot tell
    # "not whitelisted" apart from "not granted" from the reply alone.
    assert resp_a.status_code == resp_b.status_code
    assert resp_a.json() == resp_b.json()
    assert resp_b.json().get("text") == _REPLY_DENIED
    assert list_sessions(client, consumer_headers) == []

    # Grant access, and the SAME sender on a NEW thread now routes.
    replace_channel_grants(
        client, superuser_token_headers, channel_restricted["id"], [consumer["id"]]
    )
    event_c = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    # Exactly one eligible candidate, empty auto-install list: `only_one`
    # short-circuits, so no classifier answer is named.
    resp_c, _ = post_channel_message(client, channel_restricted, signer, event_c)
    assert resp_c.status_code == 200

    sessions = [s for s in list_sessions(client, consumer_headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1, "granting access should have let this sender's own agent route"


# ---------------------------------------------------------------------------
# 3. channel.enabled=False overrides everything
# ---------------------------------------------------------------------------


def test_channel_enabled_false_overrides_an_explicit_user_enabled_true(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`channel.enabled` is term 1 of the conjunction and it is checked first
    everywhere it is checked — `ChannelPolicyService.describe` for the
    resolved projection, and `ServerChannelService.get_by_webhook_token` for
    the live webhook, which resolves an ENABLED channel by token and 404s a
    disabled one before `ChannelPolicyService` is even consulted (the same
    "no existence leak" rule as an unknown token — see
    `ServerChannelBase.enabled`'s docstring).

    That means disabling a channel does not merely flip `is_available`; it
    makes the channel invisible everywhere a regular user's API surface can
    look — `GET /users/me/channels` drops it from the list, `PUT`/`DELETE`
    404 it — REGARDLESS of what the user had explicitly chosen. This is
    proved at the strongest level the API actually exposes it: disable a
    channel the consumer had explicitly, deliberately turned ON for
    themselves, watch it disappear from every one of their routes and the
    webhook 404, then re-enable it and watch their explicit choice come back
    exactly as they left it — which is what proves the override did not also
    erase their row underneath it.

    (`ChannelPolicyService.describe`'s specific `channel_enabled=False` +
    `is_enabled=True` computation has no route that observes it directly:
    `POST /admin/routing/simulate` never resolves a channel's policy at all —
    the route passes no `channel_id`, so every simulate runs under
    `ResolvedChannelPolicy.for_no_channel()` — and every user-facing route
    gates on `channel.enabled` before anything else. This test proves the
    plan's behavioural claim, "overrides everything", at the level the API
    genuinely supports: total invisibility rather than a visible decline.)
    """
    consumer, consumer_headers = make_user_and_headers(client)
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")

    saved = update_my_channel(client, consumer_headers, channel["id"], is_enabled=True)
    assert saved["is_enabled"] is True and saved["is_enabled_inherited"] is False

    update_server_channel(client, superuser_token_headers, channel["id"], enabled=False)

    listed = get_my_channels(client, consumer_headers)
    assert channel["id"] not in {c["id"] for c in listed}, listed
    update_my_channel(client, consumer_headers, channel["id"], expected_status=404, is_enabled=False)
    delete_my_channel(client, consumer_headers, channel["id"], expected_status=404)

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 404

    # Re-enable: the consumer's explicit choice survived underneath, untouched.
    update_server_channel(client, superuser_token_headers, channel["id"], enabled=True)
    restored = find_my_channel(get_my_channels(client, consumer_headers), channel["id"])
    assert restored["is_enabled"] is True, restored
    assert restored["is_enabled_inherited"] is False, restored


# ---------------------------------------------------------------------------
# 4. agent_scope="list" / "none"
# ---------------------------------------------------------------------------


def test_agent_scope_list_records_the_out_of_scope_agent_as_a_skip_not_an_absence(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """master plan §3.5: a candidate excluded without a `skip_reason` cannot
    diagnose the failure that actually bites. An agent outside the sender's
    own `agent_scope="list"` selection must appear in the trace as a skip
    (`SKIP_NOT_IN_CHANNEL_SCOPE`), not merely be missing from the ballot.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent_in = create_agent_via_api(
        client, consumer_headers, name=f"InScope-{random_lower_string()[:6]}"
    )
    agent_out = create_agent_via_api(
        client, consumer_headers, name=f"OutScope-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent_in["id"], "Handle anything")
    set_router_trigger_prompt(client, consumer_headers, agent_out["id"], "Handle anything too")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_my_channel(
        client, consumer_headers, channel["id"], agent_scope="list", agent_ids=[agent_in["id"]]
    )

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    # A restricted scope bars Pass 2 too (`_catalog_may_run`), so with exactly
    # one IN-SCOPE eligible candidate `only_one` short-circuits regardless of
    # the (empty) auto-install catalog — no classifier answer is named.
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    sessions = [
        s for s in list_sessions(client, consumer_headers) if s["agent_id"] == agent_in["id"]
    ]
    assert len(sessions) == 1, "the in-scope agent should have routed"

    candidates = _pass1_candidates(client, superuser_token_headers, channel)
    in_row = next(c for c in candidates if c["ref_id"] == agent_in["id"])
    assert in_row["eligible"] is True, in_row

    out_row = next(c for c in candidates if c["ref_id"] == agent_out["id"])
    assert out_row["eligible"] is False, out_row
    assert out_row["skip_reason"] == "not_in_channel_scope", out_row


def test_agent_scope_none_skips_every_owned_agent_and_the_trace_explains_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The confused user's real question is "I own agents, why did none
    answer" — `agent_scope="none"` must leave a trace that can answer it:
    every owned agent recorded as a skip, none silently absent.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent_1 = create_agent_via_api(
        client, consumer_headers, name=f"NoneScope1-{random_lower_string()[:6]}"
    )
    agent_2 = create_agent_via_api(
        client, consumer_headers, name=f"NoneScope2-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent_1["id"], "Handle anything")
    set_router_trigger_prompt(client, consumer_headers, agent_2["id"], "Handle anything too")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_my_channel(client, consumer_headers, channel["id"], agent_scope="none")

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp, send_mock = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any(_REPLY_NO_MATCH_SNIPPET in t for t in reply_texts), reply_texts
    assert list_sessions(client, consumer_headers) == []

    candidates = _pass1_candidates(client, superuser_token_headers, channel)
    assert {c["ref_id"] for c in candidates} == {agent_1["id"], agent_2["id"]}, candidates
    assert all(c["eligible"] is False for c in candidates), candidates
    assert all(c["skip_reason"] == "not_in_channel_scope" for c in candidates), candidates


# ---------------------------------------------------------------------------
# 5. pinned_agent_id
# ---------------------------------------------------------------------------


def test_pinned_agent_skips_classification_and_leaves_a_matched_trace_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A pin answers the routing question outright: classification is skipped
    even with TWO eligible agents on the ballot (so this cannot be explained
    away as `only_one`), the pinned agent is used, and — the thing this
    codebase has already had to fix once — a trace row still exists, naming
    the pin (`match_method="pinned"`).
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    pinned = create_agent_via_api(client, consumer_headers, name=f"Pinned-{random_lower_string()[:6]}")
    other = create_agent_via_api(client, consumer_headers, name=f"NotPinned-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, pinned["id"], "Handle pinned requests")
    set_router_trigger_prompt(client, consumer_headers, other["id"], "Handle other requests")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    saved = update_my_channel(client, consumer_headers, channel["id"], pinned_agent_id=pinned["id"])
    assert saved["pinned_agent_id"] == pinned["id"], saved

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="totally unrelated to either agent's wording",
        sender_email=consumer["email"],
    )
    # No classifier answer named: with the pin set, classification must never
    # be reached even though the sender owns TWO eligible agents (which would
    # otherwise force a classify) — the refusal stub fails loudly if it is.
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    all_sessions = list_sessions(client, consumer_headers)
    pinned_sessions = [s for s in all_sessions if s["agent_id"] == pinned["id"]]
    assert len(pinned_sessions) == 1
    assert all_sessions == pinned_sessions, "nothing should have routed to the un-pinned agent"

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_token_headers, page["data"][0]["id"])
    assert detail["match_method"] == "pinned", detail
    assert detail["outcome"] == "routed", detail
    assert detail["selected_agent_id"] == pinned["id"], detail

    candidates = _pass1_candidates(client, superuser_token_headers, channel)
    assert [c["ref_id"] for c in candidates] == [pinned["id"]], candidates
    assert candidates[0]["eligible"] is True, candidates


def test_pinned_agent_deleted_does_not_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The FK is `ON DELETE SET NULL`: deleting the pinned agent un-pins the
    channel rather than orphaning the settings row, and the sender must not
    be routed to an agent that no longer exists.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent = create_agent_via_api(client, consumer_headers, name=f"DeletedPin-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent["id"], "Handle anything")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_my_channel(client, consumer_headers, channel["id"], pinned_agent_id=agent["id"])

    del_resp = client.delete(f"{API}/agents/{agent['id']}", headers=consumer_headers)
    assert del_resp.status_code == 200, del_resp.text

    projected = find_my_channel(get_my_channels(client, consumer_headers), channel["id"])
    assert projected["pinned_agent_id"] is None, projected
    assert projected["has_settings"] is True, projected  # the row itself survives

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp, send_mock = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any(_REPLY_NO_MATCH_SNIPPET in t for t in reply_texts), reply_texts
    assert list_sessions(client, consumer_headers) == []


def _transfer_agent_ownership(db: Session, agent_id: str, new_owner_id: str) -> None:
    """Reassign an agent's `owner_id` directly. Rule-1 exemption.

    There is no API that reassigns an existing agent's owner — a clone or a
    catalog install creates a NEW agent for the new owner, it never mutates an
    existing row's `owner_id`. `ChannelUserSetting.pinned_agent_id`'s FK
    enforces existence, never ownership, and this is the only way to put the
    row into the state the FK alone would allow, so
    `ChannelPolicyService._owned_pin` (the code defending against exactly this
    gap) can be exercised at all.
    """
    agent = db.get(Agent, uuid.UUID(agent_id))
    assert agent is not None
    agent.owner_id = uuid.UUID(new_owner_id)
    db.add(agent)
    db.commit()


def test_pinned_agent_ownership_changed_does_not_route(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The FK enforces existence, not ownership — the other of the two shapes
    plan §7 asks for. The pin was valid when written; ownership changing
    hands afterwards must un-pin it at resolution time, not merely at write
    time, or a stale pin could route a stranger's message onto an agent that
    is no longer theirs.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent = create_agent_via_api(
        client, consumer_headers, name=f"ChangedHandsPin-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent["id"], "Handle anything")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_my_channel(client, consumer_headers, channel["id"], pinned_agent_id=agent["id"])

    other_owner, _ = make_user_and_headers(client)
    _transfer_agent_ownership(db, agent["id"], other_owner["id"])

    # The pin self-heals at resolution time — re-checked, not merely written
    # once and trusted forever.
    projected = find_my_channel(get_my_channels(client, consumer_headers), channel["id"])
    assert projected["pinned_agent_id"] is None, projected

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    resp, send_mock = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any(_REPLY_NO_MATCH_SNIPPET in t for t in reply_texts), reply_texts
    assert list_sessions(client, consumer_headers) == []


# ---------------------------------------------------------------------------
# 6. Pass 2 policy bars, and their trace notes
# ---------------------------------------------------------------------------


def test_allow_auto_install_false_blocks_pass_2_with_the_not_allowed_note(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`allow_auto_install=False` bars Pass 2 outright, and the trace must say
    it is a policy decision (`PASS_2_NOT_ALLOWED_NOTE`) rather than reading
    like a Pass 2 that ran over an empty catalog.
    """
    channel = create_server_channel(
        client, superuser_token_headers, auto_register_users=True, email_whitelist="*"
    )
    update_server_channel(client, superuser_token_headers, channel["id"], allow_auto_install=False)

    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt="Handle auto-install-off requests",
        name_prefix="AutoInstallOff",
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=f"{random_lower_string()}@example.com",
    )
    # The sender owns nothing (fresh auto-registered account), so Pass 1's
    # ballot is empty and Pass 2 is the only thing that could offer anything —
    # and policy bars it before any candidate is ever scanned.
    resp, send_mock = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any(_REPLY_NO_MATCH_SNIPPET in t for t in reply_texts), reply_texts

    stage = _pass_2_stage(client, superuser_token_headers, channel)
    assert stage is not None, "policy barred Pass 2 but the trace does not say so"
    assert stage["candidates"] == []
    assert (
        "installing a bundle for this sender is switched off for this channel"
        in (stage["reason"] or "")
    ), stage


def test_pass2_pinned_note_recorded_when_the_pin_resolution_races_the_background_task(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`PASS_2_PINNED_NOTE` — left untested until now (plan §7's addendum).

    Reachable only through the genuine race `_catalog_may_run`'s own docstring
    names: `handle_inbound` resolves the policy (pin included) synchronously,
    but the pin's `db.get` re-fetch happens later, inside the scheduled
    background routing task. Deleting the pinned agent in the gap between the
    webhook's sync response and `drain_tasks()` reproduces that race on
    purpose — `_post_then_mutate_before_drain` is the seam.

    `allow_auto_install=True` (the default) and a bundle genuinely on the
    catalog prove the note is attributable to the PIN specifically, not to
    the auto-install switch or the scope (both of which have their own,
    already-tested notes).
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent = create_agent_via_api(client, consumer_headers, name=f"RacyPin-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent["id"], "Handle anything")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_my_channel(client, consumer_headers, channel["id"], pinned_agent_id=agent["id"])

    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt="Handle racy pin requests",
        name_prefix="RacyPinBundle",
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )

    def _delete_the_pinned_agent() -> None:
        r = client.delete(f"{API}/agents/{agent['id']}", headers=consumer_headers)
        assert r.status_code == 200, r.text

    resp, send_mock = _post_then_mutate_before_drain(
        client, channel, signer, event, _delete_the_pinned_agent
    )
    assert resp.status_code == 200
    reply_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any(_REPLY_NO_MATCH_SNIPPET in t for t in reply_texts), reply_texts

    stage = _pass_2_stage(client, superuser_token_headers, channel)
    assert stage is not None, "the pin barred Pass 2 but the trace does not say so"
    assert stage["candidates"] == []
    assert "this sender has pinned an agent to this channel" in (stage["reason"] or ""), stage
    assert stage["not_run_code"] == "pinned", stage


def test_pass2_note_suppressed_when_pass1_already_recorded_an_error(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`_record_pass_2_not_run` is silent when the trace already carries an
    error: the truthful answer to "why did Pass 2 not run" is "Pass 1 failed
    first", and stamping a policy note over that would assert a reason that
    was never reached. Contrast directly with the `allow_auto_install=False`
    test above, which uses the SAME policy bar and gets the note — the only
    difference here is that Pass 1 blew up before policy was ever consulted.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer["id"])
    agent_1 = create_agent_via_api(client, consumer_headers, name=f"ErrRace1-{random_lower_string()[:6]}")
    agent_2 = create_agent_via_api(client, consumer_headers, name=f"ErrRace2-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, consumer_headers, agent_1["id"], "Handle anything")
    set_router_trigger_prompt(client, consumer_headers, agent_2["id"], "Handle anything too")

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    update_server_channel(client, superuser_token_headers, channel["id"], allow_auto_install=False)

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please help",
        sender_email=consumer["email"],
    )
    # Two eligible candidates force a real classify (the `only_one`
    # short-circuit cannot fire), and the classifier is made to raise a PLAIN
    # exception — the kind `_route_installed`'s own internal try/except
    # catches and records via `record_error`, unlike `UnstubbedLLMProvider`
    # (a `BaseException`, deliberately uncatchable by that clause).
    resp, _ = post_channel_message(
        client, channel, signer, event, classify_side_effect=RuntimeError("provider exploded")
    )
    assert resp.status_code == 200

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    detail = get_routing_trace(client, superuser_token_headers, page["data"][0]["id"])
    assert detail["error"], detail  # Pass 1 really did record an error

    stages = [s for s in detail["stages"] if s["stage"] == "pass_2"]
    assert stages == [], (
        "allow_auto_install=False would normally leave a pass_2 stage carrying "
        "PASS_2_NOT_ALLOWED_NOTE (see the test above) — here Pass 1 failed "
        "first, and the note must not be written over that error"
    )


# ---------------------------------------------------------------------------
# 7. The decline gate applies to an already-bound thread
#    (explicitly authorised addition — not in plan §7's list)
# ---------------------------------------------------------------------------


def test_decline_gate_applies_to_an_already_bound_thread(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The channel-policy decline check in `handle_inbound` runs BEFORE the
    binding-dispatch branch, for every inbound message — so a sender whose
    access is revoked after their thread is already bound and active gets
    declined on that SAME thread too, not only on a hypothetical new one.
    `ServerChannel.enabled` is documented as an absolute kill switch, and an
    already-open thread that kept answering after access was pulled would
    make that documentation false.

    Three ways access is revoked, with two different OBSERVABLE shapes,
    because the webhook resolves an ENABLED channel by token before anything
    else runs (see the kill-switch test above for the same nuance):
    disabling the whole channel 404s at channel resolution, one step before
    the policy gate this test is otherwise about; withdrawing a grant, or
    switching the user's own toggle off, both leave the channel resolvable
    and are declined by the actual policy gate with `REPLY_DENIED`. All three
    are genuine revocations, and the point holds for all three: having an
    active binding does not let a revoked sender keep talking.
    """
    signer = GoogleChatJWTSigner()

    def _new_bound_thread(**channel_overrides) -> tuple[dict, dict, dict, dict, str]:
        consumer, headers = make_user_and_headers(client)
        promote_to_developer(client, superuser_token_headers, consumer["id"])
        agent = create_agent_via_api(client, headers, name=f"Bound-{random_lower_string()[:6]}")
        drain_tasks()
        set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
        channel = create_server_channel(
            client, superuser_token_headers, email_whitelist="*", **channel_overrides
        )
        thread_key = f"spaces/AAA/threads/{random_lower_string()}"
        event = build_message_event(
            thread_key=thread_key, text="please help", sender_email=consumer["email"]
        )
        resp, _ = post_channel_message(client, channel, signer, event)
        assert resp.status_code == 200
        sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
        assert len(sessions) == 1, "setup: the first message should have bound and routed"
        return consumer, headers, agent, channel, thread_key

    def _next_message(channel: dict, consumer: dict, thread_key: str):
        event = build_message_event(
            thread_key=thread_key, text="are you still there?", sender_email=consumer["email"]
        )
        return post_channel_message(client, channel, signer, event)

    # ---- (a) the admin disables the whole channel ------------------------
    consumer_a, _headers_a, _agent_a, channel_a, thread_a = _new_bound_thread()
    update_server_channel(client, superuser_token_headers, channel_a["id"], enabled=False)
    resp_a, _ = _next_message(channel_a, consumer_a, thread_a)
    assert resp_a.status_code == 404

    # ---- (b) the admin withdraws a restricted channel's grant -------------
    consumer_b, _headers_b, _agent_b, channel_b, thread_b = _new_bound_thread()
    update_server_channel(client, superuser_token_headers, channel_b["id"], visibility="restricted")
    replace_channel_grants(client, superuser_token_headers, channel_b["id"], [consumer_b["id"]])
    replace_channel_grants(client, superuser_token_headers, channel_b["id"], [])
    resp_b, _ = _next_message(channel_b, consumer_b, thread_b)
    assert resp_b.status_code == 200
    assert resp_b.json().get("text") == _REPLY_DENIED, resp_b.json()

    # ---- (c) the user switches the channel off for themselves -------------
    consumer_c, headers_c, _agent_c, channel_c, thread_c = _new_bound_thread()
    update_my_channel(client, headers_c, channel_c["id"], is_enabled=False)
    resp_c, _ = _next_message(channel_c, consumer_c, thread_c)
    assert resp_c.status_code == 200
    assert resp_c.json().get("text") == _REPLY_DENIED, resp_c.json()
