"""
Integration tests for POST /environments/{id}/usage-intent.

Scenarios:
  1. Happy path — owner calls usage-intent on a running environment →
     200, status="ok", environment_id echoes the env.
  2. Suspended environment — usage-intent triggers background activation →
     200, status="activating", environment_id is the env.
  3. Unauthenticated → 401/403.
  4. Foreign user → 403 ("not enough permissions" semantics, matching
     GET /environments/{id} ownership behaviour).
  5. Unknown environment id → 404.

Note on resolution (Scenario 3 from the spec): calling with a non-active
environment when the agent has a *different* active environment requires
two environments on one agent to be set up with deterministic active-env
pointers. The test infrastructure supports this (create_environment + activate_environment),
but the active_environment_id on the agent after activation can only be
verified via GET /agents/{id}. This scenario is exercised at the bottom of
the happy-path test since we already have two environments in play there.
"""

import uuid
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import (
    activate_environment,
    create_environment,
    get_environment,
    list_environments,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/environments"

# usage_intent.py spawns a background activation coroutine via this target.
# It is included in BACKGROUND_TASK_TARGETS_FULL but NOT in
# BACKGROUND_TASK_TARGETS_BASE (used by the autouse fixture in the
# agent_environments conftest). We patch it explicitly in tests that trigger
# the suspended-env activation path so it is collected rather than scheduled
# on the real event loop.
_USAGE_INTENT_BG_TARGET = (
    "app.services.environments.usage_intent.create_task_with_error_logging"
)


def _signal_usage_intent(
    client: TestClient,
    headers: dict[str, str],
    env_id: str,
) -> dict:
    """Call POST /environments/{env_id}/usage-intent and assert 200."""
    r = client.post(f"{_BASE}/{env_id}/usage-intent", headers=headers)
    assert r.status_code == 200, f"usage-intent failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 1: Happy path — running environment
# ---------------------------------------------------------------------------


def test_usage_intent_running_environment(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Happy path for usage-intent on a running environment:
      1. Create agent → auto-creates and activates default environment (running).
      2. POST usage-intent on the env → 200, status="ok", environment_id matches.
      3. Response fields are all present and correctly typed.
    """
    # ── Phase 1: Create agent → drain so env reaches "running" ───────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    env = result["data"][0]
    env_id = env["id"]
    assert env["status"] == "running"

    # ── Phase 2: Signal usage intent ─────────────────────────────────────
    resp = _signal_usage_intent(client, superuser_token_headers, env_id)

    # ── Phase 3: Verify response shape and semantics ──────────────────────
    assert resp["status"] == "ok"
    assert resp["environment_id"] == env_id
    assert isinstance(resp["message"], str)
    assert len(resp["message"]) > 0
    # The message should mention the current env status.
    assert "running" in resp["message"]


# ---------------------------------------------------------------------------
# Scenario 2: Suspended environment → triggers background activation
# ---------------------------------------------------------------------------


def test_usage_intent_suspended_environment(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Usage-intent on a suspended environment triggers background activation:
      1. Create agent → drain so env reaches "running".
      2. Suspend the environment via POST /environments/{id}/suspend.
      3. Patch the usage_intent background-task dispatcher so the activation
         coroutine is collected rather than fired on the real event loop.
      4. POST usage-intent → 200, status="activating", environment_id matches.
      5. Verify a background task was collected (activation was scheduled).
    """
    # ── Phase 1: Create agent → drain → env is "running" ─────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    env = result["data"][0]
    env_id = env["id"]
    assert env["status"] == "running"

    # ── Phase 2: Suspend the environment via API ──────────────────────────
    r = client.post(f"{_BASE}/{env_id}/suspend", headers=superuser_token_headers)
    assert r.status_code == 200, f"suspend failed: {r.text}"

    fetched = get_environment(client, superuser_token_headers, env_id)
    assert fetched["status"] == "suspended"

    # ── Phase 3 + 4: Signal usage intent; patch bg target to capture task ─
    collected_tasks = []

    def _capture_task(coro, *, task_name=""):
        collected_tasks.append((coro, task_name))

    with patch(_USAGE_INTENT_BG_TARGET, side_effect=_capture_task):
        resp = _signal_usage_intent(client, superuser_token_headers, env_id)

    # ── Phase 5: Verify response and that activation was scheduled ────────
    assert resp["status"] == "activating"
    assert resp["environment_id"] == env_id
    assert "activation" in resp["message"].lower()

    assert len(collected_tasks) == 1, (
        f"Expected 1 background activation task, got {len(collected_tasks)}: "
        f"{[name for _, name in collected_tasks]}"
    )
    task_name = collected_tasks[0][1]
    assert "activate_from_usage_intent" in task_name
    assert env_id.replace("-", "") in task_name.replace("-", "")


# ---------------------------------------------------------------------------
# Scenario 3: Resolution — non-active environment redirects to active env
# ---------------------------------------------------------------------------


def test_usage_intent_resolves_to_active_environment(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Calling usage-intent with a non-active environment resolves to the active one:
      1. Create agent → env1 is active (running).
      2. Create env2 → drain build; env1 is still active.
      3. Activate env2 → drain → env2 becomes active, env1 is not.
      4. POST usage-intent on env1 (now non-active) → response environment_id
         is env2's id (the active env), not env1's id.
    """
    # ── Phase 1: Create agent → env1 is active ───────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    env1_id = result["data"][0]["id"]
    assert result["data"][0]["is_active"] is True

    # ── Phase 2: Create env2 ──────────────────────────────────────────────
    env2 = create_environment(
        client, superuser_token_headers, agent_id,
        instance_name="Secondary",
    )
    env2_id = env2["id"]
    drain_tasks()

    # ── Phase 3: Activate env2 ────────────────────────────────────────────
    activate_environment(client, superuser_token_headers, agent_id, env2_id)
    drain_tasks()

    result = list_environments(client, superuser_token_headers, agent_id)
    env_map = {e["id"]: e for e in result["data"]}
    assert env_map[env2_id]["is_active"] is True, "env2 should be active after activation"
    assert env_map[env1_id]["is_active"] is False

    # ── Phase 4: Usage-intent on env1 → resolves to env2 ─────────────────
    resp = _signal_usage_intent(client, superuser_token_headers, env1_id)

    assert resp["status"] == "ok"
    assert resp["environment_id"] == env2_id, (
        f"Expected resolved env_id={env2_id!r} (the active env), "
        f"got {resp['environment_id']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 4: Auth + ownership guards
# ---------------------------------------------------------------------------


def test_usage_intent_auth_and_ownership(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Auth and ownership guards for usage-intent:
      1. Create agent → get env_id.
      2. Unauthenticated → 401/403.
      3. Foreign user → 403 (mirrors GET /environments/{id} ownership check).
      4. Non-existent environment id → 404.
    """
    # ── Phase 1: Create agent, get env_id ─────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    result = list_environments(client, superuser_token_headers, agent_id)
    env_id = result["data"][0]["id"]

    # ── Phase 2: Unauthenticated ──────────────────────────────────────────
    r = client.post(f"{_BASE}/{env_id}/usage-intent")
    assert r.status_code in (401, 403), (
        f"Expected 401 or 403 for unauthenticated, got {r.status_code}"
    )

    # ── Phase 3: Foreign user ─────────────────────────────────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )

    r = client.post(f"{_BASE}/{env_id}/usage-intent", headers=other_headers)
    assert r.status_code == 403, (
        f"Expected 403 for foreign user, got {r.status_code}: {r.text}"
    )
    assert "Not enough permissions" in r.json().get("detail", "")

    # Owner's environment is unaffected after the failed attempts.
    resp = _signal_usage_intent(client, superuser_token_headers, env_id)
    assert resp["status"] == "ok"

    # ── Phase 4: Non-existent environment id ─────────────────────────────
    ghost = str(uuid.uuid4())
    r = client.post(f"{_BASE}/{ghost}/usage-intent", headers=superuser_token_headers)
    assert r.status_code == 404, (
        f"Expected 404 for unknown env, got {r.status_code}: {r.text}"
    )
