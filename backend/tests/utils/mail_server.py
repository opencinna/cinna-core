"""Helpers to create IMAP/SMTP mail servers via API for tests."""
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.email_stubs import StubIMAPConnector
from tests.utils.background_tasks import drain_tasks


def create_imap_server(
    client: TestClient,
    token_headers: dict[str, str],
    host: str = "imap.test.com",
    port: int = 993,
    username: str = "agent@test.com",
    password: str = "test-password",
    name: str = "Test IMAP",
) -> dict:
    """Create IMAP server via POST /api/v1/mail-servers/."""
    data = {
        "name": name,
        "server_type": "imap",
        "host": host,
        "port": port,
        "encryption_type": "ssl",
        "username": username,
        "password": password,
    }
    r = client.post(
        f"{settings.API_V1_STR}/mail-servers/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200, f"IMAP server creation failed: {r.text}"
    return r.json()


def create_smtp_server(
    client: TestClient,
    token_headers: dict[str, str],
    host: str = "smtp.test.com",
    port: int = 465,
    username: str = "agent@test.com",
    password: str = "test-password",
    name: str = "Test SMTP",
) -> dict:
    """Create SMTP server via POST /api/v1/mail-servers/."""
    data = {
        "name": name,
        "server_type": "smtp",
        "host": host,
        "port": port,
        "encryption_type": "ssl",
        "username": username,
        "password": password,
    }
    r = client.post(
        f"{settings.API_V1_STR}/mail-servers/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200, f"SMTP server creation failed: {r.text}"
    return r.json()


def process_emails_with_stub(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    raw_emails: list[bytes],
    agent_env_stub=None,
) -> tuple[dict, StubIMAPConnector]:
    """Process emails via API with a stubbed IMAP connector.

    Polls IMAP via stub, then drains all background tasks (process_pending_messages,
    event handlers, etc.) so the full pipeline completes before returning.

    Args:
        agent_env_stub: Optional StubAgentEnvConnector. When provided, patches
            agent_env_connector during task execution so the streaming pipeline
            uses the stub instead of making real HTTP calls.

    Returns (response_json, stub_imap) so callers can assert on both the API
    result and the stubs' recorded calls.
    """
    stub_imap = StubIMAPConnector(emails=raw_emails)
    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.email.polling_service.imap_connector", stub_imap)
        )
        if agent_env_stub:
            stack.enter_context(
                patch("app.services.sessions.message_service.agent_env_connector", agent_env_stub)
            )
        r = client.post(
            f"{settings.API_V1_STR}/agents/{agent_id}/email-integration/process-emails",
            headers=token_headers,
        )
        # Drain background tasks while all patches are active.
        # Must happen after client.post() returns (test thread, no event loop).
        drain_tasks()

    assert r.status_code == 200, f"Process emails failed: {r.text}"
    return r.json(), stub_imap
