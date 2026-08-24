"""`outcome="error"` is reachable end-to-end (dev fact #3), plus the fold
defect the Phase 2 review found (W1) while merging two error-adjacent passes
into one row.

Both `_route_installed_in_thread` and `_route_catalog_in_thread` wrap their
capture in `except: persist(...); raise` (see
`channel_inbound_service.py`). This module proves three properties:

  1. An exception that escapes Pass 2's classify call is recorded as
     `outcome="error"`, the webhook still answers 200, and the caller still
     gets the generic setup-failed reply — the exception "propagates intact"
     through the pipeline's own outer handler rather than 500ing or vanishing.
  2. The symmetric case for Pass 1's own thread target — reached by forcing
     `_route_installed` itself to raise, since a `route_message` exception is
     already caught *inside* `_route_installed`
     (`server_channels_routing_test.py::test_pass1_ownership_filter_swallows_a_router_exception`)
     and never reaches the thread-target boundary.
  3. W1 (Phase 2 review): Pass 1's OWN router call fails (a real outage,
     caught inside `_route_installed`, which records the error directly on
     `pass1_trace` without raising), and Pass 2 then finds a real match. The
     merged row must keep Pass 2's positive verdict — a decision that
     ultimately succeeded should not be mislabeled `error` — but must NOT
     silently drop Pass 1's failure text just because Pass 2 recovered.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.routing import get_routing_trace, list_routing_traces, post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
)
from tests.utils.user import promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_ROUTE_MESSAGE_TARGET = "app.services.app_mcp.app_mcp_routing_service.AppMCPRoutingService.route_message"


# The persisted `error` carries the exception's TYPE, not its message:
# `RoutingTrace.record_error` goes through `describe_exception`, because a
# provider SDK exception's message routinely echoes the request payload back —
# and at the router's call site that payload is the rendered classifier prompt
# containing the sender's words. These three subclasses give each injected
# failure a distinct *type name*, so every test below can still prove the error
# in the row came from its own call site, and can additionally prove the
# message body did NOT survive.


class _Pass2ClassifierBoom(RuntimeError):
    pass


class _Pass1ThreadBoom(RuntimeError):
    pass


class _Pass1RouterOutage(RuntimeError):
    pass


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


def _only_reply_texts(send_mock) -> list[str]:
    return [c.args[-1] for c in send_mock.await_args_list]


def test_pass2_unhandled_exception_persists_error_outcome_without_500ing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt="Handle anything at all", name_prefix="Pass2Err"
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])
    resp, send_mock = post_channel_message(
        client, channel, signer, event,
        classify_side_effect=_Pass2ClassifierBoom("pass2-classifier-blew-up"),
    )

    # ── The webhook itself must never 500 on a routing-internal exception ──
    assert resp.status_code == 200
    assert any("setting up your assistant failed" in t for t in _only_reply_texts(send_mock)), send_mock

    # ── A row landed, with outcome=error and the failure identifiable ──
    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page["data"]
    row = page["data"][0]
    assert row["outcome"] == "error"
    assert row["error"] is not None
    assert "_Pass2ClassifierBoom" in row["error"]
    # ...and the exception's message is deliberately absent — see the note at
    # the top of this module. This half is the de-tainting, pinned.
    assert "pass2-classifier-blew-up" not in row["error"]

    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    assert {s["stage"] for s in detail["stages"]} == {"pass_1", "pass_2"}


def test_pass1_thread_level_exception_persists_error_outcome_without_500ing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Forces `_route_installed` itself to raise — the only way to reach
    `_route_installed_in_thread`'s own `except: ... persist(...); raise`,
    since a `route_message` exception is already swallowed one level down."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="hello", sender_email="pass1err@example.com")
    resp, send_mock = post_channel_message(
        client, channel, signer, event,
        route_installed_side_effect=_Pass1ThreadBoom("pass1-thread-blew-up"),
    )

    assert resp.status_code == 200
    assert any("setting up your assistant failed" in t for t in _only_reply_texts(send_mock)), send_mock

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page["data"]
    row = page["data"][0]
    assert row["outcome"] == "error"
    assert row["error"] is not None
    assert "_Pass1ThreadBoom" in row["error"]
    assert "pass1-thread-blew-up" not in row["error"]

    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    # Pass 2 never ran — the exception escaped before Pass 1 even returned a
    # (None) result to weigh against it.
    assert [s["stage"] for s in detail["stages"]] == ["pass_1"]


def test_pass1_router_outage_error_is_not_dropped_when_pass2_then_finds_a_match(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """W1 (Phase 2 review): Pass 1's router call fails and is recorded on
    `pass1_trace` (caught *inside* `_route_installed`, which returns `None`
    rather than raising); Pass 2 then genuinely matches a catalog bundle.

    The merged row must keep Pass 2's positive outcome (`parked_install`) —
    but must still carry Pass 1's failure text forward. Silently dropping it
    would mean a real provider outage leaves no trace at all once a later
    pass happens to recover, which defeats the point of a routing trace.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    bundle = _publish_public_bundle(
        client, publisher_headers, trigger_prompt="Handle recovery requests", name_prefix="Recovers"
    )
    add_auto_install_bundle(client, superuser_token_headers, bundle["bundle_uuid"])

    classify_result = type("ClassifyResult", (), {"agent_id": bundle["bundle_uuid"]})()

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="please help", sender_email=consumer["email"])
    from unittest.mock import patch

    with patch(_ROUTE_MESSAGE_TARGET, side_effect=_Pass1RouterOutage("pass1-router-outage-marker")):
        resp, _ = post_channel_message(
            client, channel, signer, event, classify_result=classify_result
        )
    assert resp.status_code == 200

    page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
    assert page["count"] == 1, page["data"]
    row = page["data"][0]

    # Positive verdict wins — Pass 2 genuinely found something.
    assert row["outcome"] == "parked_install"
    assert row["selected_bundle_uuid"] == bundle["bundle_uuid"]

    # But Pass 1's failure must not have been silently dropped.
    assert row["error"] is not None, (
        "Pass 1's router-outage error was dropped once Pass 2 recovered — "
        "see routing_trace_service.py persist()'s handling of `preceded_by.error`."
    )
    # Still attributable to Pass 1's router call — by the stage prefix `persist`
    # adds and by the exception's own type name — without carrying its message.
    assert "pass_1: _Pass1RouterOutage" in row["error"]
    assert "pass1-router-outage-marker" not in row["error"]

    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    assert {s["stage"] for s in detail["stages"]} == {"pass_1", "pass_2"}
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0


