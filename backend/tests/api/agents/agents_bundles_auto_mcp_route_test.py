"""
Auto App MCP Route creation and propagation via bundle install/update.

Covers (Phase 3 + 4 of installed_agents_auto_app_mcp_routing plan):

  1. Install with a non-empty ``router_trigger_prompt`` creates an
     ``AppAgentRoute`` (is_auto_managed=True, session_mode="conversation",
     channel_app_mcp=True, is_active=True, name=agent.name) and a
     self-assignment (is_enabled=True) for the installer.

  2. Install whose revision has an empty ``router_trigger_prompt`` succeeds
     but does NOT create a route; Agent.last_update_status reflects the
     degraded state.

  3. Route-creation error does NOT abort the install (best-effort guard).

  4. Apply-update with a new ``router_trigger_prompt`` refreshes the
     auto-managed route's ``trigger_prompt`` for installs where
     is_auto_managed=True.

  5. When the user has manually edited the route via PUT
     (is_auto_managed is now False), apply-update leaves the route alone.

  6. Legacy install (no route exists) — apply-update creates a new route
     unless a user-edited (is_auto_managed=False) route already exists for
     the same agent.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a fresh user with a default AI credential and return (user, headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _publish(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    notes: str | None = None,
    visibility: str = "public",
    is_listed: bool = True,
    router_trigger_prompt: str | None = None,
) -> dict:
    """Update agent fields, publish, and make the bundle catalog-visible.

    Setting ``router_trigger_prompt`` before publish ensures the snapshot
    carries the value into the revision's ``router_trigger_prompt`` column.
    """
    if router_trigger_prompt is not None:
        # PATCH the new focused endpoint
        r = client.patch(
            f"{API}/agents/{agent_id}/router-trigger-prompt",
            headers=headers,
            json={"router_trigger_prompt": router_trigger_prompt},
        )
        assert r.status_code == 200, f"Setting router_trigger_prompt failed: {r.text}"

    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == 200, r.text
    revision = r.json()
    drain_tasks()

    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_uuid is not None
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": is_listed, "visibility": visibility},
    )
    assert r.status_code == 200, r.text
    return revision


def _install(
    client: TestClient,
    headers: dict,
    bundle_id: str,
) -> dict:
    """Install a bundle from the catalog and return the install dict."""
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json={},
    )
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()
    return install


def _list_agent_routes(
    client: TestClient,
    headers: dict,
    agent_id: str,
) -> list[dict]:
    """List AppAgentRoute rows for an agent via the agent-scoped endpoint."""
    r = client.get(
        f"{API}/agents/{agent_id}/app-mcp-routes/",
        headers=headers,
    )
    assert r.status_code == 200, f"List routes failed: {r.text}"
    return r.json()


def _list_user_routes(client: TestClient, headers: dict) -> dict:
    r = client.get(f"{API}/users/me/app-agent-routes/", headers=headers)
    assert r.status_code == 200, f"List user routes failed: {r.text}"
    return r.json()


def _update_agent_route(
    client: TestClient,
    headers: dict,
    agent_id: str,
    route_id: str,
    **fields,
) -> dict:
    r = client.put(
        f"{API}/agents/{agent_id}/app-mcp-routes/{route_id}",
        headers=headers,
        json=fields,
    )
    assert r.status_code == 200, f"Route update failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 1: Install with trigger prompt → auto-route created
# ---------------------------------------------------------------------------


def test_install_with_trigger_prompt_creates_auto_managed_route(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Installing a bundle whose revision has a non-empty router_trigger_prompt
    auto-creates an AppAgentRoute (is_auto_managed=True) and a self-assignment
    (is_enabled=True):

      1. Publisher sets trigger prompt, publishes bundle.
      2. Installer installs the bundle.
      3. AppAgentRoute exists with correct field values.
      4. installer's user-routes lists a self-assignment with is_enabled=True.
    """
    # ── Phase 1: Publisher sets trigger prompt and publishes ───────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Auto Route Bundle"
    )
    drain_tasks()
    trigger = "Handle calendar scheduling requests and event planning"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger,
    )
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    # ── Phase 2: Installer installs the bundle ─────────────────────────────
    _, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 3: AppAgentRoute exists with correct fields ──────────────────
    routes = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes) == 1, f"Expected 1 route, got {len(routes)}"
    route = routes[0]

    assert route["is_auto_managed"] is True, "Route must be auto-managed"
    assert route["session_mode"] == "conversation"
    assert route["channel_app_mcp"] is True
    assert route["is_active"] is True
    # name mirrors the agent name (the install's bundle display name)
    assert route["name"] == install["name"]
    # trigger_prompt must match the revision's snapshot
    assert route["trigger_prompt"] == trigger

    route_id = route["id"]

    # ── Phase 4: Self-assignment with is_enabled=True ──────────────────────
    user_routes = _list_user_routes(client, installer_headers)
    # The auto-managed route appears in personal_routes (it was created via
    # activate_for_myself=True, which makes it a self-assignment in the
    # assignments table — surfaced in shared_routes for admin-created routes
    # but for user-created-via-agent-endpoint it's in the agent route list).
    # Verify the assignment by inspecting the route's assignments list.
    assignments = route.get("assignments", [])
    assert len(assignments) == 1, f"Expected 1 assignment, got {assignments}"
    assert assignments[0]["is_enabled"] is True


