"""Webhook edge behavior + whitelist + auto-register — plan §13 checklist.

Covers:
  - Unknown/disabled webhook token -> 404, indistinguishable (no existence
    leak).
  - Ignored event kinds (bot's own messages) ack cleanly with {} — never an
    error, since a non-2xx makes Chat retry forever.
  - ADDED_TO_SPACE -> static welcome reply.
  - Whitelist matrix: pattern forms, fail-closed on empty/null, case
    insensitivity, denial reply.
  - Auto-register on/off; the created user is passwordless, email-confirmed,
    and `agent-user`; repeat contact is idempotent (same user, not a second
    account).
  - Redelivery dedup (same external_message_id twice -> ingested once).
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_added_to_space_event,
    build_ignored_event,
    build_message_attachment,
    build_message_event,
    create_server_channel,
    post_webhook,
    update_server_channel,
)
from tests.utils.routing import classification, enter_classifier_patch
from tests.utils.session import list_sessions
from tests.utils.message import list_messages
from tests.utils.user import create_random_user_with_headers, promote_to_developer, user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"


def _post(client, channel, signer, event, *, stub=None, classify_result=None):
    """One verified delivery. ``classify_result`` is required whenever the
    sender owns an agent that is eligible for channel routing — Pass 1
    classifies over the sender's own agents with no short-circuit."""
    token = signer.token(audience=channel["config"]["project_number"])
    stream_stub = stub or StubAgentEnvConnector(response_text="ok")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stream_stub))
        send_mock = stack.enter_context(
            patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id"))
        )
        enter_classifier_patch(stack, classify_result=classify_result)
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp, send_mock


# ---------------------------------------------------------------------------
# 404 — unknown / disabled token
# ---------------------------------------------------------------------------