def test_pass1_agent_vanishing_before_bind_persists_error_not_a_phantom_routed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A Pass-1 selection that no longer resolves must not persist as `routed`.

    Found in review of the Phase 3 `decide()` split, and introduced by it. Pass
    2 is now gated on Pass 1 returning no agent, so when Pass 1 *does* select an
    agent that has vanished by the time `_route_new_thread` re-resolves it, no
    Pass-2 trace exists to supply a verdict — and `pass1_trace` is already
    settled `routed`.

    **What the pre-fix behaviour actually was is worse than it looks, and it
    was established by running it rather than by reading it.** The obvious
    reading is a durable row saying `outcome=routed` with a `selected_agent_id`
    pointing at a deleted agent — bad enough, since it is invisible to
    `?outcome=error` and to `?outcome=no_match` alike while contradicting both
    the reply the sender got and the live debug feed.

    That row cannot exist. `routing_decision.selected_agent_id` carries a
    foreign key to `agent.id`, so the INSERT violates it, and
    `RoutingTraceService.persist` is never-raises by contract: it logs and
    returns `None`. So the real consequence was that the decision left **no
    durable record at all**, and `_decision_detail(None)` then withheld the
    `trace_id` from the live feed too — the debugging aid silently losing the
    single decision it most needed to explain, which §12 names as the worst
    failure mode a debugging aid has.

    Hence the assertion that matters here is as much `count == 1` (a row exists)
    as it is `outcome == "error"`. The fix is the mirror of the bundle-vanished
    branch, which had always recorded the error.

    Driven by patching `decide` to return a selection for an agent id that was
    never persisted: the real race (a delete landing between the worker thread's
    `db.get` and the caller's) cannot be provoked through the API, and the
    branch under test is reached by the *unresolvable id*, not by how it became
    unresolvable.
    """
    import uuid as _uuid
    from unittest.mock import AsyncMock, patch

    from app.services.routing import routing_trace
    from app.services.routing.routing_trace import RoutingTrace
    from app.services.server_channels.channel_routing_service import (
        RoutingDecisionResult,
    )

    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    ghost_agent_id = _uuid.uuid4()

    def _decision_naming_a_ghost(**kwargs):
        """A real recorder settled exactly as a successful Pass 1 settles it."""
        with RoutingTrace.capture(
            origin=kwargs.get("origin", routing_trace.ORIGIN_SERVER_CHANNEL),
            user_id=kwargs.get("user_id"),
            channel_id=kwargs.get("channel_id"),
            thread_key=kwargs.get("thread_key"),
            message=kwargs.get("text"),
            stage=routing_trace.STAGE_PASS_1,
        ) as trace:
            trace.record_outcome(
                routing_trace.OUTCOME_ROUTED, selected_agent_id=ghost_agent_id
            )
        return RoutingDecisionResult(agent_id=ghost_agent_id, pass1_trace=trace)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key, text="hello", sender_email="ghost@example.com"
    )
    with patch(
        "app.services.server_channels.channel_routing_service."
        "ChannelRoutingService.decide",
        new=AsyncMock(side_effect=lambda **kw: _decision_naming_a_ghost(**kw)),
    ):
        resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    page = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )
    assert page["count"] == 1, page["data"]
    row = page["data"][0]

    # The verdict is the failure it was, and the stale selection is gone —
    # `record_error` clears it through the same settler the bundle branch uses.
    assert row["outcome"] == "error", row
    assert row["error"] is not None
    assert str(ghost_agent_id) in row["error"]
    assert row["selected_agent_id"] is None, (
        "a non-routed row must not still name the agent it failed to bind"
    )

    # And it is reachable from the filter that exists to find exactly this.
    errors = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"], outcome="error"
    )
    assert {r["id"] for r in errors["data"]} == {row["id"]}
