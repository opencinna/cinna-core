"""`detail.trace_id` on the live debug feed is a real key into `routing_decision`
(dev fact #4): `RoutingDecision.id == trace.trace_id`, and the id is published
on the debug feed only when a row was actually written (`_decision_detail`
in `channel_inbound_service.py` returns `{}` for a falsy decision id — no
dead links in the diagnostic panel).
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.utils.routing import get_routing_trace, post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
    list_debug_events,
)
from tests.utils.utils import random_lower_string


def _channel(client, superuser_headers) -> dict:
    return create_server_channel(client, superuser_headers, auto_register_users=True, email_whitelist="*")


def test_debug_feed_trace_id_is_a_real_key_into_the_routing_decision_table(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key, text="anybody home?", sender_email=f"{random_lower_string()}@example.com"
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    feed = list_debug_events(client, superuser_token_headers, channel["id"])
    no_match_events = [e for e in feed["events"] if e["kind"] == "no_match"]
    assert len(no_match_events) == 1, feed["events"]
    trace_id = no_match_events[0]["detail"].get("trace_id")
    assert trace_id, no_match_events[0]

    fetched = get_routing_trace(client, superuser_token_headers, trace_id)
    assert fetched["id"] == trace_id


def test_debug_feed_carries_no_trace_id_when_tracing_is_disabled(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """No dead links: with tracing off, no row is written, so the feed must
    not publish a `trace_id` that `GET /admin/routing/traces/{id}` would 404
    on."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key, text="anybody home?", sender_email=f"{random_lower_string()}@example.com"
    )
    with patch("app.core.config.settings.ROUTING_TRACE_ENABLED", False):
        resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    feed = list_debug_events(client, superuser_token_headers, channel["id"])
    no_match_events = [e for e in feed["events"] if e["kind"] == "no_match"]
    assert len(no_match_events) == 1, feed["events"]
    assert "trace_id" not in no_match_events[0]["detail"], no_match_events[0]
