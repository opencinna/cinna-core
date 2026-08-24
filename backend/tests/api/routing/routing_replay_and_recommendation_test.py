"""Replay and the recommendation draft — the other two Phase 3 routes.

Replay re-runs a stored decision's message against *current* state and diffs
the two; the recommendation draft turns a failed decision into wording its
agent's owner can apply. Both share simulate's conditions (superuser-only,
rate-limited, LLM-spending) and both are read-only with respect to agents.

Like the simulate suite next door, the absence assertions here are asserted
against durable state — the original trace still reading as it did, the
candidate's trigger prompt unchanged through the API — never against a log.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.mfa import find_security_events
from tests.utils.routing import (
    STUB_TRIGGER_DRAFT,
    draft_routing_recommendation,
    get_routing_trace,
    patched_routing_externals,
    patched_trigger_prompt_draft,
    post_channel_message,
    replay_routing_trace,
    simulate_routing,
)
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR


def _channel(client, superuser_headers) -> dict:
    return create_server_channel(
        client, superuser_headers, auto_register_users=True, email_whitelist="*"
    )


def _user_with_agent(client, superuser_headers) -> tuple[dict, dict, dict]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Rep-{random_lower_string()[:6]}")
    drain_tasks()
    return user, headers, agent


def _delivered_no_match_trace(client, superuser_headers, channel) -> dict:
    """One real webhook delivery from an unknown sender -> a `no_match` trace."""
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text="please compute the eigenvalues",
        sender_email=f"{random_lower_string()}@example.com",
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    from tests.utils.routing import list_routing_traces

    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1, page["data"]
    return page["data"][0]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_reruns_the_stored_message_and_reports_no_change(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Replaying an unchanged system must say so *out loud*. "The change I made
    did not fix it" is a real answer, and an empty diff panel would read as
    though the replay had not run at all — §11a Rule 1 on a read surface.

    The original trace is also asserted unchanged: a replay writes a NEW row,
    it never edits the one it re-ran.
    """
    channel = _channel(client, superuser_token_headers)
    original_row = _delivered_no_match_trace(client, superuser_token_headers, channel)
    original_before = get_routing_trace(
        client, superuser_token_headers, original_row["id"]
    )

    with patched_routing_externals():
        result = replay_routing_trace(
            client, superuser_token_headers, original_row["id"]
        )

    assert result["replay"]["id"] != original_row["id"], "replay overwrote the original"
    assert result["replay"]["origin"] == "simulate"
    assert result["replay"]["actor_user_id"] is not None
    assert result["replay"]["outcome"] == original_row["outcome"]

    diff = result["diff"]
    assert diff["changed"] is False
    assert "Nothing changed" in diff["summary"]
    assert diff["original_outcome"] == diff["replay_outcome"] == "no_match"

    assert (
        get_routing_trace(client, superuser_token_headers, original_row["id"])
        == original_before
    ), "replay mutated the trace it re-ran"


