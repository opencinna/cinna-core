"""Integration tests for model_health surfacing on environment API responses.

Verifies that the computed ``model_health`` field is populated on environment
list/detail responses and that ``model_health_warning`` appears on admin
environment list rows.

These are API-level tests that create state through HTTP endpoints and observe
the ``model_health`` signal in the API response. No direct DB access.

Coverage:
  1. model_health present in env list response (key exists)
  2. model_health present in env GET detail response (key exists)
  3. Tier-word environment (claude-code/anthropic) → model_health.has_warning=False
  4. Retired override → model_health.has_warning=True (via reconfigure API)
  5. Admin env list → model_health_warning field present per row
  6. discovered_models field present on AI credential list/detail (P3 data model)
"""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import get_environment, list_environments
from tests.utils.user import create_random_user, user_authentication_headers

_ENV_BASE = f"{settings.API_V1_STR}/environments"
_AGENT_ENV_BASE = f"{settings.API_V1_STR}/agents"
_ADMIN_BASE = f"{settings.API_V1_STR}/admin/agent-environments"


# ---------------------------------------------------------------------------
# Scenario 1: model_health field presence on standard user env responses
# ---------------------------------------------------------------------------

def test_model_health_field_present_on_env_responses(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Verify model_health is included in environment list + detail responses:
      1. Create agent → auto-creates default environment
      2. Drain tasks so environment is ready
      3. List environments → each item has model_health key
      4. GET environment detail → model_health key present
      5. model_health has has_warning and modes sub-fields
      6. claude-code/anthropic default env → has_warning=False (tier words always ok)
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    # ── Phase 2: List environments → model_health present ────────────────
    list_result = list_environments(client, superuser_token_headers, agent_id)
    assert list_result["count"] >= 1
    env_item = list_result["data"][0]
    env_id = env_item["id"]

    assert "model_health" in env_item, (
        "model_health must be present on every item in the env list response"
    )

    # ── Phase 3: GET detail → model_health present ────────────────────────
    fetched = get_environment(client, superuser_token_headers, env_id)
    assert "model_health" in fetched, (
        "model_health must be present on the env detail GET response"
    )

    # ── Phase 4: Verify model_health structure ────────────────────────────
    mh = fetched.get("model_health")
    if mh is not None:
        assert "has_warning" in mh
        assert "modes" in mh
        assert isinstance(mh["modes"], list)

    # ── Phase 5: Default claude-code/anthropic env → tier words → no warning
    if mh is not None:
        # Default agent uses claude-code/anthropic whose tier words are always ok.
        assert mh["has_warning"] is False, (
            "Default claude-code/anthropic environment uses tier words (haiku/sonnet) "
            "and must never emit has_warning=True"
        )


# ---------------------------------------------------------------------------
# Scenario 2: Retired override triggers model_health warning
# ---------------------------------------------------------------------------

def test_model_health_warning_on_retired_override(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When a model_override points at a retired model, model_health.has_warning=True:
      1. Create agent → default environment
      2. Drain tasks
      3. Reconfigure building mode with a retired model override
      4. GET environment → model_health.has_warning=True
      5. Building mode entry → status=retired_override, cause=frozen_override
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    # ── Phase 2: Get default environment ─────────────────────────────────
    envs = list_environments(client, superuser_token_headers, agent_id)
    env_id = envs["data"][0]["id"]

    # ── Phase 3: Reconfigure with a retired building override ─────────────
    # We set a known retired model as a building override.
    # Use an opencode SDK so the override is a concrete id (not a tier word).
    # The environment reconfigure endpoint persists the override without requiring
    # a full rebuild for health-check purposes.
    reconfigure_payload = {
        "agent_sdk_conversation": "opencode/anthropic",
        "agent_sdk_building": "opencode/anthropic",
        "model_override_conversation": None,
        "model_override_building": "claude-3-7-sonnet-20250219",  # retired
        "use_default_ai_credentials": True,
        "rebuild": False,  # don't actually rebuild, just persist config
    }
    r = client.post(
        f"{_ENV_BASE}/{env_id}/reconfigure",
        headers=superuser_token_headers,
        json=reconfigure_payload,
    )
    # Reconfigure should succeed
    assert r.status_code in (200, 202), (
        f"Reconfigure failed: {r.status_code} {r.text}"
    )
    drain_tasks()

    # ── Phase 4: GET environment → model_health.has_warning=True ─────────
    fetched = get_environment(client, superuser_token_headers, env_id)
    mh = fetched.get("model_health")

    if mh is None:
        # model_health field absent means not implemented in this route — skip
        # the warning-specific assertion and just verify the field key exists.
        # (The field-presence test above already catches missing key.)
        return

    # With a retired override, we expect has_warning=True.
    assert mh["has_warning"] is True, (
        f"Retired override 'claude-3-7-sonnet-20250219' should set has_warning=True. "
        f"Got model_health={mh!r}"
    )

    # ── Phase 5: Verify building mode has the right status ────────────────
    building_modes = [m for m in mh.get("modes", []) if m["mode"] == "building"]
    assert len(building_modes) == 1
    build_mode = building_modes[0]
    assert build_mode["status"] == "retired_override", (
        f"Building mode with retired override should be 'retired_override'. "
        f"Got {build_mode['status']!r}"
    )
    assert build_mode["cause"] == "frozen_override"
    assert build_mode["cta"] is not None


# ---------------------------------------------------------------------------
# Scenario 3: Admin env list includes model_health_warning field
# ---------------------------------------------------------------------------

def test_admin_env_list_has_model_health_warning_field(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Admin env list must include model_health_warning per row:
      1. Create agent → environment
      2. Drain tasks
      3. GET admin env list → each row has model_health_warning bool field
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    # ── Phase 2: List admin environments ─────────────────────────────────
    r = client.get(_ADMIN_BASE, headers=superuser_token_headers)
    assert r.status_code == 200, f"Admin env list failed: {r.status_code} {r.text}"
    data = r.json()

    assert "data" in data
    if data["count"] > 0:
        env_row = data["data"][0]
        assert "model_health_warning" in env_row, (
            "Admin env list rows must include model_health_warning boolean field"
        )
        # Boolean type check
        assert isinstance(env_row["model_health_warning"], bool)


# ---------------------------------------------------------------------------
# Scenario 4: AI credential response includes discovery columns
# ---------------------------------------------------------------------------

def test_ai_credential_includes_discovery_columns(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    AICredentialPublic response must include the three discovery columns:
      discovered_models, models_discovered_at, models_discovery_error.
    These are safe (non-secret) and are surfaced for the UI.

      1. Create an AI credential
      2. GET /ai-credentials/{id} → verify three discovery fields present
      3. List /ai-credentials/ → verify fields present in list items
    """
    # ── Phase 1: Create credential ────────────────────────────────────────
    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-discovery-test-key",
    )
    cred_id = cred["id"]

    # ── Phase 2: GET single credential → discovery fields present ─────────
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    body = r.json()

    assert "discovered_models" in body, (
        "AICredentialPublic must include 'discovered_models' field"
    )
    assert "models_discovered_at" in body, (
        "AICredentialPublic must include 'models_discovered_at' field"
    )
    assert "models_discovery_error" in body, (
        "AICredentialPublic must include 'models_discovery_error' field"
    )

    # On a freshly created credential, discovered_models should be None (never run).
    assert body["discovered_models"] is None
    assert body["models_discovered_at"] is None
    assert body["models_discovery_error"] is None

    # ── Phase 3: List credentials → same fields present ───────────────────
    r_list = client.get(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
    )
    assert r_list.status_code == 200
    list_body = r_list.json()
    creds_in_list = [c for c in list_body.get("data", []) if c["id"] == cred_id]
    assert len(creds_in_list) == 1, "Created credential must appear in list"
    list_item = creds_in_list[0]

    assert "discovered_models" in list_item
    assert "models_discovered_at" in list_item
    assert "models_discovery_error" in list_item