def test_unknown_and_disabled_webhook_token_both_404_identically(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Unknown token AND a disabled channel's token must return the SAME 404
    with no distinguishing detail — no existence-leak oracle.
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    token = signer.token(audience=channel["config"]["project_number"])
    event = build_message_event(thread_key="spaces/AAA/threads/x", text="hi")

    r_unknown = post_webhook(client, "totally-made-up-token", event, bearer_token=token)
    assert r_unknown.status_code == 404
    body_unknown = r_unknown.json()

    update_server_channel(client, superuser_token_headers, channel["id"], enabled=False)
    with signer.patched():
        r_disabled = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
    assert r_disabled.status_code == 404
    assert r_disabled.json() == body_unknown  # identical body, no leak


# ---------------------------------------------------------------------------
# Ignored / added_to_space event kinds
# ---------------------------------------------------------------------------


def test_bot_own_message_is_ignored_and_acked_cleanly(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An authentic BOT-sender event is acked ({}), never treated as an error."""
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    resp, send_mock = _post(client, channel, signer, build_ignored_event())
    assert resp.status_code == 200
    assert resp.json() == {}
    send_mock.assert_not_awaited()


def test_added_to_space_gets_static_welcome_reply(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    resp, _ = _post(client, channel, signer, build_added_to_space_event())
    assert resp.status_code == 200
    assert "Hi!" in resp.json().get("text", "")


# ---------------------------------------------------------------------------
# Attachments: the relaxed empty-text branch (channel-message-attachments)
# ---------------------------------------------------------------------------


def test_attachment_only_message_from_a_human_sender_is_no_longer_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``GoogleChatAdapter._parse_event``'s empty-text ignore guard was relaxed
    from ``if not text:`` to ``if not text and not attachments:`` — a
    message that is nothing but a file is a real message and must reach the
    pipeline, not be dropped silently the way a bot echo or a membership
    event is.

    Proven by observing it reach the WHITELIST gate — a real, specific
    ``REPLY_DENIED`` reply, because this channel's whitelist is left unset
    (deny-all) — rather than the "ignored" branch's silent ``{}`` ack. The
    two are distinguishable exactly because "ignored" never even looks at
    the whitelist; ``process_inbound`` acks it before step 4 is reached.
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers)  # no email_whitelist -> deny-all
    event = build_message_event(
        thread_key="spaces/AAA/threads/attachment-only",
        text="",
        attachments=[build_message_attachment()],
    )
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    assert resp.json() != {}, (
        "an attachment-only message was acked as if it were an 'ignored' "
        "event — the empty-text guard regressed back to `if not text:`"
    )
    assert "administrator" in resp.json().get("text", "")
    send_mock.assert_not_awaited()


def test_bot_sender_message_with_an_attachment_is_still_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The bot-sender guard runs BEFORE the attachment check — a security
    property, not an implementation detail: the app's own posts (echoed back
    on some Chat configurations) must never route, whatever they carry. An
    attachment on a bot-sender event must never be what tips it out of
    'ignored'.
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    event = build_message_event(
        thread_key="spaces/AAA/threads/bot-with-attachment",
        text="",
        sender_type="BOT",
        attachments=[build_message_attachment()],
    )
    resp, send_mock = _post(client, channel, signer, event)
    assert resp.status_code == 200
    assert resp.json() == {}
    send_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Whitelist matrix
# ---------------------------------------------------------------------------


def test_whitelist_matrix(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Pattern forms, fail-closed on empty/null, case-insensitivity, denial reply.

      - `None` whitelist -> deny everyone (fail closed).
      - `""` whitelist -> deny everyone (fail closed; the service normalizes
        blank input to NULL on write, but assert the runtime behavior too).
      - `"*"` -> allow any verified sender.
      - `"*@example.com"` -> allows matching domain, denies others.
      - Case-insensitive: `"*@Example.COM"` pattern matches
        `bob@EXAMPLE.com` sender.
      - Multi-pattern comma list: second pattern matches when the first
        doesn't.
    """
    signer = GoogleChatJWTSigner()

    def _try(channel, email) -> str | None:
        event = build_message_event(
            thread_key=f"spaces/AAA/threads/{random_lower_string()}", text="hi", sender_email=email
        )
        resp, _ = _post(client, channel, signer, event)
        assert resp.status_code == 200
        return resp.json().get("text")

    denied_text = (
        "Sorry, you don't have access to this assistant. "
        "Please contact your administrator."
    )

    # auto_register_users=True on every channel below: a whitelist PASS must
    # be distinguishable from a whitelist DENY. With auto-register off, an
    # unknown sender is denied at the user-resolution step with the exact
    # same reply text regardless of the whitelist outcome, which would make
    # this matrix unable to tell "whitelisted" apart from "not".
    channel_none = create_server_channel(
        client, superuser_token_headers, email_whitelist=None, auto_register_users=True
    )
    assert _try(channel_none, "anyone@example.com") == denied_text

    channel_blank = create_server_channel(
        client, superuser_token_headers, email_whitelist="   ", auto_register_users=True
    )
    assert _try(channel_blank, "anyone@example.com") == denied_text

    channel_star = create_server_channel(
        client, superuser_token_headers, email_whitelist="*", auto_register_users=True
    )
    assert _try(channel_star, "anyone@wherever.example") != denied_text

    channel_domain = create_server_channel(
        client, superuser_token_headers, email_whitelist="*@example.com", auto_register_users=True
    )
    assert _try(channel_domain, "alice@example.com") != denied_text
    assert _try(channel_domain, "alice@notexample.com") == denied_text

    channel_case = create_server_channel(
        client, superuser_token_headers, email_whitelist="*@Example.COM", auto_register_users=True
    )
    assert _try(channel_case, "bob@EXAMPLE.com") != denied_text

    channel_multi = create_server_channel(
        client,
        superuser_token_headers,
        email_whitelist="devops.*@corp.example, *@example.com",
        auto_register_users=True,
    )
    assert _try(channel_multi, "someone@example.com") != denied_text
    assert _try(channel_multi, "devops.jane@corp.example") != denied_text
    assert _try(channel_multi, "random@other.example") == denied_text


# ---------------------------------------------------------------------------
# Auto-register
# ---------------------------------------------------------------------------


def test_auto_register_off_denies_unknown_sender(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(
        client, superuser_token_headers, email_whitelist="*", auto_register_users=False
    )
    email = random_email()
    event = build_message_event(thread_key="spaces/AAA/threads/noreg", text="hi", sender_email=email)
    resp, _ = _post(client, channel, signer, event)
    assert resp.status_code == 200
    assert "administrator" in resp.json().get("text", "")

    # No account was created for this address.
    r = client.get(f"{API}/users/search", headers=superuser_token_headers, params={"q": email, "include_self": True})
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_auto_register_on_creates_passwordless_confirmed_agent_user_idempotently(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    First contact with auto_register_users=True creates exactly one account:
    passwordless, email-confirmed, role=agent-user. A second message from the
    same address reuses the same account (idempotent — no duplicate).
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(
        client, superuser_token_headers, email_whitelist="*", auto_register_users=True
    )
    email = random_email()
    event1 = build_message_event(
        thread_key="spaces/AAA/threads/reg1", text="hello there", sender_email=email
    )
    resp1, _ = _post(client, channel, signer, event1)
    assert resp1.status_code == 200

    r = client.get(f"{API}/users/search", headers=superuser_token_headers, params={"q": email, "include_self": True})
    assert r.status_code == 200
    results = r.json()["data"]
    assert len(results) == 1
    user_id = results[0]["id"]

    user_detail = client.get(f"{API}/users/{user_id}", headers=superuser_token_headers).json()
    assert user_detail["has_password"] is False
    assert user_detail["email_confirmed"] is True
    assert user_detail["is_active"] is True

    # The privilege assertion, read from the account AS CREATED — never after a
    # mutation. This account was created by an unauthenticated request from the
    # public internet, so the role it is born with is the security property.
    # Asserting the echo of a PATCH to "agent-user" would pass even if
    # `create_external_user` regressed to minting admins: the PATCH would
    # simply demote it first and the suite would stay green.
    assert user_detail["role"] == "agent-user", (
        f"Auto-registered account was created with role {user_detail['role']!r}; "
        "webhook-triggered signup must never mint a privileged account."
    )
    assert user_detail["is_superuser"] is False

    # Second message, same sender, DIFFERENT thread -> no new account.
    event2 = build_message_event(
        thread_key="spaces/AAA/threads/reg2", text="hello again", sender_email=email
    )
    resp2, _ = _post(client, channel, signer, event2)
    assert resp2.status_code == 200

    r2 = client.get(f"{API}/users/search", headers=superuser_token_headers, params={"q": email, "include_self": True})
    assert len(r2.json()["data"]) == 1
    assert r2.json()["data"][0]["id"] == user_id


# ---------------------------------------------------------------------------
# Redelivery dedup
# ---------------------------------------------------------------------------


def test_redelivery_with_same_external_message_id_is_deduped(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A resumed (active-binding) thread redelivering the exact same
    external_message_id must be ingested exactly once, not twice.
    """
    signer = GoogleChatJWTSigner()
    channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")

    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"DedupAgent-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    message_name = f"spaces/AAA/messages/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text="first delivery",
        sender_email=user["email"],
        message_name=message_name,
    )

    # First delivery: creates the binding + session.
    resp1, _ = _post(client, channel, signer, event)
    assert resp1.status_code == 200
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    user_msgs_after_first = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs_after_first) == 1

    # Second delivery of the SAME event (same external_message_id) — must be
    # acked without a second ingest.
    # No classifier answer named: the redelivery must be deduped before it
    # ever reaches routing, and a classifier that runs here fails at the call.
    resp2, _ = _post(client, channel, signer, event)
    assert resp2.status_code == 200
    assert resp2.json() == {}

    sessions_after = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions_after) == 1
    user_msgs_after_second = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs_after_second) == 1, "Redelivered message must not be ingested twice"
