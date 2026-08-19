"""
Integration test: incoming email → session creation → agent response → outgoing email.

Exercises the full email flow through FastAPI TestClient with IMAP/SMTP stubs.
Background tasks are collected during API calls and drained automatically by
process_emails_with_stub, so the entire pipeline runs end-to-end.
Only agent-env HTTP and SMTP are stubbed.
"""
import uuid
from email.mime.text import MIMEText
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.email.outgoing_email_queue import OutgoingEmailQueue, OutgoingEmailStatus
from app.services.email.sending_service import EmailSendingService
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.email_stubs import StubSMTPConnector
from tests.utils.agent import (
    configure_email_integration,
    create_agent_via_api,
    enable_email_integration,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.mail_server import (
    create_imap_server,
    create_smtp_server,
    process_emails_with_stub,
)
from tests.utils.message import get_messages_by_role
from tests.utils.session import get_agent_session


def _build_raw_email(
    from_addr: str = "sender@example.com",
    to_addr: str = "agent@test.com",
    subject: str = "Test Email Subject",
    body: str = "Hello, this is a test email body.",
    message_id: str | None = None,
) -> bytes:
    """Build a minimal RFC822 email as raw bytes."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<{uuid.uuid4()}@example.com>"
    return msg.as_bytes()


def test_email_integration_owner_mode_full_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Full integration test for owner-mode email flow:
      1. Setup agent + mail servers + email integration via API
      2. Process incoming email (full pipeline runs end-to-end)
      3. Verify session, messages, agent response, and outgoing email
      4. Send outgoing email via SMTP stub
    """
    # ── Phase 1: Setup ───────────────────────────────────────────────────

    me_resp = client.get(
        f"{settings.API_V1_STR}/users/me",
        headers=superuser_token_headers,
    )
    assert me_resp.status_code == 200

    agent = create_agent_via_api(client, superuser_token_headers, name="Email Test Agent")
    drain_tasks()
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent['id']}",
        headers=superuser_token_headers,
    )
    agent = r.json()
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    imap_server = create_imap_server(client, superuser_token_headers)
    smtp_server = create_smtp_server(client, superuser_token_headers)

    configure_email_integration(
        client, superuser_token_headers, agent_id,
        incoming_server_id=imap_server["id"],
        outgoing_server_id=smtp_server["id"],
    )
    enable_email_integration(client, superuser_token_headers, agent_id)

    # ── Phase 2: Process incoming email (full pipeline) ──────────────────

    raw_email = _build_raw_email(
        from_addr="sender@example.com",
        to_addr="agent@test.com",
        subject="Test Email Subject",
        body="Hello, this is a test email body.",
    )

    agent_response_text = "Thank you for your email. I have received it."
    stub_agent_env = StubAgentEnvConnector(response_text=agent_response_text)

    # process_emails_with_stub drains all background tasks automatically,
    # running the full chain: IMAP → session → agent streaming → email queue
    result, stub_imap = process_emails_with_stub(
        client, superuser_token_headers, agent_id,
        raw_emails=[raw_email],
        agent_env_stub=stub_agent_env,
    )

    assert result["polled"] == 1, f"Full result: {result}"
    assert result["processed"] == 1, f"Full result: {result}"
    assert len(stub_imap.connect_calls) == 1
    assert stub_imap.connect_calls[0][0] == "imap.test.com"
    assert len(stub_agent_env.stream_calls) == 1
    assert "Subject: Test Email Subject" in stub_agent_env.stream_calls[0]["payload"]["message"]

    # ── Phase 3: Verify results ──────────────────────────────────────────

    # Session created with email metadata
    chat_session = get_agent_session(client, superuser_token_headers, agent_id)
    session_id = chat_session["id"]
    assert chat_session["integration_type"] == "email"
    assert chat_session["sender_email"] == "sender@example.com"

    # User message contains email content
    user_messages = get_messages_by_role(
        client, superuser_token_headers, session_id, role="user",
    )
    assert len(user_messages) >= 1
    assert "Subject: Test Email Subject" in user_messages[0]["content"]
    assert "From: sender@example.com" in user_messages[0]["content"]
    assert user_messages[0]["sent_to_agent_status"] == "sent"

    # Agent response created by the pipeline
    agent_messages = get_messages_by_role(
        client, superuser_token_headers, session_id, role="agent",
    )
    assert len(agent_messages) >= 1
    agent_message = agent_messages[-1]
    assert agent_response_text in agent_message["content"]

    meta = agent_message["message_metadata"] or {}
    assert "external_session_id" in meta
    assert "streaming_events" in meta
    assert len(meta["streaming_events"]) > 0

    # Outgoing email was queued automatically by handle_stream_completed
    queue_entries = db.exec(
        select(OutgoingEmailQueue).where(
            OutgoingEmailQueue.session_id == uuid.UUID(session_id),
        )
    ).all()
    assert len(queue_entries) == 1
    queue_entry = queue_entries[0]
    assert queue_entry.recipient == "sender@example.com"
    assert queue_entry.status == OutgoingEmailStatus.PENDING

    # ── Phase 4: Send outgoing email (SMTP stub) ────────────────────────

    stub_smtp = StubSMTPConnector()
    with patch(
        "app.services.email.sending_service.smtp_connector",
        stub_smtp,
    ):
        sent_count = EmailSendingService.send_pending_emails(db_session=db)

    assert sent_count == 1
    assert len(stub_smtp.sent_emails) == 1
    sent = stub_smtp.sent_emails[0]
    assert sent["from"] == "agent@test.com"
    assert sent["to"] == "sender@example.com"
    assert sent["server_host"] == "smtp.test.com"

    db.refresh(queue_entry)
    assert queue_entry.status == OutgoingEmailStatus.SENT
    assert queue_entry.sent_at is not None
