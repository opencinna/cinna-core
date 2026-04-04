"""
Tests for session context provisioning in the message streaming pipeline.

Verifies that message_service correctly builds the session_context dict,
includes it in the payload sent to agent-env, and HMAC-signs it using the
environment's auth_token.

Covers:
- Non-email (direct conversation) sessions
- Email-initiated sessions with full metadata
- Email thread continuity (two emails → same session, same backend_session_id)
"""
import hashlib
import hmac
import json
import uuid
from email.mime.text import MIMEText
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from tests.stubs.agent_env_stub import StubAgentEnvConnector
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
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api, get_agent_session


# ── Local helpers ────────────────────────────────────────────────────────


def _verify_session_context(context: dict, signature: str, signing_key: str) -> bool:
    """Verify HMAC-SHA256 signature of session context using only stdlib.

    Mirrors the logic in session_context_signer without importing from app.services.
    Uses canonical JSON (sorted keys, no whitespace) for deterministic signing.
    """
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(
        signing_key.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _build_raw_email(
    from_addr: str = "sender@example.com",
    to_addr: str = "agent@test.com",
    subject: str = "Test Email Subject",
    body: str = "Hello, this is a test email body.",
    message_id: str | None = None,
    in_reply_to: str | None = None,
) -> bytes:
    """Build a minimal RFC822 email as raw bytes, with optional In-Reply-To."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<{uuid.uuid4()}@example.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    return msg.as_bytes()


def _get_auth_token(db: Session, environment_id: str) -> str:
    """Read auth_token from the AgentEnvironment config column."""
    env = db.get(AgentEnvironment, uuid.UUID(environment_id))
    assert env is not None, f"Environment {environment_id} not found"
    token = env.config.get("auth_token")
    assert token, f"Environment {environment_id} has no auth_token in config"
    return token


def _setup_email_agent(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[dict, str]:
    """Create agent + mail servers + email integration. Returns (agent_dict, agent_id)."""
    agent = create_agent_via_api(client, headers, name="Session Context Email Agent")
    drain_tasks()
    # Re-fetch to get active_environment_id
    r = client.get(f"{settings.API_V1_STR}/agents/{agent['id']}", headers=headers)
    agent = r.json()
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    imap_server = create_imap_server(client, headers)
    smtp_server = create_smtp_server(client, headers)
    configure_email_integration(
        client, headers, agent_id,
        incoming_server_id=imap_server["id"],
        outgoing_server_id=smtp_server["id"],
    )
    enable_email_integration(client, headers, agent_id)
    return agent, agent_id


# ── Tests ────────────────────────────────────────────────────────────────


def test_non_email_session_context_and_hmac(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Direct conversation: session_context is present, has correct fields,
    HMAC signature verifies, and tampered context fails verification.
    """
    # Setup: create agent + session
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent['id']}",
        headers=superuser_token_headers,
    )
    agent = r.json()
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    assert env_id is not None

    session = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session["id"]

    # Send a message with StubAgentEnvConnector
    stub = StubAgentEnvConnector(response_text="Hello from agent")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, "Hi there")
        drain_tasks()

    # ── Verify session_context in payload ────────────────────────────
    assert len(stub.stream_calls) == 1
    payload = stub.stream_calls[0]["payload"]
    assert "session_state" in payload

    session_state = payload["session_state"]
    ctx = session_state["session_context"]
    sig = session_state["session_context_signature"]

    # Field values for non-email session
    assert ctx["integration_type"] is None
    assert ctx["agent_id"] == agent_id
    assert ctx["is_clone"] is False
    assert ctx["parent_agent_id"] is None
    assert ctx["sender_email"] is None
    assert ctx["email_thread_id"] is None
    assert ctx["backend_session_id"] == session_id
    # email_subject only added for email integration
    assert "email_subject" not in ctx

    # Signature format: 64-char hex (SHA-256)
    assert isinstance(sig, str)
    assert len(sig) == 64

    # HMAC verification with real auth_token
    auth_token = _get_auth_token(db, env_id)
    assert _verify_session_context(ctx, sig, auth_token) is True

    # Tamper detection: mutating a field should invalidate signature
    tampered = dict(ctx)
    tampered["agent_id"] = str(uuid.uuid4())
    assert _verify_session_context(tampered, sig, auth_token) is False


