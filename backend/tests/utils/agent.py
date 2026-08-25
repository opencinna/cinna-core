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


def set_router_trigger_prompt(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    trigger_prompt: str = "Handle anything",
) -> dict:
    """PATCH /agents/{id}/router-trigger-prompt — the owner-only field.

    This is what makes an agent a **channel routing candidate**.
    ``ChannelCandidateProvider`` builds Pass 1's ballot from the sender's own
    agents and admits one that has a non-blank ``router_trigger_prompt`` or a
    non-empty ``example_prompts``; an ``AppAgentRoute`` (personal or admin)
    grants nothing on the channel path any more — it is an App-MCP exposure and
    is read only there.

    Setting it here also propagates to the agent's auto-managed route, so a
    setup that wants both surfaces gets both from this one call.
    """
    r = client.patch(
        f"{settings.API_V1_STR}/agents/{agent_id}/router-trigger-prompt",
        headers=token_headers,
        json={"router_trigger_prompt": trigger_prompt},
    )
    assert r.status_code == 200, f"Set router trigger prompt failed: {r.text}"
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


# ---------------------------------------------------------------------------
# Agent-status rate-limit helpers
#
# AgentStatusService._rate_limit_lock is module-level internal state with
# no public API surface. These helpers isolate the app.services import to
# this utility module so individual test files stay free of app.services
# imports (same pattern as tests/utils/session.py for active_streaming_manager).
# ---------------------------------------------------------------------------

def list_agents(
    client: TestClient,
    token_headers: dict[str, str],
) -> dict:
    """List agents via GET /api/v1/agents/. Returns the full {data, count} JSON body."""
    r = client.get(
        f"{settings.API_V1_STR}/agents/",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List agents failed: {r.text}"
    return r.json()


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
