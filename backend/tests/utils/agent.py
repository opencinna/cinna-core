"""Helper to create/manage agents via API for tests."""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


def create_agent_via_api(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
) -> dict:
    """Create agent via POST /api/v1/agents/. Environment stub must be active."""
    data = {"name": name or f"Test Agent {random_lower_string()[:8]}"}
    r = client.post(
        f"{settings.API_V1_STR}/agents/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200, f"Agent creation failed: {r.text}"
    return r.json()


def get_agent(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Get agent by ID via GET /api/v1/agents/{id}."""
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get agent failed: {r.text}"
    return r.json()


def update_agent(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    **fields,
) -> dict:
    """Update agent via PUT /api/v1/agents/{id}. Pass fields as kwargs."""
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update agent failed: {r.text}"
    return r.json()


def sync_agent_prompts(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Sync agent prompts to active environment via POST /api/v1/agents/{id}/sync-prompts."""
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/sync-prompts",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Sync prompts failed: {r.text}"
    return r.json()


def enable_a2a(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Enable A2A integration for an agent via PUT /agents/{id}."""
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=token_headers,
        json={"a2a_config": {"enabled": True}},
    )
    assert r.status_code == 200, f"Enable A2A failed: {r.text}"
    return r.json()


def configure_email_integration(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    incoming_server_id: str,
    outgoing_server_id: str,
    agent_session_mode: str = "owner",
    access_mode: str = "open",
    incoming_mailbox: str = "agent@test.com",
    outgoing_from_address: str = "agent@test.com",
) -> dict:
    """Configure email integration via POST /api/v1/agents/{id}/email-integration."""
    data = {
        "agent_session_mode": agent_session_mode,
        "access_mode": access_mode,
        "incoming_server_id": incoming_server_id,
        "outgoing_server_id": outgoing_server_id,
        "incoming_mailbox": incoming_mailbox,
        "outgoing_from_address": outgoing_from_address,
    }
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/email-integration",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200, f"Email integration config failed: {r.text}"
    return r.json()


def enable_email_integration(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Enable email integration via PUT /api/v1/agents/{id}/email-integration/enable."""
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}/email-integration/enable",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Email integration enable failed: {r.text}"
    body = r.json()
    assert body["enabled"] is True
    return body


# ---------------------------------------------------------------------------
# Agent-status rate-limit helpers
#
# AgentStatusService._rate_limit_lock is module-level internal state with
# no public API surface. These helpers isolate the app.services import to
# this utility module so individual test files stay free of app.services
# imports (same pattern as tests/utils/session.py for active_streaming_manager).
# ---------------------------------------------------------------------------

def set_agent_status_rate_limit(env_id: "uuid.UUID") -> None:
    """Pre-populate the force-refresh rate-limit lock for an environment.

    Sets the lock timestamp to now so the very next force_refresh API call
    sees the limit as active and returns 429.
    """
    import uuid  # noqa: F401 — needed for the type annotation at runtime
    from datetime import datetime, UTC
    from app.services.agents import agent_status_service as _mod

    _mod._rate_limit_lock[env_id] = datetime.now(UTC)


def clear_agent_status_rate_limit(env_id: "uuid.UUID") -> None:
    """Remove the force-refresh rate-limit lock for an environment.

    Call this in a ``finally`` block after ``set_agent_status_rate_limit``
    to prevent leaking state across tests.
    """
    from app.services.agents import agent_status_service as _mod

    _mod._rate_limit_lock.pop(env_id, None)
