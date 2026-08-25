"""The Email channel — the first *polled* transport (channels & identity
unification, phase 4).

Reuses the domain's established machinery (`create_server_channel`, the
session/message/background-task stubs, the `patch_create_session` /
`patch_anyio_to_thread` autouse fixtures from `conftest.py`) and adds only
what the polled transport needs: `tests/utils/email_channel.py` for raw-MIME
construction, the email-shaped admin-create composition, and the
`poll_channel` Rule-1 exemption (mirrors `flush_pending_bindings` — the poll
scheduler has no HTTP surface and is `TESTING`-gated, per project
convention).

`EmailMessage` / `OutgoingEmailQueue` / `ChannelThreadBinding` have no
admin/user-facing GET endpoint (same posture the domain README already states
for bindings: "verified through observable effects instead"). Here the rows
themselves *are* the observable effect several of these scenarios are about,
so they are read directly via `db.exec(select(...))`, exactly as
`server_channels_pending_outbound_test.py` already does for
`ChannelThreadBinding`.

Covers (phase 4 plan §6):
  1. A polled channel needs no webhook token; a webhook channel still does.
  2. Sender-routed: a known user's email routes over their own agent.
  3. Auto-registration: the channel's own whitelist is the sole gate —
     `AUTH_WHITELIST_USER_DOMAINS` is deliberately not re-checked.
  4. Threading: a reply binds to the same thread, keyed on the *root*
     Message-ID in both directions.
  5. Reply headers: `In-Reply-To` / `References` on the queued reply, and the
     composite transport key round-trips (unit-level round-trip lives in
     tests/unit/test_email_channel_thread_key.py — see that file for the
     `>|<` separator trap).
  6. `_binding_thread_key` stays total when a binding is deleted between
     commit and delivery: `None`, and a declined send — never an exception.
  7. Recipient validation: mail to a different mailbox in the same inbox is
     ignored.
  8. `has_outbound_credentials` is derived from the referenced SMTP server,
     not from `encrypted_secrets`.
  9. Store-on-arrival: the `EmailMessage` row exists with a NULL `agent_id`
     before classification runs, and is stamped after — including the case
     that motivated it, a sender denied before routing.
  10. Stamped-redelivery drop: a stamped row is dropped on redelivery; an
      unstamped (denied/failed) row is still returned to the pipeline.
  11/12. Email session context + thread continuity — re-covering, over the
      channel pipeline, the two scenarios `tests/api/agents/sessions
      /agents_session_context_test.py` used to cover through the deleted
      per-agent Email Integration (see that file's module docstring for the
      cross-reference back to here). `integration_type` is `channel_email`,
      not `"email"` — the `== "email"` enrichment branch in
      `message_service._build_session_context` is dead for channel-borne
      sessions by design (re-stamping it would break
      `ChannelOutboundService._resolve_channel_session`'s `channel_` prefix
      gate); the subject reaches the agent through
      `EmailPollingService.format_email_as_message` in the message text
      instead, and these tests assert exactly that shape rather than the
      pre-refactor one.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from app.core.config import settings
from app.models import ChannelThreadBinding, ServerChannel
from app.models.email.email_message import EmailMessage
from app.models.email.outgoing_email_queue import OutgoingEmailQueue
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.email_stubs import StubIMAPConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.email_channel import (
    IMAP_CONNECTOR_TARGET,
    build_raw_email,
    create_email_channel,
    poll_channel,
)
from tests.utils.mail_server import create_imap_server, create_smtp_server
from tests.utils.message import list_messages
from tests.utils.server_channel import (
    binding_thread_key,
    create_server_channel,
    deliver_via_binding,
    update_server_channel,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"


# ---------------------------------------------------------------------------
# Local setup helpers
# ---------------------------------------------------------------------------


def _mail_servers(client, superuser_headers) -> tuple[str, str]:
    imap = create_imap_server(client, superuser_headers)
    smtp = create_smtp_server(client, superuser_headers)
    return imap["id"], smtp["id"]


def _known_sender_with_agent(client, superuser_headers):
    """A platform user with exactly one eligible agent (Pass 1's `only_one`
    short-circuit — no classifier is ever reached, matching the domain's
    documented "deterministic Pass 1 setup" pattern)."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"EmailAgent-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _poll_with_stubs(db: Session, emails: list[bytes], *, stream_stub=None):
    """One poll tick: patch the IMAP connector + agent-env stream target."""
    stub = stream_stub or StubAgentEnvConnector(response_text="On it.")
    imap_stub = StubIMAPConnector(emails=emails)
    with patch(IMAP_CONNECTOR_TARGET, imap_stub), patch(_STREAM_TARGET, stub):
        processed = poll_channel(db)
        drain_tasks()
    return processed, stub