def test_email_session_context_full_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Email integration: session_context contains email-specific fields
    (sender_email, email_subject, email_thread_id) and HMAC verifies.
    """
    agent, agent_id = _setup_email_agent(client, superuser_token_headers)
    env_id = agent["active_environment_id"]

    raw_email = _build_raw_email(
        from_addr="customer@example.com",
        to_addr="agent@test.com",
        subject="Order Status Request",
        body="Please check my order #12345.",
    )

    stub = StubAgentEnvConnector(response_text="Let me check your order.")
    result, _ = process_emails_with_stub(
        client, superuser_token_headers, agent_id,
        raw_emails=[raw_email],
        agent_env_stub=stub,
    )
    assert result["processed"] == 1

    # ── Verify session_context ───────────────────────────────────────
    assert len(stub.stream_calls) == 1
    payload = stub.stream_calls[0]["payload"]
    session_state = payload["session_state"]
    ctx = session_state["session_context"]
    sig = session_state["session_context_signature"]

    assert ctx["integration_type"] == "email"
    assert ctx["sender_email"] == "customer@example.com"
    assert ctx["email_subject"] == "Order Status Request"
    assert ctx["agent_id"] == agent_id
    assert ctx["is_clone"] is False
    assert ctx["email_thread_id"] is not None

    # backend_session_id matches the session created for this email
    chat_session = get_agent_session(client, superuser_token_headers, agent_id)
    assert ctx["backend_session_id"] == chat_session["id"]

    # HMAC signature valid
    auth_token = _get_auth_token(db, env_id)
    assert _verify_session_context(ctx, sig, auth_token) is True


def test_email_thread_continuity_session_context(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Two emails in the same thread reuse the same session and both
    stream calls carry matching backend_session_id and email fields.
    """
    agent, agent_id = _setup_email_agent(client, superuser_token_headers)

    first_message_id = f"<first-{uuid.uuid4()}@example.com>"

    email_1 = _build_raw_email(
        from_addr="customer@example.com",
        to_addr="agent@test.com",
        subject="Shipping Question",
        body="When does my package arrive?",
        message_id=first_message_id,
    )

    # First email
    stub1 = StubAgentEnvConnector(response_text="Checking shipment status.")
    result1, _ = process_emails_with_stub(
        client, superuser_token_headers, agent_id,
        raw_emails=[email_1],
        agent_env_stub=stub1,
    )
    assert result1["processed"] == 1
    assert len(stub1.stream_calls) == 1

    # Second email in same thread (In-Reply-To first)
    email_2 = _build_raw_email(
        from_addr="customer@example.com",
        to_addr="agent@test.com",
        subject="Re: Shipping Question",
        body="Any update on the tracking?",
        in_reply_to=first_message_id,
    )

    stub2 = StubAgentEnvConnector(response_text="Here is the tracking info.")
    result2, _ = process_emails_with_stub(
        client, superuser_token_headers, agent_id,
        raw_emails=[email_2],
        agent_env_stub=stub2,
    )
    assert result2["processed"] == 1
    assert len(stub2.stream_calls) == 1

    # ── Verify both calls share the same backend_session_id ──────────
    ctx1 = stub1.stream_calls[0]["payload"]["session_state"]["session_context"]
    ctx2 = stub2.stream_calls[0]["payload"]["session_state"]["session_context"]

    assert ctx1["backend_session_id"] == ctx2["backend_session_id"]

    # Both have email integration type and sender
    assert ctx1["integration_type"] == "email"
    assert ctx2["integration_type"] == "email"
    assert ctx1["sender_email"] == "customer@example.com"
    assert ctx2["sender_email"] == "customer@example.com"

    # email_subject reflects initiating email (first email's subject)
    assert ctx1["email_subject"] == "Shipping Question"
    assert ctx2["email_subject"] == "Shipping Question"
