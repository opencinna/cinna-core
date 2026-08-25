"""
Tests for session context provisioning in the message streaming pipeline.

Verifies that message_service correctly builds the session_context dict,
includes it in the payload sent to agent-env, and HMAC-signs it using the
environment's auth_token.

Covers:
- Non-email (direct conversation) sessions

Email session context and thread continuity were covered here through the
now-deleted per-agent Email Integration; that behaviour is re-covered over
the channel pipeline in `tests/api/server_channels/` (phase 4 of the
channels & identity unification refactor).
"""
import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api


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


def _get_auth_token(db: Session, environment_id: str) -> str:
    """Read auth_token from the AgentEnvironment config column."""
    env = db.get(AgentEnvironment, uuid.UUID(environment_id))
    assert env is not None, f"Environment {environment_id} not found"
    token = env.config.get("auth_token")
    assert token, f"Environment {environment_id} has no auth_token in config"
    return token


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
    # Phase 2 — bundle-aware session context (no more is_clone/parent_agent_id).
    assert ctx["bundle_id"] is not None
    assert ctx["bundle_uuid"] is None
    assert ctx["is_publisher_install"] is False
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
