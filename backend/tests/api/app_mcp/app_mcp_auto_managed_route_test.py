"""
Tests for auto-managed App MCP route semantics.

Covers:
  1. B1 trust boundary — POST /agents/{id}/app-mcp-routes/ with
     ``is_auto_managed=true`` in the body MUST NOT create an auto-managed
     route; the field is silently ignored and AppAgentRoute.is_auto_managed
     ends up False.

  2. B1 trust boundary — POST /admin/app-agent-routes/ with
     ``is_auto_managed=true`` also produces is_auto_managed=False.

  3. Manual PUT on the agent-scoped endpoint flips is_auto_managed from
     True to False (the "user edited the route" flow).

  4. Conflict detection endpoint:
       - Returns a match when a similar route exists for the same user.
       - Returns empty when no similar route exists.
       - Threshold boundary: Jaccard similarity just at/above 0.45
         triggers a match; well below 0.45 does not.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_ADMIN_BASE = f"{API}/admin/app-agent-routes"


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


def _create_route_via_agent_endpoint(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    trigger_prompt: str,
    extra_fields: dict | None = None,
) -> dict:
    payload: dict = {
        "name": f"route-{random_lower_string()[:8]}",
        "agent_id": agent_id,
        "trigger_prompt": trigger_prompt,
        "session_mode": "conversation",
        "channel_app_mcp": True,
        "is_active": True,
        "auto_enable_for_users": False,
        "assigned_user_ids": [],
        "activate_for_myself": True,
    }
    if extra_fields:
        payload.update(extra_fields)
    r = client.post(
        f"{API}/agents/{agent_id}/app-mcp-routes/",
        headers=headers,
        json=payload,
    )
    return r


def _create_route_via_admin_endpoint(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    trigger_prompt: str,
    extra_fields: dict | None = None,
) -> dict:
    payload: dict = {
        "name": f"admin-route-{random_lower_string()[:8]}",
        "agent_id": agent_id,
        "trigger_prompt": trigger_prompt,
        "session_mode": "conversation",
        "channel_app_mcp": True,
        "is_active": True,
        "auto_enable_for_users": False,
        "assigned_user_ids": [],
    }
    if extra_fields:
        payload.update(extra_fields)
    r = client.post(_ADMIN_BASE + "/", headers=headers, json=payload)
    return r


def _publish_and_install(
    client: TestClient,
    superuser_headers: dict,
    installer_headers: dict,
    *,
    trigger_prompt: str,
) -> tuple[str, str]:
    """Publish a bundle and install it. Returns (bundle_id, install_id)."""
    agent = create_agent_via_api(client, superuser_headers, name=f"Pub-{random_lower_string()[:6]}")
    drain_tasks()

    # Set trigger prompt
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=superuser_headers,
        json={"router_trigger_prompt": trigger_prompt},
    )
    assert r.status_code == 200, r.text

    # Publish
    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=superuser_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(f"{API}/agents/{agent['id']}", headers=superuser_headers).json()
    bundle_id = fresh["bundle_id"]
    bundle_uuid = fresh["bundle_uuid"]

    # Make public
    client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_headers,
        json={"is_listed": True, "visibility": "public"},
    )

    # Install
    r = client.post(f"{API}/catalog/{bundle_id}/install", headers=installer_headers, json={})
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()
    return bundle_id, install["id"]


# ---------------------------------------------------------------------------
# B1: POST via agent-scoped endpoint — is_auto_managed is ignored
# ---------------------------------------------------------------------------


def test_b1_agent_endpoint_ignores_is_auto_managed_in_body(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /agents/{id}/app-mcp-routes/ with is_auto_managed=true in the body
    must NOT create an auto-managed route.

    The AppAgentRouteCreate schema intentionally omits is_auto_managed so the
    JSON field is simply discarded and the DB row ends up with is_auto_managed=False.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="B1 Agent Endpoint Test")
    drain_tasks()
    agent_id = agent["id"]

    # Pass is_auto_managed: true — the schema must silently ignore it.
    r = _create_route_via_agent_endpoint(
        client,
        superuser_token_headers,
        agent_id,
        trigger_prompt="Manage sales pipeline stages",
        extra_fields={"is_auto_managed": True},  # should be ignored
    )
    assert r.status_code == 200, f"Route creation failed: {r.text}"
    route = r.json()

    # The resulting route must have is_auto_managed=False.
    assert route["is_auto_managed"] is False, (
        f"B1 regression: is_auto_managed must be False after user POST, "
        f"got {route['is_auto_managed']!r}"
    )


# ---------------------------------------------------------------------------
# B1: POST via admin endpoint — is_auto_managed is ignored
# ---------------------------------------------------------------------------


def test_b1_admin_endpoint_ignores_is_auto_managed_in_body(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /admin/app-agent-routes/ with is_auto_managed=true in the body
    must NOT create an auto-managed route; the field is discarded.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="B1 Admin Endpoint Test")
    drain_tasks()
    agent_id = agent["id"]

    r = _create_route_via_admin_endpoint(
        client,
        superuser_token_headers,
        agent_id,
        trigger_prompt="Coordinate team projects",
        extra_fields={"is_auto_managed": True},  # should be ignored
    )
    assert r.status_code == 200, f"Admin route creation failed: {r.text}"
    route = r.json()

    assert route["is_auto_managed"] is False, (
        f"B1 regression: admin POST with is_auto_managed=true in body "
        f"must produce is_auto_managed=False, got {route['is_auto_managed']!r}"
    )


# ---------------------------------------------------------------------------
# Manual edit flips is_auto_managed → False
# ---------------------------------------------------------------------------


def test_put_on_auto_managed_route_flips_flag_to_false(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A PUT on the agent-scoped endpoint flips is_auto_managed from True to
    False, protecting the route from future apply-update overwrites.

      1. Create an auto-managed route (simulated via publish + install).
      2. Verify route starts as is_auto_managed=True.
      3. PUT with a new trigger_prompt.
      4. Verify is_auto_managed is now False.
    """
    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    # Install a bundle that creates an auto-managed route
    _, install_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        trigger_prompt="Process invoices and payment requests",
    )

    # ── Phase 2: Verify auto-managed state ────────────────────────────────
    routes = client.get(
        f"{API}/agents/{install_id}/app-mcp-routes/",
        headers=installer_headers,
    ).json()
    assert len(routes) == 1, f"Expected 1 route, got {routes}"
    route = routes[0]
    assert route["is_auto_managed"] is True
    route_id = route["id"]

    # ── Phase 3: PUT with new trigger_prompt ───────────────────────────────
    r = client.put(
        f"{API}/agents/{install_id}/app-mcp-routes/{route_id}",
        headers=installer_headers,
        json={"trigger_prompt": "Handle invoices, payments, and billing queries"},
    )
    assert r.status_code == 200, f"PUT failed: {r.text}"
    updated = r.json()

    # ── Phase 4: is_auto_managed flipped to False ──────────────────────────
    assert updated["is_auto_managed"] is False, (
        f"PUT must flip is_auto_managed to False, got {updated['is_auto_managed']!r}"
    )
    assert updated["trigger_prompt"] == "Handle invoices, payments, and billing queries"