# ---------------------------------------------------------------------------
# Scenario 2: Install with empty trigger prompt → no route, degraded status
# ---------------------------------------------------------------------------


def test_install_with_empty_trigger_prompt_skips_route_and_marks_degraded(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Installing a bundle with NO router_trigger_prompt skips route creation
    but the install itself succeeds; last_update_status reflects degraded.

      1. Publisher publishes without setting a trigger prompt.
      2. Installer installs — 200 OK.
      3. No AppAgentRoute row exists for the install.
      4. Install's last_update_status is 'degraded'.
    """
    # ── Phase 1: Publisher publishes without trigger prompt ────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="No Trigger Bundle"
    )
    drain_tasks()
    # Publish WITHOUT setting router_trigger_prompt
    _publish(client, superuser_token_headers, publisher_agent["id"])
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    # ── Phase 2: Install succeeds ──────────────────────────────────────────
    _, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 3: No route created ──────────────────────────────────────────
    routes = _list_agent_routes(client, installer_headers, install_id)
    assert routes == [], f"Expected no routes, got {routes}"

    # ── Phase 4: last_update_status is degraded ────────────────────────────
    fresh_install = client.get(
        f"{API}/agents/{install_id}", headers=installer_headers
    ).json()
    assert fresh_install["last_update_status"] == "degraded", (
        f"Expected 'degraded', got {fresh_install['last_update_status']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Route-creation failure does not abort install
# ---------------------------------------------------------------------------


def test_install_survives_route_creation_failure(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Even if the auto-route creation step raises, the install completes
    successfully (best-effort guard in _install_from_revision).

      1. Publisher publishes with trigger prompt.
      2. We patch _auto_create_app_mcp_route to raise RuntimeError.
      3. Install returns 200 and the agent row exists.
    """
    # ── Phase 1: Publish with trigger prompt ──────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Resilient Route Bundle"
    )
    drain_tasks()
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt="Handle document signing workflows",
    )
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    # ── Phase 2+3: Install with patched route creation raising ────────────
    _, installer_headers = _make_user_and_headers(client)

    with patch(
        "app.services.bundles.install_service.InstallService._auto_create_app_mcp_route",
        side_effect=RuntimeError("Simulated route creation failure"),
    ):
        r = client.post(
            f"{API}/catalog/{bundle_id}/install",
            headers=installer_headers,
            json={},
        )
    assert r.status_code == 200, f"Install must succeed despite route error: {r.text}"
    install = r.json()
    drain_tasks()
    assert install["id"] is not None
    # Verify the install row is accessible
    fresh = client.get(
        f"{API}/agents/{install['id']}", headers=installer_headers
    ).json()
    assert fresh["id"] == install["id"]


# ---------------------------------------------------------------------------
# Scenario 4: Apply-update propagates new trigger prompt to auto-managed route
# ---------------------------------------------------------------------------


