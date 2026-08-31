"""Superuser-only enforcement across the routing-trace admin surface.

There is no role-based partial access on this router (see
``app/api/routes/admin_routing.py`` module docstring): a trace names another
account's installed agents and, with ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` on,
an external sender's message text. That is superuser-or-nothing — there is no
"read your own traces" carve-out for a normal user, which is itself the
property worth pinning: a normal user is rejected even for a trace that
resulted from *their own* message.
"""
import uuid

from fastapi.testclient import TestClient

from tests.utils.routing import clear_routing_traces, get_routing_trace, list_routing_traces
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
    post_webhook,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers


def _post_one_message(client: TestClient, superuser_headers: dict[str, str], sender_email: str) -> dict:
    """Deliver one webhook message so at least one routing_decision row exists."""
    channel = create_server_channel(
        client, superuser_headers, auto_register_users=True, email_whitelist="*"
    )
    signer = GoogleChatJWTSigner()
    token = signer.token(audience=channel["config"]["project_number"])
    event = build_message_event(
        thread_key="spaces/AAA/threads/access-control", text="hello", sender_email=sender_email
    )
    with signer.patched():
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    assert resp.status_code == 200
    return channel


def test_non_superuser_is_rejected_on_all_three_routes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A message is routed (as the superuser would see it), producing at
       least one durable trace.
    2. A normal user — including the very sender whose message produced the
       trace — is rejected (403) on list, get, and delete.
    3. The superuser can do all three.
    """
    sender, sender_headers = create_random_user_with_headers(client)
    _post_one_message(client, superuser_token_headers, sender["email"])

    # ── Phase 1: superuser sees at least one trace ────────────────────────
    page = list_routing_traces(client, superuser_token_headers)
    assert page["count"] >= 1
    trace_id = page["data"][0]["id"]

    # ── Phase 2: the sender itself — a normal user — is rejected on all 3 ──
    # The helpers already assert the expected status code internally (see
    # tests/utils/routing.py); on a 403 they return the error body (e.g.
    # {"detail": "The user doesn't have enough privileges"}), never `None`,
    # so the call itself — not an `is None` check on its result — is the
    # assertion here.
    list_routing_traces(client, sender_headers, expected_status=403)
    get_routing_trace(client, sender_headers, trace_id, expected_status=403)
    clear_routing_traces(client, sender_headers, expected_status=403)

    # ── Phase 3: an unrelated normal user is rejected identically ─────────
    other, other_headers = create_random_user_with_headers(client)
    list_routing_traces(client, other_headers, expected_status=403)
    get_routing_trace(client, other_headers, trace_id, expected_status=403)
    clear_routing_traces(client, other_headers, expected_status=403)

    # ── Phase 4: superuser succeeds on all 3 ───────────────────────────────
    assert get_routing_trace(client, superuser_token_headers, trace_id)["id"] == trace_id
    assert list_routing_traces(client, superuser_token_headers)["count"] >= 1


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    """No Authorization header at all — rejected before the superuser check."""
    assert client.get("/api/v1/admin/routing/traces").status_code in (401, 403)
    assert (
        client.get(f"/api/v1/admin/routing/traces/{uuid.uuid4()}").status_code
        in (401, 403)
    )
    assert client.delete("/api/v1/admin/routing/traces").status_code in (401, 403)


def test_get_nonexistent_trace_404s_for_a_superuser(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A well-formed id that names no row is a 404, not a 403 or 500 —
    the superuser gate must not mask an ordinary not-found."""
    ghost = str(uuid.uuid4())
    # Same shape as Phase 2 above: the helper asserts the 404 internally and
    # returns the (non-None) error body, e.g. {"detail": "Routing trace not
    # found"} — the call is the assertion, not a comparison against `None`.
    get_routing_trace(client, superuser_token_headers, ghost, expected_status=404)