def _email_message(db: Session, message_id: str) -> EmailMessage | None:
    """Fresh read of one ``EmailMessage`` row. See module docstring."""
    db.expire_all()
    return db.exec(
        select(EmailMessage).where(EmailMessage.email_message_id == message_id)
    ).first()


def _binding_for_channel(db: Session, channel_id: str) -> ChannelThreadBinding | None:
    db.expire_all()
    return db.exec(
        select(ChannelThreadBinding).where(
            ChannelThreadBinding.server_channel_id == uuid.UUID(channel_id)
        )
    ).first()


def _outgoing_queue_for_session(db: Session, session_id: str) -> list[OutgoingEmailQueue]:
    db.expire_all()
    return list(
        db.exec(
            select(OutgoingEmailQueue)
            .where(OutgoingEmailQueue.session_id == uuid.UUID(session_id))
            .order_by(OutgoingEmailQueue.created_at.asc())
        ).all()
    )


# ---------------------------------------------------------------------------
# 1. A polled channel needs no webhook token
# ---------------------------------------------------------------------------


def test_polled_channel_needs_no_webhook_token_but_webhook_channel_does(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)

    email_channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox="support@corp.example",
    )
    assert email_channel["webhook_token"] is None
    assert email_channel["webhook_url"] is None

    chat_channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    assert chat_channel["webhook_token"] is not None
    assert chat_channel["webhook_url"] is not None
    assert chat_channel["webhook_token"] in chat_channel["webhook_url"]


# ---------------------------------------------------------------------------
# 2. Sender-routed
# ---------------------------------------------------------------------------


def test_sender_routed_email_reaches_the_senders_own_agent(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="Need a hand",
        body="please help me with this",
    )

    processed, _ = _poll_with_stubs(db, [raw])
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    user_msgs = [m for m in list_messages(client, headers, sessions[0]["id"]) if m["role"] == "user"]
    assert len(user_msgs) == 1
    content = user_msgs[0]["content"] or ""
    assert "please help me with this" in content
    assert "Subject: Need a hand" in content
    assert f"From: {user['email']}" in content


# ---------------------------------------------------------------------------
# 3. Auto-registration — the channel's own whitelist is the sole gate
# ---------------------------------------------------------------------------


def test_auto_registration_gate_is_the_channels_own_whitelist_not_platform_wide(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    A domain the channel's whitelist allows, but ``AUTH_WHITELIST_USER_DOMAINS``
    would reject, still gets auto-registered — proving the platform-wide
    signup allowlist is genuinely not re-checked, not merely untested.
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*@example.com",
        auto_register_users=True,
    )
    sender_email = f"newcomer-{random_lower_string()[:8]}@example.com"
    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email(message_id=msg_id, sender=sender_email, to=mailbox, body="hello")

    with patch("app.core.config.settings.AUTH_WHITELIST_USER_DOMAINS", "neverused.example"):
        processed, _ = _poll_with_stubs(db, [raw])
    assert processed == 1

    r = client.get(
        f"{API}/users/search",
        headers=superuser_token_headers,
        params={"q": sender_email, "include_self": True},
    )
    assert r.status_code == 200
    results = r.json()["data"]
    assert len(results) == 1
    user_id = results[0]["id"]

    user_detail = client.get(f"{API}/users/{user_id}", headers=superuser_token_headers).json()
    assert user_detail["has_password"] is False
    assert user_detail["email_confirmed"] is True
    assert user_detail["role"] == "agent-user"
    assert user_detail["is_superuser"] is False


# ---------------------------------------------------------------------------
# 4. Threading — root, not latest
# ---------------------------------------------------------------------------