# ---------------------------------------------------------------------------
# Conflict detection — match found
# ---------------------------------------------------------------------------


def test_conflict_detection_returns_match_for_similar_route(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/{id}/app-mcp-routes/conflicts returns a match when a
    similar route already exists for the same user.

    Strategy: install two bundles with highly overlapping trigger prompts.
    The first one's auto-managed route should appear as a conflict when
    checking the second.
    """
    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    # Install bundle A — "schedule meetings and calendar events"
    _, install_a_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        trigger_prompt="schedule meetings calendar events reminders",
    )

    # Install bundle B — deliberately similar trigger
    agent2 = create_agent_via_api(
        client, superuser_token_headers, name=f"B-{random_lower_string()[:6]}"
    )
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent2['id']}/router-trigger-prompt",
        headers=superuser_token_headers,
        json={"router_trigger_prompt": "schedule calendar meetings reminders events"},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"{API}/agents/{agent2['id']}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh2 = client.get(f"{API}/agents/{agent2['id']}", headers=superuser_token_headers).json()
    client.patch(
        f"{API}/bundles/{fresh2['bundle_uuid']}",
        headers=superuser_token_headers,
        json={"is_listed": True, "visibility": "public"},
    )
    r = client.post(
        f"{API}/catalog/{fresh2['bundle_id']}/install",
        headers=installer_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    install_b = r.json()
    drain_tasks()
    install_b_id = install_b["id"]

    # ── Check conflicts for install B ─────────────────────────────────────
    r = client.get(
        f"{API}/agents/{install_b_id}/app-mcp-routes/conflicts",
        headers=installer_headers,
    )
    assert r.status_code == 200, f"Conflicts endpoint failed: {r.text}"
    conflict_data = r.json()
    assert "matches" in conflict_data
    # install A's route is similar → must appear as a conflict
    assert len(conflict_data["matches"]) >= 1, (
        f"Expected at least 1 conflict match, got: {conflict_data}"
    )
    match = conflict_data["matches"][0]
    assert "route_id" in match
    assert "similarity" in match
    assert match["similarity"] >= 0.45


# ---------------------------------------------------------------------------
# Conflict detection — no match for dissimilar route
# ---------------------------------------------------------------------------


def test_conflict_detection_returns_empty_for_dissimilar_route(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/{id}/app-mcp-routes/conflicts returns no matches when no
    similar route exists.

    We install two bundles whose trigger prompts share zero tokens — the
    Jaccard similarity is 0.0, well below the 0.45 threshold.
    """
    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    # Bundle A: cooking-domain
    _, _ = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        trigger_prompt="suggest baking recipes and cooking techniques",
    )

    # Bundle B: entirely different domain (legal)
    agent2 = create_agent_via_api(
        client, superuser_token_headers, name=f"Legal-{random_lower_string()[:6]}"
    )
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent2['id']}/router-trigger-prompt",
        headers=superuser_token_headers,
        json={"router_trigger_prompt": "draft contracts review legal documents compliance"},
    )
    assert r.status_code == 200, r.text

    r = client.post(f"{API}/agents/{agent2['id']}/publish", headers=superuser_token_headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh2 = client.get(f"{API}/agents/{agent2['id']}", headers=superuser_token_headers).json()
    client.patch(
        f"{API}/bundles/{fresh2['bundle_uuid']}",
        headers=superuser_token_headers,
        json={"is_listed": True, "visibility": "public"},
    )
    r = client.post(f"{API}/catalog/{fresh2['bundle_id']}/install", headers=installer_headers, json={})
    assert r.status_code == 200, r.text
    install_b = r.json()
    drain_tasks()
    install_b_id = install_b["id"]

    # ── Conflicts endpoint ─────────────────────────────────────────────────
    r = client.get(
        f"{API}/agents/{install_b_id}/app-mcp-routes/conflicts",
        headers=installer_headers,
    )
    assert r.status_code == 200, f"Conflicts endpoint failed: {r.text}"
    conflict_data = r.json()
    assert "matches" in conflict_data
    # Dissimilar domains → no conflict
    assert conflict_data["matches"] == [], (
        f"Expected no conflicts for unrelated prompts, got: {conflict_data['matches']}"
    )


# ---------------------------------------------------------------------------
# Conflict detection — threshold boundary
# ---------------------------------------------------------------------------
# Unit tests for the Jaccard similarity helpers (tokens_for_similarity /
# jaccard_similarity, the boundary arithmetic) live in
# tests/unit/test_text_similarity.py. The helpers themselves live in
# app/services/routing/text_similarity.py -- shared with reachability near-miss
# ranking, which is why they are not on AppAgentRouteService. The endpoint-level
# conflict match/empty behavior is covered by the other tests in this file.