def test_apply_update_refreshes_auto_managed_route_trigger_prompt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    After a publisher updates the trigger prompt and releases a new revision,
    the installer's apply-update refreshes AppAgentRoute.trigger_prompt.

      1. Publish v1 with trigger_prompt = "Schedule meetings".
      2. Installer installs → route trigger_prompt = "Schedule meetings".
      3. Publisher updates trigger_prompt to "Plan and schedule meetings" + republishes.
      4. Installer apply-update → route trigger_prompt updated.
    """
    # ── Phase 1: Publish v1 ────────────────────────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Updatable Route Bundle"
    )
    drain_tasks()
    trigger_v1 = "Schedule meetings and handle calendar requests"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger_v1,
        notes="v1",
    )
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    # ── Phase 2: Installer installs → route exists ─────────────────────────
    _, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    routes_v1 = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes_v1) == 1
    assert routes_v1[0]["trigger_prompt"] == trigger_v1
    assert routes_v1[0]["is_auto_managed"] is True
    route_id = routes_v1[0]["id"]

    # ── Phase 3: Publisher updates trigger_prompt + republishes ───────────
    trigger_v2 = "Plan and schedule meetings, manage calendar invites"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger_v2,
        notes="v2",
    )

    # Flag the install as pending update
    client.post(f"{API}/agents/{install_id}/check-updates", headers=installer_headers)

    # ── Phase 4: Apply-update → route trigger_prompt updated ──────────────
    r = client.post(f"{API}/agents/{install_id}/apply-update", headers=installer_headers)
    assert r.status_code == 200, r.text
    drain_tasks()

    routes_v2 = _list_agent_routes(client, installer_headers, install_id)
    matching = [rt for rt in routes_v2 if rt["id"] == route_id]
    assert len(matching) == 1, "Route must still exist after apply-update"
    assert matching[0]["trigger_prompt"] == trigger_v2, (
        f"Expected trigger updated to {trigger_v2!r}, got {matching[0]['trigger_prompt']!r}"
    )
    assert matching[0]["is_auto_managed"] is True, "Route must remain auto-managed"


# ---------------------------------------------------------------------------
# Scenario 5: Manual edit flips is_auto_managed=False → apply-update skips
# ---------------------------------------------------------------------------


def test_manual_edit_preserves_route_on_apply_update(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the installer edits the auto-managed route via PUT (is_auto_managed
    flips to False), apply-update leaves it untouched.

      1. Publish v1 with trigger prompt.
      2. Install → auto-managed route created.
      3. Installer edits the route trigger_prompt via PUT.
      4. Route is_auto_managed flips to False, trigger_prompt is now custom.
      5. Publisher republishes with new trigger_prompt.
      6. Installer apply-update → custom trigger_prompt preserved.
    """
    # ── Phase 1: Publish v1 ────────────────────────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Override Route Bundle"
    )
    drain_tasks()
    trigger_v1 = "Help with expense reports and reimbursements"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger_v1,
        notes="v1",
    )
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    # ── Phase 2: Install ───────────────────────────────────────────────────
    _, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    routes = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes) == 1
    assert routes[0]["is_auto_managed"] is True
    route_id = routes[0]["id"]

    # ── Phase 3: Installer manually edits the route ────────────────────────
    custom_trigger = "Manage my personal expense workflow — DO NOT OVERWRITE"
    updated = _update_agent_route(
        client, installer_headers, install_id, route_id,
        trigger_prompt=custom_trigger,
    )

    # ── Phase 4: is_auto_managed flips to False ────────────────────────────
    assert updated["is_auto_managed"] is False, (
        f"After user edit, is_auto_managed must be False, got {updated['is_auto_managed']!r}"
    )
    assert updated["trigger_prompt"] == custom_trigger

    # ── Phase 5: Publisher republishes with new trigger_prompt ─────────────
    trigger_v2 = "Process corporate expense submissions"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger_v2,
        notes="v2",
    )
    client.post(f"{API}/agents/{install_id}/check-updates", headers=installer_headers)

    # ── Phase 6: Apply-update → custom trigger_prompt preserved ───────────
    r = client.post(f"{API}/agents/{install_id}/apply-update", headers=installer_headers)
    assert r.status_code == 200, r.text
    drain_tasks()

    routes_after = _list_agent_routes(client, installer_headers, install_id)
    matching = [rt for rt in routes_after if rt["id"] == route_id]
    assert len(matching) == 1
    assert matching[0]["trigger_prompt"] == custom_trigger, (
        f"User-edited trigger must be preserved. Got {matching[0]['trigger_prompt']!r}"
    )
    assert matching[0]["is_auto_managed"] is False