def test_reply_binds_to_the_same_thread_via_the_root_message_id(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    A reply with In-Reply-To/References must bind to the SAME thread as the
    original — and the binding's thread_key must be the thread ROOT in both
    directions, never the latest Message-ID (plan §11's headline trap: keying
    on the latest makes every reply open a new binding, and the symptom reads
    as "the agent forgets the conversation").
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    root_id = f"<root-{random_lower_string()}@sender.example>"
    reply_id = f"<reply-{random_lower_string()}@sender.example>"

    raw_original = build_raw_email(
        message_id=root_id, sender=user["email"], to=mailbox,
        subject="Original question", body="first message",
    )
    processed1, _ = _poll_with_stubs(db, [raw_original])
    assert processed1 == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    session_id = sessions[0]["id"]

    # thread_key for the FIRST message must already be its own (root) id.
    binding = _binding_for_channel(db, channel["id"])
    assert binding is not None
    assert binding.thread_key == root_id

    raw_reply = build_raw_email(
        message_id=reply_id, sender=user["email"], to=mailbox,
        subject="Re: Original question", body="second message",
        in_reply_to=root_id, references=root_id,
    )
    processed2, _ = _poll_with_stubs(db, [raw_reply])
    assert processed2 == 1

    # Same session — no new binding was created.
    sessions_after = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions_after) == 1
    assert sessions_after[0]["id"] == session_id

    # Explicitly the ROOT, not the reply's own id.
    binding_after = _binding_for_channel(db, channel["id"])
    assert binding_after is not None
    assert binding_after.id == binding.id
    assert binding_after.thread_key == root_id
    assert binding_after.thread_key != reply_id

    user_msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs) == 2
    assert any("second message" in (m["content"] or "") for m in user_msgs)

    # Both stored EmailMessage rows resolve to the same root and the same
    # session once routed — the record_routing_outcome stamp.
    row_original = _email_message(db, root_id)
    row_reply = _email_message(db, reply_id)
    assert row_original is not None and row_reply is not None
    assert row_original.session_id == row_reply.session_id == uuid.UUID(session_id)


# ---------------------------------------------------------------------------
# 5. Reply headers
# ---------------------------------------------------------------------------


def test_reply_carries_in_reply_to_and_references_headers(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The agent's queued reply to the SECOND message in a thread carries
    In-Reply-To = the last inbound Message-ID (the reply, not the root) and a
    References chain of root-then-last. The composite transport key that
    produced it round-trips through the transport's own parse helper (pure
    round-trip edge cases, including the ``|``-in-Message-ID trap, are unit
    tested in tests/unit/test_email_channel_thread_key.py).
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    root_id = f"<root-{random_lower_string()}@sender.example>"
    reply_id = f"<reply-{random_lower_string()}@sender.example>"

    raw_original = build_raw_email(
        message_id=root_id, sender=user["email"], to=mailbox, body="first message",
    )
    _poll_with_stubs(db, [raw_original])
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    session_id = sessions[0]["id"]

    raw_reply = build_raw_email(
        message_id=reply_id, sender=user["email"], to=mailbox, body="second message",
        in_reply_to=root_id, references=root_id,
    )
    _poll_with_stubs(db, [raw_reply])

    queue_rows = _outgoing_queue_for_session(db, session_id)
    # One queued reply per inbound message (original, then the reply).
    assert len(queue_rows) == 2
    latest = queue_rows[-1]
    assert latest.in_reply_to == reply_id
    assert latest.references == f"{root_id} {reply_id}"

    # The composite key that produced this row, round-tripped.
    binding = _binding_for_channel(db, channel["id"])
    channel_row = db.get(ServerChannel, uuid.UUID(channel["id"]))
    composite = binding_thread_key(binding, channel_row)
    assert composite == f"{root_id}|{reply_id}"


# ---------------------------------------------------------------------------
# 6. _binding_thread_key stays total
# ---------------------------------------------------------------------------


