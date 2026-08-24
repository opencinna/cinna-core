"""`DELETE /admin/routing/traces` (S4): the unscoped form must be asked for by
name, and every clear is audited.

Before this fix, a bare `DELETE /admin/routing/traces` — the shape a client
sends when it forgets a parameter, or an operator types by hand — wiped every
channel's traces for the whole retention window. That is exactly the §11a
Rule 1 shape: the destructive call looked identical to the ordinary one. Now:

  - Neither `channel_id` nor `?all=true` present -> 400, nothing deleted.
  - `channel_id=<id>` -> clears only that channel's rows.
  - `?all=true` (no `channel_id`) -> clears every channel's rows.
  - Every successful clear (scoped or unscoped) writes a `ROUTING_TRACES_CLEARED`
    security event naming the channel (or `None` for unscoped) and the count —
    the erasure path the read-gate notices point operators at has to actually
    be provable as used.

No existing test in this domain exercised any of the above — `clear_routing_traces`
was previously only called expecting a 403 from non-superusers, never a real
delete. This file closes that gap.
"""
from fastapi.testclient import TestClient

from tests.utils.mfa import find_security_events
from tests.utils.routing import clear_routing_traces, list_routing_traces, post_channel_message
from tests.utils.server_channel import GoogleChatJWTSigner, build_message_event, create_server_channel
from tests.utils.utils import random_lower_string


def _channel_with_one_trace(client: TestClient, superuser_headers: dict[str, str]) -> dict:
    """A channel with exactly one (no_match) routing_decision row."""
    channel = create_server_channel(
        client, superuser_headers, auto_register_users=True, email_whitelist="*"
    )
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key, text="hi", sender_email=f"{random_lower_string()}@example.com"
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    return channel


def test_clear_lifecycle_bare_delete_refused_scoped_then_unscoped(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Two channels, one trace each.
    2. A bare DELETE (no channel_id, no all=true) is refused with 400 and
       deletes nothing.
    3. `channel_id=<A>` clears only channel A's row; B's survives.
    4. `?all=true` then clears the rest (channel B), unscoped.
    5. Both successful clears are audited as ROUTING_TRACES_CLEARED, with the
       right channel_id (or None) and deleted_count in the event details.
    """
    channel_a = _channel_with_one_trace(client, superuser_token_headers)
    channel_b = _channel_with_one_trace(client, superuser_token_headers)

    # ── Phase 1: bare DELETE is a 400, nothing is deleted ──────────────────
    clear_routing_traces(client, superuser_token_headers, expected_status=400)
    assert list_routing_traces(client, superuser_token_headers, channel_id=channel_a["id"])["count"] == 1
    assert list_routing_traces(client, superuser_token_headers, channel_id=channel_b["id"])["count"] == 1

    # ── Phase 2: channel-scoped clear only touches that channel ────────────
    result = clear_routing_traces(client, superuser_token_headers, channel_id=channel_a["id"])
    assert result["message"] == "Cleared 1 routing trace(s)"
    assert list_routing_traces(client, superuser_token_headers, channel_id=channel_a["id"])["count"] == 0
    assert list_routing_traces(client, superuser_token_headers, channel_id=channel_b["id"])["count"] == 1

    # ── Phase 3: unscoped clear (?all=true) takes the rest ──────────────────
    result = clear_routing_traces(client, superuser_token_headers, all_channels=True)
    assert result["message"] == "Cleared 1 routing trace(s)"
    assert list_routing_traces(client, superuser_token_headers, channel_id=channel_b["id"])["count"] == 0

    # ── Phase 4: both successful clears were audited ────────────────────────
    scoped_events = find_security_events(client, superuser_token_headers, "ROUTING_TRACES_CLEARED")
    assert len(scoped_events) == 2, scoped_events

    by_channel = {e["details"].get("channel_id"): e["details"] for e in scoped_events}
    assert by_channel[channel_a["id"]]["deleted_count"] == 1
    assert by_channel[None]["deleted_count"] == 1

    # The event payload never carries message text or any per-row content —
    # only the channel and the count, mirroring the admin test-send precedent.
    for details in by_channel.values():
        assert set(details.keys()) == {"channel_id", "deleted_count"}


def test_clear_lifecycle_neither_channel_id_nor_all_true_present_is_400_not_a_wipe(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`all=false` (the default) behaves exactly like omitting it — still 400 —
    so there is no way to accidentally spell "clear everything" other than the
    explicit `all=true`."""
    channel = _channel_with_one_trace(client, superuser_token_headers)

    clear_routing_traces(client, superuser_token_headers, all_channels=False, expected_status=400)
    assert list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])["count"] == 1
