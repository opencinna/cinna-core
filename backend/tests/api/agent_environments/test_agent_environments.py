"""
Integration tests for the agent-environments API.

Five scenario-based tests covering the full surface:
  1. Lifecycle     — CRUD, field verification, delete/404
  2. Multi-image   — multiple env_name templates, count and name checks
  3. Activation    — activate second environment, active flag flips
  4. Auth/ownership — unauthenticated, other-user, and non-existent-ID guards
  5. Defaults       — default field values on auto-created environment
"""

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import (
    activate_environment,
    create_environment,
    delete_environment,
    get_environment,
    list_environments,
    update_environment,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/environments"


# ---------------------------------------------------------------------------
# Scenario 1: Full CRUD lifecycle
# ---------------------------------------------------------------------------

def test_environment_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full CRUD lifecycle for environments:
      1.  Create agent — auto-creates default environment
      2.  List environments → default env present with expected fields
      3.  GET by ID → all AgentEnvironmentPublic fields present
      4.  Update instance_name → change persisted
      5.  Verify update via GET
      6.  Delete environment
      7.  Verify it returns 404
    """
    # ── Phase 1: Create agent (auto-creates default environment) ──────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    # Drain background tasks so the environment build completes and is activated.
    drain_tasks()

    # ── Phase 2: List → default environment present ───────────────────────
    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    envs = result["data"]
    assert len(envs) == 1

    default_env = envs[0]
    assert default_env["env_name"] == settings.DEFAULT_AGENT_ENV_NAME
    assert default_env["status"] == "running"
    assert default_env["is_active"] is True
    assert default_env["agent_id"] == agent_id

    env_id = default_env["id"]

    # ── Phase 3: GET by ID → all fields present ───────────────────────────
    fetched = get_environment(client, superuser_token_headers, env_id)
    assert fetched["id"] == env_id
    assert fetched["agent_id"] == agent_id
    assert fetched["env_name"] == settings.DEFAULT_AGENT_ENV_NAME
    assert "env_version" in fetched
    assert "instance_name" in fetched
    assert "type" in fetched
    assert "status" in fetched
    assert "is_active" in fetched
    assert "created_at" in fetched
    assert "updated_at" in fetched
    assert "agent_sdk_conversation" in fetched
    assert "agent_sdk_building" in fetched
    assert "use_default_ai_credentials" in fetched
    assert "conversation_ai_credential_id" in fetched
    assert "building_ai_credential_id" in fetched

    # ── Phase 4: Update instance_name ────────────────────────────────────
    updated = update_environment(
        client, superuser_token_headers, env_id, instance_name="Production"
    )
    assert updated["instance_name"] == "Production"
    assert updated["id"] == env_id

    # ── Phase 5: Verify update persisted via GET ──────────────────────────
    re_fetched = get_environment(client, superuser_token_headers, env_id)
    assert re_fetched["instance_name"] == "Production"

    # ── Phase 6: Delete environment ───────────────────────────────────────
    deleted = delete_environment(client, superuser_token_headers, env_id)
    assert "message" in deleted

    # ── Phase 7: Verify gone → 404 ───────────────────────────────────────
    r = client.get(f"{_BASE}/{env_id}", headers=superuser_token_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 2: Multi-image environment templates
# ---------------------------------------------------------------------------

def test_multi_image_environment_templates(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Multiple environment templates for a single agent:
      1.  Create agent → default python-env-advanced environment
      2.  List → env_name matches DEFAULT_AGENT_ENV_NAME
      3.  Set up general-env template directory
      4.  Create second environment with env_name="general-env"
      5.  List → count=2, both template names present
      6.  GET each environment → env_name matches creation params
    """
    # ── Phase 1: Create agent → default environment ───────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: List → default env_name ─────────────────────────────────
    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    assert result["data"][0]["env_name"] == settings.DEFAULT_AGENT_ENV_NAME

    first_env_id = result["data"][0]["id"]

    # ── Phase 3: Set up general-env template directory ────────────────────
    lm = patch_environment_adapter
    general_template = lm.templates_dir / "general-env"
    general_template.mkdir(parents=True, exist_ok=True)
    (general_template / "docker-compose.template.yml").write_text(
        "version: '3'\nservices:\n  agent:\n    image: test-general\n"
        "    ports:\n      - '${AGENT_PORT}:8000'\n"
    )

    # ── Phase 4: Create second environment ───────────────────────────────
    second_env = create_environment(
        client, superuser_token_headers, agent_id,
        env_name="general-env",
        instance_name="General Purpose",
    )
    assert second_env["env_name"] == "general-env"
    assert second_env["instance_name"] == "General Purpose"

    second_env_id = second_env["id"]

    # ── Phase 5: List → count=2, both names present ───────────────────────
    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 2
    env_names = {e["env_name"] for e in result["data"]}
    assert settings.DEFAULT_AGENT_ENV_NAME in env_names
    assert "general-env" in env_names

    # ── Phase 6: GET each → env_name matches ─────────────────────────────
    first = get_environment(client, superuser_token_headers, first_env_id)
    assert first["env_name"] == settings.DEFAULT_AGENT_ENV_NAME

    second = get_environment(client, superuser_token_headers, second_env_id)
    assert second["env_name"] == "general-env"


# ---------------------------------------------------------------------------
# Scenario 3: Environment activation flow
# ---------------------------------------------------------------------------

def test_environment_activation_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Activation flips the active flag between environments:
      1.  Create agent → env1 is active
      2.  Create second environment (env2)
      3.  Confirm env1 is active via list
      4.  Activate env2
      5.  List → env2 is active, env1 is not
    """
    # ── Phase 1: Create agent → env1 is active ───────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    # Drain so the default env build completes and env1 becomes active.
    drain_tasks()

    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    env1 = result["data"][0]
    env1_id = env1["id"]
    assert env1["is_active"] is True

    # ── Phase 2: Create second environment ───────────────────────────────
    env2 = create_environment(
        client, superuser_token_headers, agent_id,
        instance_name="Secondary",
    )
    env2_id = env2["id"]
    # Drain the env2 build task (no auto_start, so is_active stays False).
    drain_tasks()

    # ── Phase 3: Confirm env1 is still active ─────────────────────────────
    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 2
    env_map = {e["id"]: e for e in result["data"]}
    assert env_map[env1_id]["is_active"] is True
    assert env_map[env2_id]["is_active"] is False

    # ── Phase 4: Activate env2 ────────────────────────────────────────────
    updated_agent = activate_environment(
        client, superuser_token_headers, agent_id, env2_id
    )
    assert updated_agent["id"] == agent_id
    assert updated_agent["active_environment_id"] == env2_id
    # Drain so the background _activate_environment_background task runs and
    # flips is_active flags and starts the environment.
    drain_tasks()

    # ── Phase 5: List → env2 active, env1 not active ─────────────────────
    result = list_environments(client, superuser_token_headers, agent_id)
    env_map = {e["id"]: e for e in result["data"]}
    assert env_map[env2_id]["is_active"] is True
    assert env_map[env1_id]["is_active"] is False


# ---------------------------------------------------------------------------
# Scenario 4: Auth guards and ownership checks
# ---------------------------------------------------------------------------

def test_environment_auth_and_ownership(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Auth guards and ownership enforcement:
      1.  Create agent, get env_id
      2.  Unauthenticated GET → 401/403
      3.  Other user GET → 403 (not enough permissions)
      4.  Other user PATCH → 403
      5.  Other user DELETE → 403
      6.  Non-existent env GET → 404
      7.  Non-existent env PATCH → 404
      8.  Non-existent env DELETE → 404
    """
    # ── Phase 1: Create agent and get env_id ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    result = list_environments(client, superuser_token_headers, agent_id)
    env_id = result["data"][0]["id"]

    # ── Phase 2: Unauthenticated GET → 401/403 ────────────────────────────
    r = client.get(f"{_BASE}/{env_id}")
    assert r.status_code in (401, 403)

    # ── Phase 3–5: Other user → 403 on GET, PATCH, DELETE ────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )

    r = client.get(f"{_BASE}/{env_id}", headers=other_headers)
    assert r.status_code == 403

    r = client.patch(
        f"{_BASE}/{env_id}",
        headers=other_headers,
        json={"instance_name": "Hacked"},
    )
    assert r.status_code == 403

    r = client.delete(f"{_BASE}/{env_id}", headers=other_headers)
    assert r.status_code == 403

    # Owner's environment is still intact
    get_environment(client, superuser_token_headers, env_id)

    # ── Phase 6–8: Non-existent ID → 404 ─────────────────────────────────
    ghost = str(uuid.uuid4())

    r = client.get(f"{_BASE}/{ghost}", headers=superuser_token_headers)
    assert r.status_code == 404

    r = client.patch(
        f"{_BASE}/{ghost}",
        headers=superuser_token_headers,
        json={"instance_name": "Ghost"},
    )
    assert r.status_code == 404

    r = client.delete(f"{_BASE}/{ghost}", headers=superuser_token_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 5: Default field values
# ---------------------------------------------------------------------------

def test_environment_default_values(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Verify default field values on the auto-created environment:
      1.  Create agent
      2.  List → get auto-created environment
      3.  Verify: env_version="1.0.0", type="docker",
          use_default_ai_credentials=True,
          conversation_ai_credential_id=None,
          building_ai_credential_id=None,
          status_message is None or str,
          last_health_check=None
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: List → get auto-created environment ──────────────────────
    result = list_environments(client, superuser_token_headers, agent_id)
    assert result["count"] == 1
    env = result["data"][0]
    env_id = env["id"]

    # ── Phase 3: Verify defaults ──────────────────────────────────────────
    fetched = get_environment(client, superuser_token_headers, env_id)

    assert fetched["env_version"] == "1.0.0"
    assert fetched["type"] == "docker"
    assert fetched["use_default_ai_credentials"] is True
    assert fetched["conversation_ai_credential_id"] is None
    assert fetched["building_ai_credential_id"] is None
    assert fetched["last_health_check"] is None

    # status_message may be None or a string — must not be absent
    assert "status_message" in fetched
    assert fetched["status_message"] is None or isinstance(fetched["status_message"], str)


# ---------------------------------------------------------------------------
# Scenario 6: Exception hierarchy and 403 permission denied
# ---------------------------------------------------------------------------

def test_environment_permission_denied_returns_403(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Permission denied on environment endpoints returns 403, not 400.

    Verifies the EnvironmentPermissionDeniedError status code change across
    all endpoints that perform ownership checks.
    """
    # ── Setup: Create agent and get env_id ───────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    result = list_environments(client, superuser_token_headers, agent_id)
    env_id = result["data"][0]["id"]

    # Create a second user who does not own the agent
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )

    # ── Verify 403 on all read/write environment endpoints ────────────────
    r = client.get(f"{_BASE}/{env_id}", headers=other_headers)
    assert r.status_code == 403
    assert "Not enough permissions" in r.json().get("detail", "")

    r = client.patch(
        f"{_BASE}/{env_id}",
        headers=other_headers,
        json={"instance_name": "Hacked"},
    )
    assert r.status_code == 403

    r = client.delete(f"{_BASE}/{env_id}", headers=other_headers)
    assert r.status_code == 403

    r = client.post(f"{_BASE}/{env_id}/start", headers=other_headers)
    assert r.status_code == 403

    r = client.post(f"{_BASE}/{env_id}/stop", headers=other_headers)
    assert r.status_code == 403

    r = client.post(f"{_BASE}/{env_id}/suspend", headers=other_headers)
    assert r.status_code == 403

    r = client.post(f"{_BASE}/{env_id}/restart", headers=other_headers)
    assert r.status_code == 403

    r = client.post(f"{_BASE}/{env_id}/rebuild", headers=other_headers)
    assert r.status_code == 403

    r = client.get(f"{_BASE}/{env_id}/status", headers=other_headers)
    assert r.status_code == 403

    r = client.get(f"{_BASE}/{env_id}/health", headers=other_headers)
    assert r.status_code == 403

    r = client.get(f"{_BASE}/{env_id}/logs", headers=other_headers)
    assert r.status_code == 403

    # ── Verify owner (superuser) can still access the environment ─────────
    r = client.get(f"{_BASE}/{env_id}", headers=superuser_token_headers)
    assert r.status_code == 200


def test_environment_exception_classes() -> None:
    """
    Unit test for the AgentEnvironmentError exception hierarchy.

    Verifies correct status codes and message attributes on each exception class.
    """
    from app.services.environments.environment_service import (
        AgentEnvironmentError,
        EnvironmentNotFoundError,
        AgentNotFoundError,
        EnvironmentPermissionDeniedError,
        EnvironmentCredentialError,
    )

    # Base class
    err = AgentEnvironmentError("something went wrong", status_code=400)
    assert err.status_code == 400
    assert err.message == "something went wrong"
    assert str(err) == "something went wrong"

    # Default status codes
    err = AgentEnvironmentError("oops")
    assert err.status_code == 400

    # EnvironmentNotFoundError
    err = EnvironmentNotFoundError()
    assert err.status_code == 404
    assert err.message == "Environment not found"
    assert isinstance(err, AgentEnvironmentError)

    err = EnvironmentNotFoundError("custom not found message")
    assert err.status_code == 404
    assert err.message == "custom not found message"

    # AgentNotFoundError
    err = AgentNotFoundError()
    assert err.status_code == 404
    assert err.message == "Agent not found"
    assert isinstance(err, AgentEnvironmentError)

    # EnvironmentPermissionDeniedError
    err = EnvironmentPermissionDeniedError()
    assert err.status_code == 403
    assert err.message == "Not enough permissions"
    assert isinstance(err, AgentEnvironmentError)

    # EnvironmentCredentialError
    err = EnvironmentCredentialError("Missing API key")
    assert err.status_code == 400
    assert err.message == "Missing API key"
    assert isinstance(err, AgentEnvironmentError)
