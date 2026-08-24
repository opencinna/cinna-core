"""List + get-by-id, and the single-row-per-message property (fact #1).

Channel routing is the only wired producer of routing traces today (see the
domain README): `origin` is always `server_channel` through this path. A
webhook delivery runs Pass 1 (installed agents) then, only if Pass 1 misses,
Pass 2 (auto-install catalog) — and however many passes actually ran, one
inbound message now writes exactly ONE `routing_decision` row. Pass 1's
stages fold into Pass 2's row (`persist(pass2_trace, preceded_by=pass1_trace)`
— `persist` owns its own session now and takes no `db` argument, see its
docstring) rather than being written as their own `no_match` row first — the
plan called for two rows; the shipped behavior folds them into one, and that
is what these tests assert against.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle, publish_bundle_and_make_public
from tests.utils.routing import list_routing_traces, get_routing_trace, post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=True, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _publish_public_bundle(client, publisher_headers, *, trigger_prompt, name_prefix) -> dict:
    agent = create_agent_via_api(client, publisher_headers, name=f"{name_prefix}-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": trigger_prompt},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    bundle_uuid = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()["bundle_uuid"]
    return {"bundle_uuid": bundle_uuid, "agent_id": agent["id"]}


def test_pass1_match_produces_one_summarized_and_detailed_trace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Sender has exactly one personal route -> Pass 1's `only_one` path.
    2. Exactly one routing_decision row for the channel, outcome=routed.
    3. The list summary carries the resolved names + candidate counts.
    4. The full detail carries one `pass_1` stage naming the chosen agent as
       an eligible candidate.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Pass1-{random_lower_string()[:6]}")
    drain_tasks()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="hello there", sender_email=user["email"])
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    # ── Phase 1: exactly one row for this channel ─────────────────────────
    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1
    row = page["data"][0]
    assert row["origin"] == "server_channel"
    assert row["outcome"] == "routed"
    assert row["match_method"] == "only_one"
    assert row["selected_agent_id"] == agent["id"]
    assert row["selected_agent_name"] == agent["name"]
    assert row["user_id"] == user["id"]
    assert row["user_email"] == user["email"]
    assert row["candidate_count"] >= 1
    assert row["skipped_count"] == 0

    # ── Phase 2: full detail has one pass_1 stage naming the agent ────────
    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    assert detail["id"] == row["id"]
    stages = detail["stages"]
    assert [s["stage"] for s in stages] == ["pass_1"]
    candidates = stages[0]["candidates"]
    assert any(
        c["ref_id"] == agent["id"] and c["eligible"] is True for c in candidates
    ), candidates


def test_pass1_miss_then_pass2_hit_writes_a_single_merged_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Fact #1: Pass 1 misses (no personal route), Pass 2 classifies a catalog
    bundle. This must land as ONE row (not a `no_match` row from Pass 1 plus
    a `parked_install` row from Pass 2) whose stages carry BOTH passes, and
    whose skip_reason on the excluded candidate survives into the merged row.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    consumer, _ = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])

    # Eligible candidate.
    public_bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt="Handle public requests", name_prefix="Public"
    )
    add_auto_install_bundle(client, superuser_token_headers, public_bundle["bundle_uuid"])

    # NOT public -> on the list but never a candidate; skip_reason=not_installable.
    private_agent = create_agent_via_api(client, publisher_headers, name=f"Private-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{private_agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle private requests"},
    )
    assert r.status_code == 200, r.text
    private_revision = publish_bundle(client, publisher_headers, private_agent["id"])
    add_auto_install_bundle(client, superuser_token_headers, private_revision["bundle_uuid"])

    classify_result = type(
        "ClassifyResult", (), {"agent_id": public_bundle["bundle_uuid"]}
    )()

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])
    resp, _ = post_channel_message(
        client, channel, signer, event, classify_result=classify_result
    )
    assert resp.status_code == 200

    # ── Phase 1: exactly ONE row for this channel ─────────────────────────
    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page["data"]
    row = page["data"][0]
    assert row["outcome"] == "parked_install"
    assert row["match_method"] == "ai"
    assert row["selected_bundle_uuid"] == public_bundle["bundle_uuid"]

    # ── Phase 2: both passes are present in the merged stages ─────────────
    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    stage_names = [s["stage"] for s in detail["stages"]]
    assert "pass_1" in stage_names
    assert "pass_2" in stage_names

    pass2 = next(s for s in detail["stages"] if s["stage"] == "pass_2")
    skipped = [c for c in pass2["candidates"] if not c["eligible"]]
    assert any(
        c["ref_id"] == private_revision["bundle_uuid"] and c["skip_reason"] == "not_installable"
        for c in skipped
    ), pass2["candidates"]
    chosen = [c for c in pass2["candidates"] if c["eligible"]]
    assert any(c["ref_id"] == public_bundle["bundle_uuid"] for c in chosen)


def test_no_match_on_both_passes_still_writes_one_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Neither pass finds anything -> one `no_match` row, both stages present."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key, text="anybody home?", sender_email=f"nomatch-{random_lower_string()[:8]}@example.com"
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1
    row = page["data"][0]
    assert row["outcome"] == "no_match"
    assert row["selected_agent_id"] is None
    assert row["selected_bundle_uuid"] is None

    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    assert {s["stage"] for s in detail["stages"]} == {"pass_1", "pass_2"}


def test_list_filters_by_origin_and_outcome_and_unknown_values_are_empty_not_422(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The `origin`/`outcome` filters are free-form strings (see the route's own
    docstring): a value this build has never heard of must come back as an
    empty page, not a validation error.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="hi", sender_email="x@example.com")
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    # Real filter values match.
    assert list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"], origin="server_channel"
    )["count"] == 1
    assert list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"], outcome="no_match"
    )["count"] == 1

    # Unknown vocabulary -> empty page, 200. Checked field-by-field rather
    # than by dict equality: the response also carries a `notice` (S6 —
    # `None` here since ROUTING_TRACE_ENABLED is on for this test), and
    # pinning the whole dict shape would make this test brittle against any
    # future field addition unrelated to what it actually checks.
    empty = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"], origin="not_a_real_origin"
    )
    assert empty["data"] == []
    assert empty["count"] == 0
    empty = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"], outcome="not_a_real_outcome"
    )
    assert empty["data"] == []
    assert empty["count"] == 0


def test_list_filters_by_channel_and_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """channel_id and user_id each partition the feed to just their own rows."""
    channel_a = _channel(client, superuser_token_headers)
    channel_b = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    user_a, _ = create_random_user_with_headers(client)
    user_b, _ = create_random_user_with_headers(client)

    post_channel_message(
        client, channel_a, signer,
        build_message_event(thread_key="spaces/AAA/threads/a", text="hi", sender_email=user_a["email"]),
    )
    post_channel_message(
        client, channel_b, signer,
        build_message_event(thread_key="spaces/AAA/threads/b", text="hi", sender_email=user_b["email"]),
    )

    by_channel = list_routing_traces(client, superuser_token_headers, channel_id=channel_a["id"])
    assert by_channel["count"] == 1
    assert by_channel["data"][0]["channel_id"] == channel_a["id"]

    by_user = list_routing_traces(client, superuser_token_headers, user_id=user_b["id"])
    assert by_user["count"] == 1
    assert by_user["data"][0]["user_id"] == user_b["id"]
    assert by_user["data"][0]["channel_id"] == channel_b["id"]


def test_an_inactive_route_is_recorded_as_a_skip_rather_than_dropped_silently(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`SKIP_ROUTE_INACTIVE` was defined, exported, and never emitted — so a
    candidate dropped for an inactive route vanished without a trace, which is
    exactly what the plan's "single highest-value rule" forbids: a trace listing
    only the finalists cannot diagnose "the expected agent was never a candidate
    at all".

    `get_effective_routes_for_user` used to exclude inactive routes with a SQL
    predicate, before anything could observe them. The predicate now runs in
    Python, at the point the skip is recorded — same query count, no re-fetch on
    the routing hot path.

    Both halves matter, and the first is the one a regression would break
    quietly: an inactive route must STILL not route (it is now merely visible,
    not eligible), and it must now appear in the trace saying why.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Inactive-{random_lower_string()[:6]}")
    drain_tasks()
    create_user_route(
        client, headers, agent["id"], trigger_prompt="Handle anything", is_active=False
    )

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="hello there", sender_email=user["email"])
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1
    row = page["data"][0]
    # Still excluded from routing — the switch is off, and it stays off.
    assert row["outcome"] == "no_match", row
    assert row["selected_agent_id"] is None, row
    # ...but no longer invisible.
    assert row["skipped_count"] >= 1, row

    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    pass1 = next(s for s in detail["stages"] if s["stage"] == "pass_1")
    skipped = [
        c for c in pass1["candidates"] if c["ref_id"] == agent["id"]
    ]
    assert skipped, pass1["candidates"]
    assert skipped[0]["eligible"] is False, skipped
    assert skipped[0]["skip_reason"] == "route_inactive", skipped
    assert skipped[0]["name"] == agent["name"], skipped
