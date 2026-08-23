"""Admin debug panel + email-targeted test sends.

The feature deliberately keeps inbound message text out of the logs and out of
the database, which left an admin configuring a channel with no way to see
what arrived or what the pipeline decided about it. The debug buffer is that
view; these tests pin the two properties that make it trustworthy:

  - It records the *decision*, not just the arrival — a denied sender, a
    verification failure and a routed message are distinguishable in the feed.
  - It is superuser-only, like every other admin surface here, because it
    carries sender identity and message text.

Plus the test-send targeting rework: an email is resolved *locally* to a
thread the platform has already observed. Google Chat's ``users/{email}``
alias is user-authentication only and this adapter authenticates as an app, so
an unseen address has no reachable destination — the test asserts the error
says so instead of surfacing a bare provider 404.
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    clear_debug_events,
    create_server_channel,
    list_debug_events,
    list_recent_senders,
    post_webhook,
    send_test_outbound,
)
from tests.utils.mfa import find_security_events
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_ADMIN_BASE = f"{API}/admin/server-channels"
_SEND_TARGET = (
    "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
)
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"


def _post(client, channel, signer, event):
    """Deliver one verified webhook event and run the background work."""
    token = signer.token(audience=channel["config"]["project_number"])
    with signer.patched(), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="ok")
    ), patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id")) as send_mock:
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp, send_mock


def _kinds(feed: dict) -> list[str]:
    return [e["kind"] for e in feed["events"]]


# ---------------------------------------------------------------------------
# Capture — the pipeline's decision is visible
# ---------------------------------------------------------------------------


def test_denied_sender_shows_both_arrival_and_the_reason_in_the_feed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A sender outside the whitelist is the single most common "why is nothing
    happening" case, and the one with no other observable trace: the webhook
    answers 200 with a polite denial either way. The feed must show that the
    message *arrived and verified*, and separately that the whitelist rejected
    it — collapsing those two into one entry would leave "did Google even call
    us" unanswered.
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(
        client, superuser_token_headers, email_whitelist="*@allowed.com"
    )
    event = build_message_event(
        thread_key="spaces/AAA/threads/x",
        text="hello there",
        sender_email="outsider@blocked.com",
    )
    _post(client, channel, signer, event)

    feed = list_debug_events(client, superuser_token_headers, channel["id"])
    assert feed["buffer_size"] == settings.SERVER_CHANNEL_DEBUG_BUFFER_SIZE
    assert feed["capturing_since"]

    # Newest first: the rejection, then the arrival it followed.
    assert _kinds(feed) == ["rejected", "received"]

    rejected, received = feed["events"]
    assert rejected["detail"]["stage"] == "whitelist"
    assert rejected["sender_email"] == "outsider@blocked.com"
    # The configured whitelist travels with the denial — the admin's next
    # question is always "what is it currently set to".
    assert rejected["detail"]["whitelist"] == "*@allowed.com"

    assert received["direction"] == "inbound"
    assert received["text"] == "hello there"
    assert received["thread_key"] == "spaces/AAA/threads/x"


def test_verification_failure_is_captured_without_any_payload_detail(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A bad signature is recorded — an admin pasting the wrong project number
    sees *something* rather than silence. But nothing from the unverified
    payload may appear on that entry: it failed exactly the check that would
    let us trust any of it.
    """
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    event = build_message_event(
        thread_key="spaces/AAA/threads/x", text="secret text", sender_email="a@b.com"
    )

    resp = post_webhook(
        client,
        channel["webhook_token"],
        event,
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert resp.status_code == 403

    feed = list_debug_events(client, superuser_token_headers, channel["id"])
    assert _kinds(feed) == ["rejected"]
    entry = feed["events"][0]
    assert entry["detail"]["stage"] == "verify"
    assert entry["text"] is None
    assert entry["sender_email"] is None
    assert entry["thread_key"] is None


def test_clear_empties_the_feed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="")
    _post(
        client,
        channel,
        signer,
        build_message_event(thread_key="spaces/AAA/threads/x", text="hi"),
    )
    assert list_debug_events(client, superuser_token_headers, channel["id"])["events"]

    clear_debug_events(client, superuser_token_headers, channel["id"])
    assert (
        list_debug_events(client, superuser_token_headers, channel["id"])["events"] == []
    )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_debug_and_sender_routes_are_superuser_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The feed carries sender identity and message text, so it sits behind the
    same guard as the rest of the admin surface — not merely behind "logged
    in". Checked unauthenticated AND as an ordinary authenticated user, since
    only the second distinguishes a real guard from a missing dependency.
    """
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    _, plain_headers = create_random_user_with_headers(client)

    for method, path in [
        ("get", f"{_ADMIN_BASE}/{channel['id']}/debug-events"),
        ("delete", f"{_ADMIN_BASE}/{channel['id']}/debug-events"),
        ("get", f"{_ADMIN_BASE}/{channel['id']}/recent-senders"),
    ]:
        call = getattr(client, method)
        assert call(path).status_code == 401, path
        assert call(path, headers=plain_headers).status_code == 403, path


# ---------------------------------------------------------------------------
# Test-send targeting
# ---------------------------------------------------------------------------


def test_test_send_requires_exactly_one_target(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Neither target and both targets are equally unanswerable — 422 both."""
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")

    send_test_outbound(
        client,
        superuser_token_headers,
        channel["id"],
        thread_key=None,
        expect_status=422,
    )
    send_test_outbound(
        client,
        superuser_token_headers,
        channel["id"],
        thread_key="spaces/AAA",
        email="someone@example.com",
        expect_status=422,
    )


