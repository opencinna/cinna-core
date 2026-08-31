"""Helpers to create IMAP/SMTP mail servers via API for tests.

`/api/v1/mail-servers/` is superuser-only (phase 4 of the channels & identity
unification refactor made `MailServerConfig` server-scoped, not per-agent) —
these helpers must always be called with superuser auth headers.
"""
from fastapi.testclient import TestClient

from app.core.config import settings


def create_imap_server(
    client: TestClient,
    superuser_headers: dict[str, str],
    host: str = "imap.test.com",
    port: int = 993,
    username: str = "agent@test.com",
    password: str = "test-password",
    name: str = "Test IMAP",
) -> dict:
    """Create IMAP server via POST /api/v1/mail-servers/ (superuser-only)."""
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
        headers=superuser_headers,
        json=data,
    )
    assert r.status_code == 200, f"IMAP server creation failed: {r.text}"
    return r.json()


def create_smtp_server(
    client: TestClient,
    superuser_headers: dict[str, str],
    host: str = "smtp.test.com",
    port: int = 465,
    username: str = "agent@test.com",
    password: str = "test-password",
    name: str = "Test SMTP",
) -> dict:
    """Create SMTP server via POST /api/v1/mail-servers/ (superuser-only)."""
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
        headers=superuser_headers,
        json=data,
    )
    assert r.status_code == 200, f"SMTP server creation failed: {r.text}"
    return r.json()