def test_binding_thread_key_is_total_and_a_deleted_binding_declines_the_send(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    A binding deleted between the caller's ``db.commit()`` and delivery must
    yield ``None`` from ``_binding_thread_key`` — never an ``ObjectDeletedError``
    — and the outbound ``_deliver`` path must decline the send rather than
    raise. Plan §11's other named trap.
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    root_id = f"<root-{random_lower_string()}@sender.example>"
    reply_id = f"<reply-{random_lower_string()}@sender.example>"
    _poll_with_stubs(db, [build_raw_email(message_id=root_id, sender=user["email"], to=mailbox, body="hi")])
    _poll_with_stubs(db, [build_raw_email(
        message_id=reply_id, sender=user["email"], to=mailbox, body="again",
        in_reply_to=root_id, references=root_id,
    )])

    binding = _binding_for_channel(db, channel["id"])
    channel_row = db.get(ServerChannel, uuid.UUID(channel["id"]))
    assert binding is not None

    # Healthy path first: a real composite, not None.
    assert binding_thread_key(binding, channel_row) == f"{root_id}|{reply_id}"

    # Mirror the real call site: every path into _deliver arrives after a
    # db.commit() that expires the instance.
    db.commit()

    # Simulate a concurrent deletion of the row underneath the expired
    # instance — a raw delete with ORM session-sync turned OFF, not an
    # ORM-tracked db.delete(binding). ``synchronize_session=False`` is the
    # point: the cached Python object must stay exactly as
    # expired-but-unaware as a genuinely different connection would leave
    # it, discovering the row is gone only via the ordinary expire-then-
    # lazy-reload path, never via SQLAlchemy proactively evicting it here.
    db.execute(
        sa_delete(ChannelThreadBinding)
        .where(ChannelThreadBinding.id == binding.id)
        .execution_options(synchronize_session=False)
    )
    db.commit()

    assert binding_thread_key(binding, channel_row) is None

    delivered = deliver_via_binding(db, channel_row, binding, "does this crash?")
    assert delivered is False


# ---------------------------------------------------------------------------
# 7. Recipient validation
# ---------------------------------------------------------------------------


def test_mail_addressed_to_a_different_mailbox_is_ignored(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox="support@corp.example",
        email_whitelist="*",
    )
    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email(
        message_id=msg_id,
        sender="someone@example.com",
        to="totally-different-mailbox@corp.example",
        body="not for this channel",
    )

    processed, _ = _poll_with_stubs(db, [raw])
    assert processed == 0
    # Never even accepted into the store: the recipient filter runs before
    # _store_arrivals.
    assert _email_message(db, msg_id) is None


# ---------------------------------------------------------------------------
# 8. has_outbound_credentials is derived, not read off encrypted_secrets
# ---------------------------------------------------------------------------


def test_has_outbound_credentials_is_true_for_a_configured_email_channel(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox="support@corp.example",
    )
    # No `secrets` were ever passed — encrypted_secrets is None for this row —
    # yet the field must read True, because it derives from outgoing_server_id.
    assert channel["has_outbound_credentials"] is True

    # Contrast: a Google Chat channel with no secrets reads False, which is
    # the default (encrypted_secrets-backed) reading this transport overrides.
    chat_channel = create_server_channel(client, superuser_token_headers, email_whitelist="*")
    assert chat_channel["has_outbound_credentials"] is False


# ---------------------------------------------------------------------------
# 9. Store-on-arrival
# ---------------------------------------------------------------------------


def test_email_message_is_stored_on_arrival_before_classification_and_stamped_after(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Phase 1: a routable message is stored with a NULL agent_id the instant it
    is polled — before the background routing task has even run — and is
    stamped with agent_id/session_id only once drain_tasks() lets routing
    complete.

    Phase 2: the case that motivated storing on arrival at all — a sender the
    whitelist denies never reaches routing, and the row is the only durable
    trace of that decline (declines are silent to the sender on email, and
    ChannelDebugBuffer is process-local). Its agent_id stays NULL forever.
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email(
        message_id=msg_id, sender=user["email"], to=mailbox,
        subject="Store me first", body="routable message",
    )

    # ── Phase 1: before classification ──────────────────────────────────
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])):
        processed = poll_channel(db)
    assert processed == 1

    row_before = _email_message(db, msg_id)
    assert row_before is not None
    assert row_before.agent_id is None
    assert row_before.session_id is None
    assert row_before.sender == user["email"].lower()
    assert row_before.subject == "Store me first"

    # ── After classification: stamped ───────────────────────────────────
    stub = StubAgentEnvConnector(response_text="Sure.")
    with patch(_STREAM_TARGET, stub):
        drain_tasks()

    row_after = _email_message(db, msg_id)
    assert row_after is not None
    assert row_after.agent_id == uuid.UUID(agent["id"])
    assert row_after.session_id is not None

    # ── Phase 2: a denied sender still leaves a durable, unstamped row ──
    denied_msg_id = f"<{random_lower_string()}@sender.example>"
    raw_denied = build_raw_email(
        message_id=denied_msg_id, sender="stranger@not-whitelisted.example", to=mailbox,
        subject="I should not get through", body="denied message",
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw_denied])):
        processed_denied = poll_channel(db)
        drain_tasks()
    # process_inbound ran (it reached the whitelist gate and was denied); the
    # row exists regardless of the outcome.
    assert processed_denied == 1

    row_denied = _email_message(db, denied_msg_id)
    assert row_denied is not None
    assert row_denied.agent_id is None
    assert row_denied.session_id is None


# ---------------------------------------------------------------------------
# 10. Stamped-redelivery drop
# ---------------------------------------------------------------------------


