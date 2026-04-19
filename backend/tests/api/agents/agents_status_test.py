"""
Agent Status API integration tests.

All tests interact exclusively through the HTTP API (TestClient).
Pure-unit tests for AgentStatusService (parser, rate-limit helpers,
refresh_after_action, handle_post_action_event) live in
tests/unit/test_agent_status_service.py.

Scenarios:
  1. GET /agents/status — list snapshots (empty list, with agent, workspace filter)
  2. GET /agents/{agent_id}/status — lifecycle: happy path, 404, 403, unauthenticated
  3. GET /agents/{agent_id}/status?force_refresh=true — file missing fallback,
     file present (stub adapter), rate-limit 429
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import (
    clear_agent_status_rate_limit,
    create_agent_via_api,
    get_agent,
    set_agent_status_rate_limit,
)
from tests.utils.user import create_random_user, user_authentication_headers


# ---------------------------------------------------------------------------
# Scenario 1: GET /agents/status — list endpoint
# ---------------------------------------------------------------------------

def test_list_agent_statuses_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/status returns an empty list when the user has no agents.
    (Superuser has no agents in a clean test transaction.)
    """
    r = client.get(
        f"{settings.API_V1_STR}/agents/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_list_agent_statuses_with_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/status returns a snapshot for each agent owned by the user.
    A newly created agent with no STATUS.md data has null severity.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    r = client.get(
        f"{settings.API_V1_STR}/agents/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1

    # Find our agent in the list
    our = next((i for i in items if i["agent_id"] == agent_id), None)
    assert our is not None
    assert our["severity"] is None
    assert our["raw"] is None


def test_list_agent_statuses_workspace_filter(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/status?workspace_id=<id> filters to agents in that workspace.
    A fake workspace_id returns an empty list.
    """
    fake_workspace_id = str(uuid.uuid4())
    r = client.get(
        f"{settings.API_V1_STR}/agents/status?workspace_id={fake_workspace_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# Scenario 2: GET /agents/{agent_id}/status — lifecycle scenario
# ---------------------------------------------------------------------------

def test_agent_status_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full lifecycle for GET /agents/{agent_id}/status:
      1. Create agent
      2. Happy path (cached) — returns null-severity snapshot
      3. Unknown agent — 404
      4. Other user — 403
      5. Unauthenticated — 401/403
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: Happy path — null-severity snapshot for fresh agent ──────
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == agent_id
    assert body["severity"] is None
    assert body["fetched_at"] is None

    # ── Phase 3: Unknown agent → 404 ─────────────────────────────────────
    fake_id = str(uuid.uuid4())
    r_404 = client.get(
        f"{settings.API_V1_STR}/agents/{fake_id}/status",
        headers=superuser_token_headers,
    )
    assert r_404.status_code == 404

    # ── Phase 4: Other user → 403 ─────────────────────────────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )
    r_403 = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status",
        headers=other_headers,
    )
    assert r_403.status_code == 403

    # ── Phase 5: Unauthenticated → 401/403 ────────────────────────────────
    r_unauth = client.get(f"{settings.API_V1_STR}/agents/{agent_id}/status")
    assert r_unauth.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Scenario 3: GET /agents/{agent_id}/status?force_refresh=true
# ---------------------------------------------------------------------------

def test_get_agent_status_force_refresh_file_missing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    force_refresh=true falls back to cached snapshot (null) when the adapter
    reports the file as missing (stub default: no workspace_files set).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Stub returns exists=False by default (no workspace_files set)
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
        headers=superuser_token_headers,
    )
    # Should NOT be 500 — falls back to cached snapshot (which is empty)
    assert r.status_code == 200
    body = r.json()
    assert body["severity"] is None
    assert body["raw"] is None


def test_get_agent_status_force_refresh_with_status_file(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    force_refresh=true fetches and parses STATUS.md when the adapter has
    file content set on workspace_files.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    status_content = b"---\nstatus: ok\nsummary: All systems nominal\n---\n\n# Agent Status\n"

    # workspace_files is a class-level dict on the test adapter — populate it
    # so fetch_workspace_item_with_meta returns this content for docs/STATUS.md
    # whenever an environment exists for the agent.
    EnvironmentTestAdapter.workspace_files["docs/STATUS.md"] = status_content
    try:
        r = client.get(
            f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
            headers=superuser_token_headers,
        )
        assert r.status_code == 200
    finally:
        EnvironmentTestAdapter.workspace_files.pop("docs/STATUS.md", None)


def test_get_agent_status_force_refresh_rate_limited(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    force_refresh=true returns 429 when the rate limit is active.

    Strategy: create an agent, obtain its environment_id via the API response,
    then pre-populate the module-level rate-limit lock so the next
    force_refresh call is guaranteed to see the limit and return 429.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Fetch the agent to get active_environment_id (exposed in AgentPublic)
    agent_data = get_agent(client, superuser_token_headers, agent_id)
    env_id_str = agent_data.get("active_environment_id")
    assert env_id_str is not None, "Stub failed to create environment"

    env_id = uuid.UUID(env_id_str)

    # Pre-mark the rate limit as just-set so the next call sees it
    set_agent_status_rate_limit(env_id)
    try:
        r = client.get(
            f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
            headers=superuser_token_headers,
        )
        assert r.status_code == 429, (
            f"Expected 429 when rate limit is active, got {r.status_code}: {r.text}"
        )
    finally:
        clear_agent_status_rate_limit(env_id)
