"""
Integration tests for the external agents catalog (GET /external/agents).

Scenarios covered:
  1. Unauthenticated request is rejected (401)
  2. Empty result when user has no agents, routes, or identity contacts
  3. Personal active agent appears in results with correct fields
  4. Inactive personal agent is filtered out
  5. MCP Shared Agent appears when assignment is enabled by the caller
  6. MCP Shared Agent is absent when assignment is disabled
  7. Identity contact appears when is_enabled=True
  8. Identity contact is absent when is_enabled=False (default)
  9. Identity contact example prompts are prefixed with owner name
  10. All three sections coexist in a single response
  11. agent_card_url patterns are correct for each target type
  12. protocol_versions is ["1.0", "0.3.0"] for every target
  13. workspace_id filter limits personal agents to the given workspace
  14. workspace_id filter does not affect shared MCP route entries
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_admin_route, toggle_admin_assignment
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import (
    create_identity_binding,
    toggle_identity_contact,
)
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_lower_string

_EXT_BASE = f"{settings.API_V1_STR}/external"
_WORKSPACES_BASE = f"{settings.API_V1_STR}/user-workspaces"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_external_agents(
    client: TestClient,
    headers: dict,
    workspace_id: str | None = None,
) -> list[dict]:
    """Call GET /external/agents and return the targets list."""
    params = {}
    if workspace_id is not None:
        params["workspace_id"] = workspace_id
    r = client.get(f"{_EXT_BASE}/agents", headers=headers, params=params)
    assert r.status_code == 200, f"list_external_agents failed: {r.text}"
    data = r.json()
    assert "targets" in data
    return data["targets"]


def _targets_by_type(targets: list[dict], target_type: str) -> list[dict]:
    return [t for t in targets if t["target_type"] == target_type]


def _ensure_user_can_create_agents(client: TestClient, headers: dict) -> None:
    """Create a default AI credential for a user so they can create agents.

    Agent creation validates that the creator has a credential for the default
    SDK (claude-code/anthropic). This helper creates a dummy anthropic credential
    and sets it as the default so `create_agent_via_api` succeeds for non-superusers.
    """
    create_random_ai_credential(
        client,
        headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-key",
        name="test-agent-cred",
        set_default=True,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Unauthenticated
# ---------------------------------------------------------------------------


def test_list_external_agents_unauthenticated(client: TestClient) -> None:
    """GET /external/agents without a token must return 401."""
    r = client.get(f"{_EXT_BASE}/agents")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 2: Empty result
# ---------------------------------------------------------------------------


def test_list_external_agents_empty(client: TestClient) -> None:
    """A fresh user with no agents, routes, or contacts gets an empty list."""
    _, headers = create_random_user_with_headers(client)
    targets = _list_external_agents(client, headers)
    assert targets == []


# ---------------------------------------------------------------------------
# Scenario 3 & 4: Personal agents
# ---------------------------------------------------------------------------


def test_personal_agent_appears(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """An active personal agent owned by the user appears in the results."""
    agent = create_agent_via_api(client, superuser_token_headers, name="My Visible Agent")
    agent_id = agent["id"]

    targets = _list_external_agents(client, superuser_token_headers)
    agent_targets = _targets_by_type(targets, "agent")
    ids = [t["target_id"] for t in agent_targets]
    assert agent_id in ids, f"Expected agent {agent_id} in targets, got {ids}"

    # Verify required fields on this target
    target = next(t for t in agent_targets if t["target_id"] == agent_id)
    assert target["name"] == "My Visible Agent"
    assert target["target_type"] == "agent"
    assert "agent_card_url" in target
    assert f"/api/v1/external/a2a/agent/{agent_id}/" in target["agent_card_url"]
    assert target["protocol_versions"] == ["1.0", "0.3.0"]


def test_inactive_personal_agent_filtered(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """An agent deactivated via PUT should not appear in the external list."""
    agent = create_agent_via_api(client, superuser_token_headers, name="Deactivated Agent")
    agent_id = agent["id"]

    # Deactivate the agent via PUT (the agent update endpoint)
    update_agent(client, superuser_token_headers, agent_id, is_active=False)

    targets = _list_external_agents(client, superuser_token_headers)
    agent_ids = [t["target_id"] for t in _targets_by_type(targets, "agent")]
    assert agent_id not in agent_ids, "Inactive agent must not appear in external list"


# ---------------------------------------------------------------------------
# Scenario 5 & 6: MCP Shared Agents
# ---------------------------------------------------------------------------


def test_shared_route_appears_when_assignment_enabled(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A route shared with a user appears when the user enables their assignment."""
    # Owner creates agent and route, assigns caller
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_agent = create_agent_via_api(
        client, superuser_token_headers, name="Shared Agent Owner"
    )
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Handle reports",
        assigned_user_ids=[caller_id],
        auto_enable_for_users=False,
    )
    route_id = route["id"]

    # Assignment is disabled by default — should not appear yet
    targets_before = _list_external_agents(client, caller_headers)
    route_ids_before = [t["target_id"] for t in _targets_by_type(targets_before, "app_mcp_route")]
    assert route_id not in route_ids_before, "Disabled route must not appear"

    # Caller enables the assignment
    assignment_id = next(
        a["id"] for a in route["assignments"] if a["user_id"] == caller_id
    )
    toggle_admin_assignment(client, caller_headers, assignment_id, is_enabled=True)

    # Now the route should appear
    targets_after = _list_external_agents(client, caller_headers)
    route_targets = _targets_by_type(targets_after, "app_mcp_route")
    route_ids_after = [t["target_id"] for t in route_targets]
    assert route_id in route_ids_after, f"Enabled route must appear. Got: {route_ids_after}"

    # Verify required fields
    target = next(t for t in route_targets if t["target_id"] == route_id)
    assert target["target_type"] == "app_mcp_route"
    assert "agent_card_url" in target
    assert f"/api/v1/external/a2a/route/{route_id}/" in target["agent_card_url"]
    assert target["protocol_versions"] == ["1.0", "0.3.0"]
    assert target["description"] == "Handle reports"  # trigger_prompt used as description