def test_test_send_to_an_unseen_email_explains_the_limit_and_sends_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    An address the platform has never observed cannot be turned into a
    destination at all. The failure must be an explanation, not a provider
    404 — and the adapter must not be called on the way to finding out.
    """
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")

    with patch(_SEND_TARGET, AsyncMock(return_value="x")) as send_mock:
        result = send_test_outbound(
            client,
            superuser_token_headers,
            channel["id"],
            thread_key=None,
            email="never-seen@example.com",
        )

    assert result["success"] is False
    assert "never-seen@example.com" in result["error"]
    assert "message the app once" in result["error"]
    send_mock.assert_not_called()


def test_a_sender_who_messaged_becomes_reachable_by_email(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The end-to-end point of the feature: someone messages the app, and from
    then on an admin can test-send to them *by email* without ever handling a
    space id. The resolved destination must be the thread that person is
    actually in — the whole complaint was not knowing where a test landed.

    Pass 1 is made deterministic with a single personal app-mcp route, so
    routing takes the ``only_one`` path and needs no LLM.
    """
    signer = GoogleChatJWTSigner()
    thread_key = "spaces/AAA/threads/known"

    # The sender needs a platform account with exactly one routable agent, so
    # Pass 1 takes the `only_one` path (no LLM in the test environment).
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"Debug-{random_lower_string()[:6]}"
    )
    drain_tasks()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")
    sender_email = user["email"]

    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello agent", sender_email=sender_email
        ),
    )

    senders = list_recent_senders(client, superuser_token_headers, channel["id"])
    assert [s["email"] for s in senders] == [sender_email]
    assert senders[0]["thread_key"] == thread_key

    with patch(_SEND_TARGET, AsyncMock(return_value="sent-id")) as send_mock:
        result = send_test_outbound(
            client,
            superuser_token_headers,
            channel["id"],
            thread_key=None,
            email=sender_email,
            text="ping",
        )

    assert result["success"] is True
    # The adapter only ever receives a native thread identity — the email is
    # resolved before it, never handed to the provider.
    assert send_mock.await_args.args[1] == thread_key
    assert send_mock.await_args.args[2] == "ping"

    # And the admin's own send shows up in the feed, attributed as a test.
    feed = list_debug_events(client, superuser_token_headers, channel["id"])
    assert "test_send" in _kinds(feed)


def test_recent_senders_merges_a_bound_thread_with_a_buffer_only_one(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Regression: the merge's own headline case used to 500.

    ``list_recent_senders`` draws ``last_seen`` from two sources with different
    timezone habits — ``channel_thread_binding.updated_at`` is a bare
    ``DateTime`` column and comes back from Postgres naive, while the debug
    buffer stamps ``datetime.now(UTC)``. Sorting one of each raised
    ``TypeError: can't compare offset-naive and offset-aware datetimes``, so the
    picker worked with a single sender and broke the moment it had the mixture
    it exists to produce: someone with a real conversation, plus someone the
    pipeline has only just seen and denied.
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")

    # --- Sender 1: routes through to a durable binding. ---
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"Debug-{random_lower_string()[:6]}"
    )
    drain_tasks()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key="spaces/AAA/threads/bound",
            text="hello agent",
            sender_email=user["email"],
        ),
    )

    # --- Sender 2: no platform account, auto-registration off — denied, so
    # they exist only in the buffer. Exactly who an admin wants to test-send to.
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key="spaces/AAA/threads/unbound",
            text="let me in",
            sender_email="stranger@nowhere.test",
        ),
    )

    senders = list_recent_senders(client, superuser_token_headers, channel["id"])

    by_email = {s["email"]: s for s in senders}
    assert set(by_email) == {user["email"], "stranger@nowhere.test"}
    assert by_email[user["email"]]["bound"] is True
    assert by_email[user["email"]]["thread_key"] == "spaces/AAA/threads/bound"
    assert by_email["stranger@nowhere.test"]["bound"] is False
    assert by_email["stranger@nowhere.test"]["thread_key"] == (
        "spaces/AAA/threads/unbound"
    )


def test_admin_test_send_is_audited_without_the_message_body(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A test send can push arbitrary text into a *named person's* real chat
    thread — the email-targeted form resolves to a thread belonging to an
    identified user. The debug buffer records it, but that is in-memory and a
    superuser can wipe it with one DELETE, so the durable audit row is the
    only account that survives.

    The row must carry the resolved destination and NOT the message body:
    `SecurityEvent` rows are broadly readable, and the audit answers "who sent
    something where", not "what did it say".
    """
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")

    with patch(_SEND_TARGET, AsyncMock(return_value="sent-id")):
        result = send_test_outbound(
            client,
            superuser_token_headers,
            channel["id"],
            thread_key="spaces/AAA/threads/audited",
            text="secret probe body",
        )
    assert result["success"] is True

    events = find_security_events(
        client, superuser_token_headers, "SERVER_CHANNEL_TEST_SEND"
    )
    assert len(events) == 1, "the admin test send must leave exactly one audit row"

    details = events[0]["details"]
    assert details["server_channel_id"] == channel["id"]
    assert details["thread_key"] == "spaces/AAA/threads/audited"
    assert details["targeted_by"] == "thread_key"
    assert "secret probe body" not in str(details)