# ---------------------------------------------------------------------------
# Scenario 6a: Legacy install (no route) — apply-update creates one
# ---------------------------------------------------------------------------


def test_apply_update_creates_route_for_legacy_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An install that existed before auto-routing (no route row) gets a route
    created by apply-update when the new revision has a trigger_prompt.

      1. Publish v1 WITHOUT trigger prompt → install, no route.
      2. Publisher adds trigger_prompt + republishes v2.
      3. Installer apply-update → route created.
    """
    # ── Phase 1: Publish v1 without trigger prompt → no route ─────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Legacy Install Bundle"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, publisher_agent["id"], notes="v1")
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    _, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    routes_v1 = _list_agent_routes(client, installer_headers, install_id)
    assert routes_v1 == [], "No route expected after install without trigger prompt"

    # ── Phase 2: Publisher adds trigger_prompt + republishes ──────────────
    trigger = "Automate project tracking and sprint planning"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger,
        notes="v2",
    )
    client.post(f"{API}/agents/{install_id}/check-updates", headers=installer_headers)

    # ── Phase 3: Apply-update → route created ─────────────────────────────
    r = client.post(f"{API}/agents/{install_id}/apply-update", headers=installer_headers)
    assert r.status_code == 200, r.text
    drain_tasks()

    routes_v2 = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes_v2) == 1, f"Expected 1 route after apply-update, got {routes_v2}"
    assert routes_v2[0]["is_auto_managed"] is True
    assert routes_v2[0]["trigger_prompt"] == trigger


# ---------------------------------------------------------------------------
# Scenario 6b: User-edited route exists → apply-update does NOT create a second
# ---------------------------------------------------------------------------


def test_apply_update_does_not_double_create_when_manual_route_exists(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the installer already has a user-edited (is_auto_managed=False) route
    on the agent, apply-update must NOT mint a second auto-managed route.

      1. Publish v1 with trigger prompt → install → route auto-created.
      2. Installer edits route → is_auto_managed flips to False.
      3. Publisher republishes (same or new trigger prompt).
      4. Apply-update → still only 1 route (the user-edited one).
    """
    # ── Phase 1: Publish v1 with trigger prompt → install ─────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="No Double Route Bundle"
    )
    drain_tasks()
    trigger_v1 = "Summarise customer support tickets"
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt=trigger_v1,
        notes="v1",
    )
    pub_fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]

    _, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    routes = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes) == 1
    route_id = routes[0]["id"]

    # ── Phase 2: Installer edits → is_auto_managed=False ──────────────────
    _update_agent_route(
        client, installer_headers, install_id, route_id,
        trigger_prompt="My customised support ticket triage prompt",
    )

    # ── Phase 3: Publisher republishes ────────────────────────────────────
    _publish(
        client, superuser_token_headers, publisher_agent["id"],
        router_trigger_prompt="Triage and summarise support tickets",
        notes="v2",
    )
    client.post(f"{API}/agents/{install_id}/check-updates", headers=installer_headers)

    # ── Phase 4: Apply-update → no second route created ───────────────────
    r = client.post(f"{API}/agents/{install_id}/apply-update", headers=installer_headers)
    assert r.status_code == 200, r.text
    drain_tasks()

    routes_after = _list_agent_routes(client, installer_headers, install_id)
    assert len(routes_after) == 1, (
        f"Expected exactly 1 route, got {len(routes_after)}. Routes: {routes_after}"
    )
    assert routes_after[0]["id"] == route_id, "The original (user-edited) route must remain"
    assert routes_after[0]["is_auto_managed"] is False
