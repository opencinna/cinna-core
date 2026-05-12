"""
Router trigger prompt propagation tests (M1 regression + generator endpoint).

Covers:
  1. PATCH /agents/{id}/router-trigger-prompt updates the agent's field and
     propagates to the auto-managed AppAgentRoute.

  2. PUT /agents/{id} (generic update) propagates router_trigger_prompt to
     the auto-managed route.

  3. POST /agents/{id}/generate-router-trigger-prompt returns a non-empty
     trigger_prompt string when the agent has a description (LLM mocked).

  4. POST /agents/{id}/generate-router-trigger-prompt returns a 200 with
     success=False (and an error field) when the agent has no description —
     the endpoint never 500s.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import (
    create_random_user,
    promote_to_developer,
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
    trigger_prompt: str,
    description: str | None = None,
) -> tuple[str, str]:
    """Publish a bundle (with trigger prompt) and install it.

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


def _list_agent_routes(
    client: TestClient, headers: dict, agent_id: str
) -> list[dict]:
    r = client.get(
        f"{API}/agents/{agent_id}/app-mcp-routes/",
        headers=headers,
    )
    assert r.status_code == 200, f"List routes failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Test 1: PATCH /router-trigger-prompt propagates to auto-managed route
# ---------------------------------------------------------------------------


def test_patch_router_trigger_prompt_propagates_to_auto_managed_route(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /agents/{id}/router-trigger-prompt updates Agent.router_trigger_prompt
    and syncs the new value to the matching auto-managed AppAgentRoute.

      1. Publish + install bundle with trigger prompt → auto route created.
      2. PATCH the install's trigger prompt with a new value.
      3. Agent row reflects new value.
      4. Auto-managed route's trigger_prompt also updated.
    """
    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    original_trigger = "Help plan travel itineraries and book flights"
    _, install_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        trigger_prompt=original_trigger,
    )

    # Verify the auto-managed route has the original trigger
    routes = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes) == 1
    assert routes[0]["is_auto_managed"] is True
    route_id = routes[0]["id"]
    assert routes[0]["trigger_prompt"] == original_trigger

    # ── PATCH with new value ───────────────────────────────────────────────
    new_trigger = "Plan travel itineraries, hotel bookings, and flight reservations"
    r = client.patch(
        f"{API}/agents/{install_id}/router-trigger-prompt",
        headers=installer_headers,
        json={"router_trigger_prompt": new_trigger},
    )
    assert r.status_code == 200, f"PATCH failed: {r.text}"
    # PATCH response carries the updated AgentPublic — verify the
    # router_trigger_prompt round-trips through to_public_with_clone_info.
    patch_body = r.json()
    assert patch_body["router_trigger_prompt"] == new_trigger

    # ── Route updated ──────────────────────────────────────────────────────
    routes_after = _list_agent_routes(client, installer_headers, install_id)
    matching = [rt for rt in routes_after if rt["id"] == route_id]
    assert len(matching) == 1
    assert matching[0]["trigger_prompt"] == new_trigger, (
        f"Auto-managed route trigger_prompt must be updated via PATCH endpoint. "
        f"Got {matching[0]['trigger_prompt']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: PUT /agents/{id} propagates router_trigger_prompt (M1 regression)
# ---------------------------------------------------------------------------


def test_generic_put_propagates_router_trigger_prompt_to_route(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Updating router_trigger_prompt via the generic PUT /agents/{id} endpoint
    updates Agent.router_trigger_prompt and propagates it to the
    auto-managed AppAgentRoute.

    M1 regression: AgentService.update_agent must call
    AppAgentRouteService.sync_router_trigger_prompt_from_agent when
    router_trigger_prompt appears in the update dict.

      1. Promote a developer + install a bundle for them → auto-managed
         route created (foreign install path).
      2. Use ``PUT /agents/{id}`` to update ``router_trigger_prompt`` on
         the install (PUT requires the developer role).
      3. Response carries the new ``router_trigger_prompt`` (round-trips
         through ``to_public_with_clone_info``).
      4. Auto-managed route's ``trigger_prompt`` is synced to the same
         value via ``AgentService.update_agent`` → ``AppAgentRouteService
         .sync_router_trigger_prompt_from_agent``.
    """
    installer = create_random_user(client)
    installer_headers = user_authentication_headers(
        client=client, email=installer["email"], password=installer["_password"]
    )
    create_random_ai_credential(client, installer_headers, set_default=True)
    # PUT /agents/{id} is gated on require_developer; promote so we can
    # exercise the actual production update path.
    promote_to_developer(client, superuser_token_headers, installer["id"])

    original_trigger = "Generate monthly financial reports"
    _, install_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        trigger_prompt=original_trigger,
    )

    # Verify route starts with the original trigger
    routes = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes) == 1
    assert routes[0]["trigger_prompt"] == original_trigger
    assert routes[0]["is_auto_managed"] is True
    route_id = routes[0]["id"]

    # ── Generic PUT updates agent + propagates to auto-managed route ─────
    new_trigger = "Compile and deliver monthly financial reports with trend analysis"
    r = client.put(
        f"{API}/agents/{install_id}",
        headers=installer_headers,
        json={"router_trigger_prompt": new_trigger},
    )
    assert r.status_code == 200, f"PUT failed: {r.text}"
    put_body = r.json()
    assert put_body["router_trigger_prompt"] == new_trigger, (
        f"PUT response must echo updated router_trigger_prompt. "
        f"Got {put_body.get('router_trigger_prompt')!r}"
    )

    # ── Auto-managed route also updated (the M1 regression check) ────────
    routes_after = _list_agent_routes(client, installer_headers, install_id)
    matching = [rt for rt in routes_after if rt["id"] == route_id]
    assert len(matching) == 1
    assert matching[0]["trigger_prompt"] == new_trigger, (
        f"M1 regression: generic PUT must sync auto-managed route "
        f"trigger_prompt. Got {matching[0]['trigger_prompt']!r}"
    )
    assert matching[0]["is_auto_managed"] is True


# ---------------------------------------------------------------------------
# Test 2b: Generic PUT /agents/{id} propagates router_trigger_prompt (M1)
# ---------------------------------------------------------------------------


def test_patch_router_trigger_prompt_returns_updated_field_in_response(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /agents/{id}/router-trigger-prompt returns an AgentPublic whose
    ``router_trigger_prompt`` reflects the new value. Complements Test 1
    (which asserts the route side-effect) by directly checking the field
    surfaces through ``AgentService.to_public_with_clone_info``.
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

    routes = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes) == 1
    assert routes[0]["is_auto_managed"] is True
    route_id = routes[0]["id"]

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

    # Side-effect check — keeps this test self-contained.
    routes_after = _list_agent_routes(client, installer_headers, install_id)
    matching = [rt for rt in routes_after if rt["id"] == route_id]
    assert len(matching) == 1
    assert matching[0]["trigger_prompt"] == new_trigger


# ---------------------------------------------------------------------------
# Test 3: Generator endpoint returns non-empty prompt (LLM mocked)
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
# Test 4: Generator returns 200 + success=False when no description
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