def test_replay_reports_the_change_after_a_route_is_added(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The tuning loop's payoff: a message that found nothing, replayed after its
    sender gains a route, now routes — and the diff names the outcome flip and
    the candidate that appeared.
    """
    channel = _channel(client, superuser_token_headers)
    user, headers, agent = _user_with_agent(client, superuser_token_headers)

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please compute the eigenvalues",
        sender_email=user["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    from tests.utils.routing import list_routing_traces

    original = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )["data"][0]
    assert original["outcome"] == "no_match", original

    # The fix an admin would ask the owner to make.
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle maths")

    with patched_routing_externals():
        result = replay_routing_trace(client, superuser_token_headers, original["id"])

    diff = result["diff"]
    assert diff["changed"] is True
    assert diff["outcome_changed"] is True
    assert diff["original_outcome"] == "no_match"
    assert diff["replay_outcome"] == "routed"
    assert diff["selection_changed"] is True
    assert diff["original_selection"] is None
    assert agent["name"] in (diff["replay_selection"] or "")
    assert agent["name"] in diff["candidates_added"], diff
    assert result["replay"]["selected_agent_id"] == agent["id"]


def test_replay_is_refused_when_the_message_text_gate_is_off(
    client: TestClient, superuser_token_headers: dict[str, str], monkeypatch
) -> None:
    """
    Replay needs the original message. With ``ROUTING_TRACE_STORE_MESSAGE_TEXT``
    off it is refused rather than run — the flag means "stop showing me this
    text", and re-running it to make a fresh trace is not honouring that. The
    refusal names the flag and points at simulate, so an admin is not left
    guessing.
    """
    channel = _channel(client, superuser_token_headers)
    original = _delivered_no_match_trace(client, superuser_token_headers, channel)

    monkeypatch.setattr(settings, "ROUTING_TRACE_STORE_MESSAGE_TEXT", False)
    r = client.post(
        f"{API}/admin/routing/traces/{original['id']}/replay",
        headers=superuser_token_headers,
        json={"include_catalog": True},
    )
    assert r.status_code == 409, r.text
    assert "ROUTING_TRACE_STORE_MESSAGE_TEXT" in r.json()["detail"]
    assert "simulate" in r.json()["detail"]


def test_replay_is_superuser_only_and_audited(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Same conditions as simulate: it decides against another account's live
    state and spends an LLM call, so it is superuser-only and leaves an audit
    row naming the acting admin, the target, and which mode it was."""
    channel = _channel(client, superuser_token_headers)
    user, headers, _ = _user_with_agent(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=user["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    from tests.utils.routing import list_routing_traces

    original = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )["data"][0]

    replay_routing_trace(client, headers, original["id"], expected_status=403)

    with patched_routing_externals():
        replay_routing_trace(client, superuser_token_headers, original["id"])

    events = find_security_events(
        client, superuser_token_headers, "ROUTING_SIMULATE_RUN"
    )
    assert len(events) == 1, events
    details = events[0]["details"]
    assert details["mode"] == "replay"
    assert details["source_trace_id"] == original["id"]
    assert details["target_user_id"] == user["id"]
    assert "hello" not in str(details), details


def test_replay_of_an_unknown_trace_is_404(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    replay_routing_trace(
        client, superuser_token_headers, str(uuid.uuid4()), expected_status=404
    )


# ---------------------------------------------------------------------------
# Recommendation draft
# ---------------------------------------------------------------------------


def test_recommendation_drafts_for_a_candidate_and_changes_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The advisory boundary: a draft comes back, and the agent's actual trigger
    prompt is untouched. Asserted by reading the agent back through its own
    API — the durable state, not a log line saying we did not write.
    """
    channel = _channel(client, superuser_token_headers)
    user, headers, agent = _user_with_agent(client, superuser_token_headers)
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle invoices")

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please compute the eigenvalues",
        sender_email=user["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    from tests.utils.routing import list_routing_traces

    trace = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )["data"][0]

    before = client.get(f"{API}/agents/{agent['id']}", headers=headers).json()

    with patched_trigger_prompt_draft() as generator:
        draft = draft_routing_recommendation(
            client, superuser_token_headers, trace["id"], ref_id=agent["id"]
        )
    assert draft["trace_id"] == trace["id"]
    assert draft["success"] is True
    assert draft["suggested_trigger_prompt"] == STUB_TRIGGER_DRAFT
    # The brief handed to the generator carries the message that failed to
    # route AND the candidate's current configuration — that pairing is the
    # whole reason this beats re-running the generator on the description.
    brief = generator.call_args.kwargs["description"]
    assert "please compute the eigenvalues" in brief
    assert "Handle invoices" in brief
    assert draft["ref_id"] == agent["id"]
    assert draft["kind"] == "agent"
    assert draft["current_trigger_prompt"] == "Handle invoices"
    # The advisory notice is server-authored and always present, whether or not
    # the generator succeeded — it is what stops the draft reading as a change.
    assert "never edits" in draft["notice"]

    after = client.get(f"{API}/agents/{agent['id']}", headers=headers).json()
    assert after == before, "the recommendation draft modified the agent"


def test_recommendation_refuses_a_candidate_that_is_not_in_the_trace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Scoped to this trace's candidates on purpose. Unrestricted, the route
    would be a general 'draft a trigger prompt for any agent id' oracle that
    happens to hang off a diagnostics endpoint."""
    channel = _channel(client, superuser_token_headers)
    user, headers, agent = _user_with_agent(client, superuser_token_headers)
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle invoices")

    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=user["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    from tests.utils.routing import list_routing_traces

    trace = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )["data"][0]

    draft_routing_recommendation(
        client,
        superuser_token_headers,
        trace["id"],
        ref_id=str(uuid.uuid4()),
        expected_status=404,
    )


def test_recommendation_refuses_a_trace_with_no_candidates(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A trace that considered nothing has no subject to draft for, and saying
    so names the real problem: the expected agent was never a candidate."""
    channel = _channel(client, superuser_token_headers)
    trace = _delivered_no_match_trace(client, superuser_token_headers, channel)
    r = client.post(
        f"{API}/admin/routing/traces/{trace['id']}/recommendation",
        headers=superuser_token_headers,
        json={"ref_id": None},
    )
    assert r.status_code == 409, r.text
    assert "never a routing candidate" in r.json()["detail"]


def test_recommendation_is_superuser_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    trace = _delivered_no_match_trace(client, superuser_token_headers, channel)
    _, headers = create_random_user_with_headers(client)
    draft_routing_recommendation(
        client, headers, trace["id"], expected_status=403
    )


def test_recommendation_writes_no_security_event(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """"Writes nothing" is meant literally. The draft exposes nothing the
    caller did not already have from GET /traces/{id}, so unlike simulate and
    replay it leaves no audit row — and that is easier to keep true when the
    route genuinely writes nothing at all."""
    channel = _channel(client, superuser_token_headers)
    user, headers, agent = _user_with_agent(client, superuser_token_headers)
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle invoices")
    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello",
        sender_email=user["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    from tests.utils.routing import list_routing_traces

    trace = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )["data"][0]

    traces_before = list_routing_traces(client, superuser_token_headers)["count"]
    with patched_trigger_prompt_draft():
        draft_routing_recommendation(
            client, superuser_token_headers, trace["id"], ref_id=agent["id"]
        )

    assert (
        find_security_events(client, superuser_token_headers, "ROUTING_SIMULATE_RUN")
        == []
    )
    assert list_routing_traces(client, superuser_token_headers)["count"] == traces_before


def test_simulate_replay_and_recommendation_share_one_rate_limit_bucket(
    client: TestClient, superuser_token_headers: dict[str, str], monkeypatch
) -> None:
    """One per-admin budget across all three LLM-spending routes. Separate
    buckets would let one admin spend three times the configured limit by
    rotating between them."""
    from app.api.routes import admin_routing
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(settings, "ROUTING_SIMULATE_RATE_LIMIT_PER_MIN", 2)
    monkeypatch.setattr(admin_routing, "_simulate_rate_limiter", RateLimiter())

    channel = _channel(client, superuser_token_headers)
    trace = _delivered_no_match_trace(client, superuser_token_headers, channel)
    user, _, _ = _user_with_agent(client, superuser_token_headers)

    with patched_routing_externals():
        # One simulate + one replay exhausts a budget of two...
        simulate_routing(
            client, superuser_token_headers, message="hi", as_user_id=user["id"]
        )
        replay_routing_trace(client, superuser_token_headers, trace["id"])
        # ...so the third call, on the third route, is throttled.
        stack = patched_trigger_prompt_draft()
        stack.__enter__()
        r = client.post(
            f"{API}/admin/routing/traces/{trace['id']}/recommendation",
            headers=superuser_token_headers,
            json={"ref_id": None},
        )
        stack.__exit__(None, None, None)
    assert r.status_code == 429, r.text