def test_redelivery_drops_stamped_row_but_retries_unstamped_row(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    A Message-ID redelivered (IMAP `\\Seen` flag lost, or a restart) after it
    was already routed is dropped in `_store_arrivals` and never reaches
    `process_inbound` again — that would be a second classification and a
    possible second install. A redelivery of mail that was DENIED (never
    stamped) must still reach the pipeline, because denials are retryable.
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    # ── Stamped row: a known sender's message routes over their own agent ──
    routed_msg_id = f"<{random_lower_string()}@sender.example>"
    raw_routed = build_raw_email(
        message_id=routed_msg_id, sender=user["email"], to=mailbox, body="please route this",
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw_routed])), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="ok")
    ):
        processed_routed = poll_channel(db)
        drain_tasks()
    assert processed_routed == 1
    routed_row = _email_message(db, routed_msg_id)
    assert routed_row is not None
    assert routed_row.agent_id == uuid.UUID(agent["id"]), "precondition: the row must be stamped"

    # Redeliver the SAME Message-ID (fresh IMAP stub, same bytes) — the
    # redelivery must be dropped in _store_arrivals, never reaching
    # process_inbound again.
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw_routed])):
        processed_redelivery = poll_channel(db)
    assert processed_redelivery == 0, (
        "A redelivery of an already-routed (stamped) message must be dropped "
        "in _store_arrivals, never reaching process_inbound again."
    )

    # ── Unstamped row (denied): redelivery must still reach the pipeline ──
    denied_msg_id = f"<{random_lower_string()}@sender.example>"
    raw_denied = build_raw_email(
        message_id=denied_msg_id, sender="nobody@denied.example", to=mailbox, body="denied",
    )
    update_server_channel(client, superuser_token_headers, channel["id"], email_whitelist=user["email"])
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw_denied])):
        processed_deny_1 = poll_channel(db)
        drain_tasks()
    assert processed_deny_1 == 1
    assert _email_message(db, denied_msg_id).agent_id is None

    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw_denied])):
        processed_deny_2 = poll_channel(db)
        drain_tasks()
    assert processed_deny_2 == 1, (
        "A redelivery of a never-routed (unstamped/denied) message must still "
        "reach process_inbound — denials must stay retryable."
    )


# ---------------------------------------------------------------------------
# 11 / 12. Email session context + thread continuity
# ---------------------------------------------------------------------------


def test_email_session_context_full_fields(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    An email-originated session's context: integration_type is
    "channel_email" (not the dead "email" string), the old
    sender_email/email_thread_id columns stay NULL for a channel session
    (they were only ever stamped by the deleted per-agent integration), and
    there is no email_subject key — the subject reaches the agent through the
    message text instead (asserted below), not through session_context.
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email(
        message_id=msg_id, sender=user["email"], to=mailbox,
        subject="Context check", body="what fields do you see",
    )
    stub = StubAgentEnvConnector(response_text="Got it")
    processed, stub = _poll_with_stubs(db, [raw], stream_stub=stub)
    assert processed == 1

    assert len(stub.stream_calls) == 1
    payload = stub.stream_calls[0]["payload"]
    ctx = payload["session_state"]["session_context"]

    assert ctx["integration_type"] == "channel_email"
    assert ctx["agent_id"] == agent["id"]
    assert ctx["sender_email"] is None
    assert ctx["email_thread_id"] is None
    assert "email_subject" not in ctx

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    session_id = sessions[0]["id"]
    assert ctx["backend_session_id"] == session_id

    # The subject reaches the agent through the message text.
    user_msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert any("Subject: Context check" in (m["content"] or "") for m in user_msgs)


def test_email_thread_continuity_session_context(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A reply continues the SAME session — same backend_session_id in the
    session_context handed to the agent on the second turn, and the message
    count grows rather than a second session being created."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    root_id = f"<root-{random_lower_string()}@sender.example>"
    reply_id = f"<reply-{random_lower_string()}@sender.example>"

    processed1, stub1 = _poll_with_stubs(
        db,
        [build_raw_email(message_id=root_id, sender=user["email"], to=mailbox, body="first turn")],
        stream_stub=StubAgentEnvConnector(response_text="First reply"),
    )
    assert processed1 == 1
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    session_id = sessions[0]["id"]

    stub2 = StubAgentEnvConnector(response_text="Second reply")
    processed2, stub2 = _poll_with_stubs(
        db,
        [build_raw_email(
            message_id=reply_id, sender=user["email"], to=mailbox, body="second turn",
            in_reply_to=root_id, references=root_id,
        )],
        stream_stub=stub2,
    )
    assert processed2 == 1

    # No new session was created.
    sessions_after = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions_after) == 1
    assert sessions_after[0]["id"] == session_id

    # The second turn's session_context names the SAME backend session.
    assert len(stub2.stream_calls) == 1
    ctx2 = stub2.stream_calls[0]["payload"]["session_state"]["session_context"]
    assert ctx2["backend_session_id"] == session_id
    assert ctx2["integration_type"] == "channel_email"

    user_msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs) == 2
    assert any("second turn" in (m["content"] or "") for m in user_msgs)
