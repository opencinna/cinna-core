"""
Integration tests for the external agents catalog (GET /external/agents).

Scenarios covered:
  1. Unauthenticated request is rejected (401)
  2. Empty result when user has no agents or identity contacts
  3. Personal active agent appears in results with correct fields
  4. Inactive personal agent is filtered out
  5. Identity contact appears when is_enabled=True
  6. Identity contact is absent when is_enabled=False (default)
  7. Identity contact example prompts are prefixed with owner name
  8. Both sections coexist in a single response
  9. agent_card_url patterns are correct for each target type
  10. protocol_versions is ["1.0", "0.3.0"] for every target
  11. workspace_id filter limits personal agents to the given workspace

The third source this catalog used to build from — the "MCP Shared Agent"
section, backed by ``AppAgentRoute`` / ``AppAgentRouteAssignment`` and
surfaced as ``target_type == "app_mcp_route"`` — was deleted in phase 5 of
``docs/plans/channels_identity_unification/``. Personal agents and identity
contacts are the two sources that remain; see
``app/services/external/external_agent_catalog_service.py``.
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
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


def _promote_to_developer(
    client: TestClient,
    superuser_headers: dict,
    user_id: str,
) -> None:
    """Promote a user to the ``agent-developer`` role.

    Agent creation (``POST /agents/``) is gated on the developer role since
    the Phase-3 RBAC rollout. Tests that create agents as a freshly-signed-up
    user must promote that user first.
    """
    r = client.patch(
        f"{settings.API_V1_STR}/users/{user_id}/role",
        headers=superuser_headers,
        json={"role": "agent-developer"},
    )
    assert r.status_code == 200, f"Failed to promote user to agent-developer: {r.text}"


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
# Scenario 5 & 6: Identity Contacts
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

    # Owner needs developer role + AI credential to create an agent
    _promote_to_developer(client, superuser_token_headers, owner_id)
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
    _promote_to_developer(client, superuser_token_headers, owner["id"])
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
    superuser_token_headers: dict,
) -> None:
    """Identity contact example prompts are prefixed with 'ask {owner_name} to'."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner, owner_headers = create_random_user_with_headers(client)
    owner_id = owner["id"]
    # full_name may be None for signup-created users; service coerces to ""
    owner_name = owner["full_name"] or ""

    _promote_to_developer(client, superuser_token_headers, owner_id)
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
# Scenario 8: Both sections coexist
# ---------------------------------------------------------------------------


def test_both_sections_coexist(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Personal agents and identity contacts can both appear together.

    Used to be three sections (personal agents, MCP shared routes, identity
    contacts) — the middle one was deleted with the AppAgentRoute family in
    phase 5 of docs/plans/channels_identity_unification/.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    # 1. Caller owns a personal agent (needs developer role + AI credential)
    _promote_to_developer(client, superuser_token_headers, caller_id)
    _ensure_user_can_create_agents(client, caller_headers)
    personal_agent = create_agent_via_api(
        client, caller_headers, name="Caller Personal Agent"
    )
    personal_agent_id = personal_agent["id"]

    # 2. A second user owns an agent and exposes it as an identity contact for the caller
    identity_owner, identity_owner_headers = create_random_user_with_headers(client)
    _promote_to_developer(client, superuser_token_headers, identity_owner["id"])
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
    identity_target_ids = [t["target_id"] for t in _targets_by_type(targets, "identity")]

    assert personal_agent_id in agent_target_ids, "Personal agent must appear"
    assert identity_owner["id"] in identity_target_ids, "Identity contact must appear"


# ---------------------------------------------------------------------------
# Scenario: cinna.mcp descriptor mirrored in the discovery payload
# ---------------------------------------------------------------------------


def test_discovery_mcp_descriptor_for_personal_agent(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    A personal agent target carries a populated `mcp` descriptor:
      1. version == 1, tool_name derived from name
      2. input_schema omits context_id (desktop is stateful)
      3. capabilities.files is True, resources is False
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Discovery MCP Agent"
    )
    agent_id = agent["id"]

    targets = _list_external_agents(client, superuser_token_headers)
    target = next(
        t for t in _targets_by_type(targets, "agent") if t["target_id"] == agent_id
    )

    mcp = target["mcp"]
    assert mcp is not None, "Personal agent target must carry an mcp descriptor"
    assert mcp["version"] == 1
    assert mcp["tool_name"] == "discovery_mcp_agent"
    assert mcp["display_name"] == "Discovery MCP Agent"
    assert "context_id" not in mcp["input_schema"]["properties"]
    assert mcp["capabilities"]["files"] is True
    assert mcp["capabilities"]["resources"] is False


def test_discovery_mcp_slugs_are_unique_across_response(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Two agents with the same name get distinct, deterministic tool_name slugs:
      1. Create two agents with identical names
      2. Both carry an mcp descriptor
      3. Their tool_name slugs differ (deconflicted by agent id)
      4. Slugs are unique across all targets in the response
    """
    a1 = create_agent_via_api(client, superuser_token_headers, name="Twin Agent")
    a2 = create_agent_via_api(client, superuser_token_headers, name="Twin Agent")

    targets = _list_external_agents(client, superuser_token_headers)
    agent_targets = _targets_by_type(targets, "agent")

    t1 = next(t for t in agent_targets if t["target_id"] == a1["id"])
    t2 = next(t for t in agent_targets if t["target_id"] == a2["id"])

    slug1 = t1["mcp"]["tool_name"]
    slug2 = t2["mcp"]["tool_name"]
    assert slug1 != slug2, (
        f"Same-name agents must get distinct slugs, both were {slug1!r}"
    )
    # Both should be a deconflicted variant of the shared base slug.
    assert slug1.startswith("twin_agent")
    assert slug2.startswith("twin_agent")

    # Determinism: a second fetch yields identical slugs.
    targets_again = _list_external_agents(client, superuser_token_headers)
    again = {t["target_id"]: t["mcp"]["tool_name"] for t in targets_again if t["mcp"]}
    assert again[a1["id"]] == slug1
    assert again[a2["id"]] == slug2

    # All descriptor slugs across the whole response are unique.
    all_slugs = [t["mcp"]["tool_name"] for t in targets if t.get("mcp")]
    assert len(all_slugs) == len(set(all_slugs)), (
        f"Tool slugs must be unique across the response, got {all_slugs}"
    )


def test_discovery_mcp_absent_for_identity_contact(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Identity contacts are person-level and carry no single-tool mcp descriptor."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner, owner_headers = create_random_user_with_headers(client)
    owner_id = owner["id"]
    _promote_to_developer(client, superuser_token_headers, owner_id)
    _ensure_user_can_create_agents(client, owner_headers)
    owner_agent = create_agent_via_api(
        client, owner_headers, name="Identity MCP Agent"
    )
    create_identity_binding(
        client,
        owner_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Do identity things",
        assigned_user_ids=[caller_id],
    )
    toggle_identity_contact(client, caller_headers, owner_id=owner_id, is_enabled=True)

    targets = _list_external_agents(client, caller_headers)
    identity_target = next(
        t for t in _targets_by_type(targets, "identity") if t["target_id"] == owner_id
    )
    assert identity_target["mcp"] is None, (
        "Identity contacts expose agents via card skills, not a single mcp tool"
    )


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
