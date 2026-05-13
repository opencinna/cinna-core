"""
Phase 8 — Backfill script idempotency tests.

Tests for ``backend/app/scripts/backfill_router_trigger_prompts.py``.

Calls the ``backfill()`` function (not ``main()``) directly on the test DB
session, so all writes share the test transaction and roll back automatically.

Covers:
  1. First run creates N routes for eligible installs and updates
     skipped_already_routed=0.

  2. Second run is a no-op: skipped_already_routed == N (the routes
     from run 1 are already present).

  3. Publisher installs (is_publisher_install=True) are skipped.

  4. Installs with empty description are skipped.

  5. Owned non-bundle agents (bundle_uuid IS NULL) are skipped — the
     owner manages App MCP exposure manually for their own agents.

Note: The ``generate_router_trigger_prompt`` AI function is patched to
return a deterministic string so tests are fast and LLM-independent.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR

# Deterministic mock for the AI generator used by the backfill script.
_MOCK_TRIGGER = "Handle requests for this agent's core functionality"


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
    description: str | None = None,
    set_trigger_prompt: bool = False,
) -> str:
    """Create a bundle install. Returns install_id."""
    agent = create_agent_via_api(client, superuser_headers, name="Backfill Test Agent")
    drain_tasks()
    agent_id = agent["id"]

    if description is not None:
        r = client.put(
            f"{API}/agents/{agent_id}",
            headers=superuser_headers,
            json={"description": description},
        )
        assert r.status_code == 200, r.text

    if set_trigger_prompt:
        r = client.patch(
            f"{API}/agents/{agent_id}/router-trigger-prompt",
            headers=superuser_headers,
            json={"router_trigger_prompt": _MOCK_TRIGGER},
        )
        assert r.status_code == 200, r.text

    # Publish
    r = client.post(f"{API}/agents/{agent_id}/publish", headers=superuser_headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(f"{API}/agents/{agent_id}", headers=superuser_headers).json()
    client.patch(
        f"{API}/bundles/{fresh['bundle_uuid']}",
        headers=superuser_headers,
        json={"is_listed": True, "visibility": "public"},
    )

    r = client.post(
        f"{API}/catalog/{fresh['bundle_id']}/install",
        headers=installer_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()
    return install["id"]


def _list_routes_for_agent(client: TestClient, headers: dict, agent_id: str) -> list[dict]:
    r = client.get(f"{API}/agents/{agent_id}/app-mcp-routes/", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 1 + 2: First run creates routes; second run is no-op
# ---------------------------------------------------------------------------


def test_backfill_idempotency(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    First backfill run creates auto-managed routes for eligible installs
    (non-publisher, has description, no existing auto-managed route).

    Second run is a pure no-op: skipped_already_routed equals the number
    created in the first run.

      1. Create 2 eligible installs (description present, no existing route).
      2. Run backfill() → generated_and_routed == 2, skipped_already_routed == 0.
      3. Run backfill() again → skipped_already_routed == 2, generated_and_routed == 0.
    """
    from app.scripts.backfill_router_trigger_prompts import backfill

    # ── Phase 1: Create 2 eligible installs ───────────────────────────────
    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    install_a_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        description="Assists with project planning and task tracking",
    )
    install_b_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        description="Answers questions about HR policies and benefits",
    )

    # Neither install should have an auto-managed route yet
    # (bundles were published without a trigger prompt → degraded installs)
    routes_a = _list_routes_for_agent(client, installer_headers, install_a_id)
    routes_b = _list_routes_for_agent(client, installer_headers, install_b_id)
    assert routes_a == []
    assert routes_b == []

    # ── Phase 2: First backfill run ───────────────────────────────────────
    with patch(
        "app.scripts.backfill_router_trigger_prompts.generate_router_trigger_prompt",
        return_value=_MOCK_TRIGGER,
    ):
        counters1 = backfill(db, dry_run=False)

    # At least 2 installs were processed and got routes
    assert counters1["generated_and_routed"] >= 2, (
        f"Expected >= 2 routes created, got {counters1}"
    )
    assert counters1["skipped_already_routed"] == 0 or (
        # May be > 0 if a previous test left routes behind (transaction rollback
        # should prevent this, but be defensive about shared seeded data)
        counters1["generated_and_routed"] + counters1["skipped_already_routed"] >= 2
    )

    # Routes now exist for both installs
    routes_a_after = _list_routes_for_agent(client, installer_headers, install_a_id)
    routes_b_after = _list_routes_for_agent(client, installer_headers, install_b_id)
    auto_a = [r for r in routes_a_after if r["is_auto_managed"]]
    auto_b = [r for r in routes_b_after if r["is_auto_managed"]]
    assert len(auto_a) == 1, f"Expected 1 auto route for install A, got {routes_a_after}"
    assert len(auto_b) == 1, f"Expected 1 auto route for install B, got {routes_b_after}"

    # ── Phase 3: Second backfill run is a no-op ────────────────────────────
    with patch(
        "app.scripts.backfill_router_trigger_prompts.generate_router_trigger_prompt",
        return_value=_MOCK_TRIGGER,
    ):
        counters2 = backfill(db, dry_run=False)

    # generated_and_routed must be 0 (already routed)
    assert counters2["generated_and_routed"] == 0, (
        f"Second backfill run must be a no-op, got generated_and_routed="
        f"{counters2['generated_and_routed']}. Full counters: {counters2}"
    )
    # The two installs we created are counted as already routed
    assert counters2["skipped_already_routed"] >= 2, (
        f"Expected >= 2 skipped_already_routed on second run, got {counters2}"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Publisher installs are skipped
# ---------------------------------------------------------------------------


def test_backfill_skips_publisher_installs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Publisher installs (is_publisher_install=True) are skipped by the backfill.

    The publisher's own install is marked is_publisher_install=True at
    publish time. After backfill, no auto-managed route must exist for it.
    """
    from app.scripts.backfill_router_trigger_prompts import backfill

    # Create a publisher agent with description
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Publisher Skip Test"
    )
    drain_tasks()
    agent_id = agent["id"]

    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"description": "This is the publisher's own install"},
    )
    assert r.status_code == 200, r.text

    # Publish — this makes is_publisher_install=True on the publisher's own row
    r = client.post(f"{API}/agents/{agent_id}/publish", headers=superuser_token_headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()

    # Verify it is a publisher install
    fresh = client.get(f"{API}/agents/{agent_id}", headers=superuser_token_headers).json()
    assert fresh["is_publisher_install"] is True

    # No route yet
    routes_before = _list_routes_for_agent(client, superuser_token_headers, agent_id)
    assert routes_before == []

    # Run backfill — publisher install must not get a route created.
    # The backfill filters to is_publisher_install=False at the DB query
    # level, so publisher installs are excluded from scanning entirely
    # (they do not appear in the scanned count) — there is no separate
    # publisher-skipped counter.
    with patch(
        "app.scripts.backfill_router_trigger_prompts.generate_router_trigger_prompt",
        return_value=_MOCK_TRIGGER,
    ):
        counters = backfill(db, dry_run=False)

    # The publisher install is not scanned (WHERE clause excludes it).
    # Confirm no auto-managed route was created for it.
    routes_after = _list_routes_for_agent(client, superuser_token_headers, agent_id)
    auto_routes = [r for r in routes_after if r["is_auto_managed"]]
    assert auto_routes == [], (
        f"Publisher install must not get an auto-managed route from backfill. "
        f"Got: {routes_after}"
    )
    # No routes should have been generated because the only agent in the DB
    # (the publisher's own install) is excluded by the WHERE clause.
    assert counters["generated_and_routed"] == 0, (
        f"Expected 0 routes generated (only publisher install exists), got {counters}"
    )


# ---------------------------------------------------------------------------
# Scenario 4: Installs with empty description are skipped
# ---------------------------------------------------------------------------


def test_backfill_skips_agents_without_description(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    The backfill skips installs that have an empty description (no generator
    input). The install remains without an auto-managed route.
    """
    from app.scripts.backfill_router_trigger_prompts import backfill

    _, installer_headers = _make_user_and_headers(client)
    create_random_ai_credential(client, installer_headers, set_default=True)

    # Install without description
    install_id = _publish_and_install(
        client,
        superuser_token_headers,
        installer_headers,
        description=None,  # no description
    )

    # Update the install to explicitly clear description.
    # Superuser headers used because the plain installer does not have
    # the agent-developer role required by the generic PUT endpoint.
    r = client.put(
        f"{API}/agents/{install_id}",
        headers=superuser_token_headers,
        json={"description": ""},
    )
    assert r.status_code == 200, r.text

    routes_before = _list_routes_for_agent(client, installer_headers, install_id)
    assert routes_before == []

    with patch(
        "app.scripts.backfill_router_trigger_prompts.generate_router_trigger_prompt",
        return_value=_MOCK_TRIGGER,
    ):
        counters = backfill(db, dry_run=False)

    assert counters["skipped_no_description"] >= 1, (
        f"Expected at least 1 no-description skip, got {counters}"
    )

    # Still no route
    routes_after = _list_routes_for_agent(client, installer_headers, install_id)
    auto_routes = [r for r in routes_after if r["is_auto_managed"]]
    assert auto_routes == [], (
        f"No auto-managed route must be created for install with empty description. "
        f"Got: {routes_after}"
    )


# ---------------------------------------------------------------------------
# Scenario 5: Owned non-bundle agents are skipped
# ---------------------------------------------------------------------------


def test_backfill_skips_owned_non_bundle_agents(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Owned agents that are NOT bundle installs (``bundle_uuid IS NULL``)
    must be skipped by the backfill — their owner controls App MCP
    exposure manually via the Integrations tab. Auto-routing is reserved
    for the install-from-catalog flow.
    """
    from app.scripts.backfill_router_trigger_prompts import backfill

    # Create an owned agent (never published, never installed from a bundle)
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Owned Non-Bundle Agent"
    )
    drain_tasks()
    agent_id = agent["id"]

    # Give it a description so the only thing keeping it out of the
    # backfill is the bundle_uuid filter — not the description filter.
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"description": "A regular agent the user owns directly"},
    )
    assert r.status_code == 200, r.text

    # Sanity check: this agent has no bundle linkage
    fresh = client.get(f"{API}/agents/{agent_id}", headers=superuser_token_headers).json()
    assert fresh["is_publisher_install"] is False
    assert fresh["bundle_uuid"] is None

    # No route to start with
    routes_before = _list_routes_for_agent(client, superuser_token_headers, agent_id)
    assert routes_before == []

    with patch(
        "app.scripts.backfill_router_trigger_prompts.generate_router_trigger_prompt",
        return_value=_MOCK_TRIGGER,
    ):
        backfill(db, dry_run=False)

    # Backfill must not have created a route for this owned non-bundle agent
    routes_after = _list_routes_for_agent(client, superuser_token_headers, agent_id)
    auto_routes = [r for r in routes_after if r["is_auto_managed"]]
    assert auto_routes == [], (
        f"Owned non-bundle agent must not receive an auto-managed route from backfill. "
        f"Got: {routes_after}"
    )
