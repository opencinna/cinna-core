"""
Router trigger prompt field tests (PATCH endpoint + generator endpoint).

Covers:
  1. PATCH /agents/{id}/router-trigger-prompt updates the agent's field and
     the response echoes it back through to_public_with_clone_info.

  2. POST /agents/{id}/generate-router-trigger-prompt returns a non-empty
     trigger_prompt string when the agent has a description (LLM mocked).

  3. POST /agents/{id}/generate-router-trigger-prompt returns a 200 with
     success=False (and an error field) when the agent has no description —
     the endpoint never 500s.

Phase 5 of docs/plans/channels_identity_unification/ deleted the
AppAgentRoute family this file used to test propagation into: PATCH
(and the generic PUT) used to sync router_trigger_prompt onto an
auto-managed route (the "M1 regression" this file's tests were originally
named for), and a PATCH on a bundle install with no auto-route used to
backfill one on demand. Saving a trigger prompt now just saves it — there
is nothing to propagate to and nothing to backfill — so those tests are
deleted with the mechanism, and what remains is the field itself: the
PATCH endpoint stays (it is still how the owner edits
router_trigger_prompt) and must stay covered.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import (
    create_random_user,
    user_authentication_headers,
)
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _publish_and_install(
    client: TestClient,
    superuser_headers: dict,
    installer_headers: dict,
    *,
    trigger_prompt: str | None,
    description: str | None = None,
) -> tuple[str, str]:
    """Publish a bundle and install it.

    Returns (bundle_id, install_id).
    """
    agent = create_agent_via_api(
        client, superuser_headers, name=f"PubAgent-{random_lower_string()[:6]}"
    )
    drain_tasks()

    if description:
        client.put(
            f"{API}/agents/{agent['id']}",
            headers=superuser_headers,
            json={"description": description},
        )

    # Set trigger prompt
    if trigger_prompt is not None:
        r = client.patch(
            f"{API}/agents/{agent['id']}/router-trigger-prompt",
            headers=superuser_headers,
            json={"router_trigger_prompt": trigger_prompt},
        )
        assert r.status_code == 200, r.text

    # Publish
    r = client.post(f"{API}/agents/{agent['id']}/publish", headers=superuser_headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(f"{API}/agents/{agent['id']}", headers=superuser_headers).json()
    client.patch(
        f"{API}/bundles/{fresh['bundle_uuid']}",
        headers=superuser_headers,
        json={"is_listed": True, "visibility": "public"},
    )

    # Install
    r = client.post(f"{API}/catalog/{fresh['bundle_id']}/install", headers=installer_headers, json={})
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()
    return fresh["bundle_id"], install["id"]


# ---------------------------------------------------------------------------
# Test 1: PATCH /router-trigger-prompt returns the updated field
# ---------------------------------------------------------------------------


def test_patch_router_trigger_prompt_returns_updated_field_in_response(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /agents/{id}/router-trigger-prompt updates
    ``Agent.router_trigger_prompt`` and returns an ``AgentPublic`` whose
    field reflects the new value — directly checking it surfaces through
    ``AgentService.to_public_with_clone_info``. There is nothing left to
    propagate to (no auto-managed route), so the field update is the whole
    of the contract now.
    """
    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    original_trigger = "Automate weekly status reports and summaries"
    _, install_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        trigger_prompt=original_trigger,
    )

    new_trigger = "Generate and distribute weekly status reports automatically"
    r = client.patch(
        f"{API}/agents/{install_id}/router-trigger-prompt",
        headers=installer_headers,
        json={"router_trigger_prompt": new_trigger},
    )
    assert r.status_code == 200, f"PATCH failed: {r.text}"
    body = r.json()
    assert body["router_trigger_prompt"] == new_trigger, (
        f"PATCH response must echo updated router_trigger_prompt. "
        f"Got {body.get('router_trigger_prompt')!r}"
    )

    # Verify it persisted, not just echoed in the response.
    fresh = client.get(f"{API}/agents/{install_id}", headers=installer_headers).json()
    assert fresh["router_trigger_prompt"] == new_trigger


# ---------------------------------------------------------------------------
# Test 2: Generator endpoint returns non-empty prompt (LLM mocked)
# ---------------------------------------------------------------------------


def test_generate_router_trigger_prompt_returns_non_empty_string(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /agents/{id}/generate-router-trigger-prompt returns success=True
    and a non-empty trigger_prompt string. The LLM call is mocked.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Generator Test Agent"
    )
    drain_tasks()
    agent_id = agent["id"]

    # Set a description so the endpoint has input to generate from
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"description": "Plans and manages team meetings using Google Calendar integration"},
    )
    assert r.status_code == 200, r.text

    mock_result = {
        "success": True,
        "trigger_prompt": "Plan and manage team meetings using the calendar integration",
    }

    with patch(
        "app.services.ai_functions.ai_functions_service.AIFunctionsService.generate_router_trigger_prompt",
        return_value=mock_result,
    ):
        r = client.post(
            f"{API}/agents/{agent_id}/generate-router-trigger-prompt",
            headers=superuser_token_headers,
        )

    assert r.status_code == 200, f"Generator endpoint failed: {r.text}"
    body = r.json()
    assert body["success"] is True
    assert "trigger_prompt" in body
    assert isinstance(body["trigger_prompt"], str)
    assert len(body["trigger_prompt"]) > 0, "trigger_prompt must not be empty"


# ---------------------------------------------------------------------------
# Test 3: Generator returns 200 + success=False when no description
# ---------------------------------------------------------------------------


def test_generate_router_trigger_prompt_no_description_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the agent has no description, the generator endpoint returns
    HTTP 200 with success=False and an error message (not a 5xx).
    The endpoint validates agent ownership before checking description.
    """
    # Create agent with NO description set
    agent = create_agent_via_api(
        client, superuser_token_headers, name="No Description Agent"
    )
    drain_tasks()
    agent_id = agent["id"]

    # Clear description explicitly (it may default to None)
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"description": ""},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"{API}/agents/{agent_id}/generate-router-trigger-prompt",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["success"] is False, (
        f"Expected success=False for agent with no description, got {body!r}"
    )
    assert "error" in body
    assert body["error"]  # non-empty error message