def test_shared_route_absent_when_assignment_disabled(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A route shared with a user does not appear when the assignment is disabled."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_agent = create_agent_via_api(
        client, superuser_token_headers, name="Shared Agent Disabled"
    )
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Handle disabled route",
        assigned_user_ids=[caller_id],
        auto_enable_for_users=False,  # assignment is_enabled=False by default
    )
    route_id = route["id"]

    targets = _list_external_agents(client, caller_headers)
    route_ids = [t["target_id"] for t in _targets_by_type(targets, "app_mcp_route")]
    assert route_id not in route_ids, "Disabled assignment must not surface the route"


# ---------------------------------------------------------------------------
# Scenario 7 & 8: Identity Contacts
# ---------------------------------------------------------------------------


def test_identity_contact_appears_when_enabled(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """An identity contact appears in results when the caller enables it."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner, owner_headers = create_random_user_with_headers(client)
    owner_id = owner["id"]

    # Owner needs an AI credential to create an agent
    _ensure_user_can_create_agents(client, owner_headers)

    # Owner creates an agent and binding, assigns caller
    owner_agent = create_agent_via_api(
        client, owner_headers, name="Identity Owner Agent"
    )
    create_identity_binding(
        client,
        owner_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Handle identity requests",
        assigned_user_ids=[caller_id],
    )

    # Identity contact is disabled by default — should not appear
    targets_before = _list_external_agents(client, caller_headers)
    identity_ids_before = [t["target_id"] for t in _targets_by_type(targets_before, "identity")]
    assert owner_id not in identity_ids_before, "Disabled identity contact must not appear"

    # Caller enables the identity contact
    toggle_identity_contact(client, caller_headers, owner_id=owner_id, is_enabled=True)

    # Now it should appear
    targets_after = _list_external_agents(client, caller_headers)
    identity_targets = _targets_by_type(targets_after, "identity")
    identity_ids_after = [t["target_id"] for t in identity_targets]
    assert owner_id in identity_ids_after, f"Enabled contact must appear. Got: {identity_ids_after}"

    # Verify required fields
    target = next(t for t in identity_targets if t["target_id"] == owner_id)
    assert target["target_type"] == "identity"
    assert "agent_card_url" in target
    assert f"/api/v1/external/a2a/identity/{owner_id}/" in target["agent_card_url"]
    assert target["protocol_versions"] == ["1.0", "0.3.0"]
    # full_name may be None for signup-created users; the service coerces to ""
    assert target["name"] == (owner["full_name"] or "")
    assert target["description"] == owner["email"]


def test_identity_contact_absent_when_disabled(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """An identity contact does not appear when is_enabled=False (the default)."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner, owner_headers = create_random_user_with_headers(client)
    _ensure_user_can_create_agents(client, owner_headers)

    owner_agent = create_agent_via_api(
        client, owner_headers, name="Identity Not Enabled Agent"
    )
    binding = create_identity_binding(
        client,
        owner_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Do stuff",
        assigned_user_ids=[caller_id],
    )

    # Do NOT toggle; assignment defaults to is_enabled=False
    targets = _list_external_agents(client, caller_headers)
    identity_ids = [t["target_id"] for t in _targets_by_type(targets, "identity")]
    assert owner["id"] not in identity_ids, "Disabled identity contact must not appear"


# ---------------------------------------------------------------------------
# Scenario 9: Identity contact example prompts are owner-prefixed
# ---------------------------------------------------------------------------


def test_identity_contact_example_prompts_are_prefixed(
    client: TestClient,
) -> None:
    """Identity contact example prompts are prefixed with 'ask {owner_name} to'."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner, owner_headers = create_random_user_with_headers(client)
    owner_id = owner["id"]
    # full_name may be None for signup-created users; service coerces to ""
    owner_name = owner["full_name"] or ""

    _ensure_user_can_create_agents(client, owner_headers)
    owner_agent = create_agent_via_api(
        client, owner_headers, name="Identity Prompts Agent"
    )
    create_identity_binding(
        client,
        owner_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Do analysis tasks",
        assigned_user_ids=[caller_id],
        prompt_examples="generate report\nanalyze data",
    )

    toggle_identity_contact(client, caller_headers, owner_id=owner_id, is_enabled=True)

    targets = _list_external_agents(client, caller_headers)
    identity_targets = _targets_by_type(targets, "identity")
    target = next((t for t in identity_targets if t["target_id"] == owner_id), None)
    assert target is not None, "Identity contact must appear after enabling"

    example_prompts = target["example_prompts"]
    assert len(example_prompts) > 0, "Must have at least one prompt example"
    expected_prefix = f"ask {owner_name} to ".lower()
    for prompt in example_prompts:
        assert prompt.lower().startswith(expected_prefix), (
            f"Prompt '{prompt}' should start with '{expected_prefix}'"
        )


# ---------------------------------------------------------------------------
# Scenario 10: All three sections coexist
# ---------------------------------------------------------------------------


def test_all_three_sections_coexist(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Personal agents, shared routes, and identity contacts can all appear together."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    # 1. Caller owns a personal agent (needs AI credential)
    _ensure_user_can_create_agents(client, caller_headers)
    personal_agent = create_agent_via_api(
        client, caller_headers, name="Caller Personal Agent"
    )
    personal_agent_id = personal_agent["id"]

    # 2. Superuser creates a route and assigns the caller
    route_agent = create_agent_via_api(
        client, superuser_token_headers, name="Route Owner Agent"
    )
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=route_agent["id"],
        trigger_prompt="Handle routed stuff",
        assigned_user_ids=[caller_id],
        auto_enable_for_users=True,  # superuser auto-enables
    )
    route_id = route["id"]

    # 3. A third user owns an agent and exposes it as an identity contact for the caller
    identity_owner, identity_owner_headers = create_random_user_with_headers(client)
    _ensure_user_can_create_agents(client, identity_owner_headers)
    identity_agent = create_agent_via_api(
        client, identity_owner_headers, name="Identity Source Agent"
    )
    create_identity_binding(
        client,
        identity_owner_headers,
        agent_id=identity_agent["id"],
        trigger_prompt="Handle identity stuff",
        assigned_user_ids=[caller_id],
    )
    toggle_identity_contact(
        client, caller_headers, owner_id=identity_owner["id"], is_enabled=True
    )

    targets = _list_external_agents(client, caller_headers)

    agent_target_ids = [t["target_id"] for t in _targets_by_type(targets, "agent")]
    route_target_ids = [t["target_id"] for t in _targets_by_type(targets, "app_mcp_route")]
    identity_target_ids = [t["target_id"] for t in _targets_by_type(targets, "identity")]

    assert personal_agent_id in agent_target_ids, "Personal agent must appear"
    assert route_id in route_target_ids, "Shared route must appear"
    assert identity_owner["id"] in identity_target_ids, "Identity contact must appear"


# ---------------------------------------------------------------------------
# Helpers for workspace filter tests
# ---------------------------------------------------------------------------


def _create_workspace(client: TestClient, headers: dict, name: str) -> dict:
    """Create a user workspace and return the response JSON."""
    r = client.post(
        f"{_WORKSPACES_BASE}/",
        json={"name": name},
        headers=headers,
    )
    assert r.status_code == 200, f"workspace creation failed: {r.text}"
    return r.json()


def _create_agent_in_workspace(
    client: TestClient,
    headers: dict,
    workspace_id: str,
    name: str | None = None,
) -> dict:
    """Create an agent assigned to a workspace via direct API call."""
    data = {
        "name": name or f"ws-agent-{random_lower_string()[:8]}",
        "user_workspace_id": workspace_id,
    }
    r = client.post(
        f"{settings.API_V1_STR}/agents/",
        headers=headers,
        json=data,
    )
    assert r.status_code == 200, f"agent creation failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 13: workspace_id filter limits personal agents
# ---------------------------------------------------------------------------


def test_workspace_id_filter_limits_personal_agents(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Workspace filter scenario:
      1. Create a workspace.
      2. Create two agents: one in the workspace, one without.
      3. GET /agents without filter → both personal agents present.
      4. GET /agents?workspace_id=<ws_id> → only the workspace agent present.
    """
    # ── Phase 1: Create workspace ────────────────────────────────────────
    ws = _create_workspace(client, superuser_token_headers, "test-workspace-agents-a")
    workspace_id = ws["id"]

    # ── Phase 2: Create two agents (drain tasks after each so env stub is ready) ─
    agent_ws = _create_agent_in_workspace(
        client, superuser_token_headers, workspace_id, name="ws-agent-filter"
    )
    drain_tasks()
    agent_no_ws = create_agent_via_api(
        client, superuser_token_headers, name="no-ws-agent-filter"
    )
    drain_tasks()

    # ── Phase 3: Without filter → both present ────────────────────────────
    all_targets = _list_external_agents(client, superuser_token_headers)
    all_agent_ids = [t["target_id"] for t in all_targets if t["target_type"] == "agent"]
    assert agent_ws["id"] in all_agent_ids, "workspace agent missing without filter"
    assert agent_no_ws["id"] in all_agent_ids, "no-workspace agent missing without filter"

    # ── Phase 4: With workspace_id filter → only workspace agent ─────────
    filtered_targets = _list_external_agents(
        client, superuser_token_headers, workspace_id=workspace_id
    )
    filtered_agent_ids = [
        t["target_id"] for t in filtered_targets if t["target_type"] == "agent"
    ]
    assert agent_ws["id"] in filtered_agent_ids, "workspace agent missing with filter"
    assert agent_no_ws["id"] not in filtered_agent_ids, (
        "no-workspace agent should be excluded by workspace_id filter"
    )


# ---------------------------------------------------------------------------
# Scenario 14: workspace_id filter does not affect shared MCP routes
# ---------------------------------------------------------------------------


def test_workspace_id_filter_does_not_affect_shared_routes(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    workspace_id filter applies only to personal agents — MCP shared agent
    entries are always returned regardless of the filter.
    """
    # ── Phase 1: Create workspace ────────────────────────────────────────
    ws = _create_workspace(client, superuser_token_headers, "test-workspace-agents-b")
    workspace_id = ws["id"]

    # ── Phase 2: Create a caller user ────────────────────────────────────
    _, caller_hdrs = create_random_user_with_headers(client)
    caller_r = client.get(f"{settings.API_V1_STR}/users/me", headers=caller_hdrs)
    caller_id = caller_r.json()["id"]

    # ── Phase 3: Create an agent + route accessible to the caller ─────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Handle shared things",
        assigned_user_ids=[caller_id],
        auto_enable_for_users=True,
    )
    route_id = route["id"]

    # ── Phase 4: Apply workspace filter for the caller ─────────────────
    # The caller has no personal agents in the workspace, but the shared
    # route must still appear.
    filtered_targets = _list_external_agents(
        client, caller_hdrs, workspace_id=workspace_id
    )
    shared_ids = [
        t["target_id"] for t in filtered_targets if t["target_type"] == "app_mcp_route"
    ]
    assert route_id in shared_ids, (
        "MCP shared route should appear even when workspace_id filter is set"
    )
