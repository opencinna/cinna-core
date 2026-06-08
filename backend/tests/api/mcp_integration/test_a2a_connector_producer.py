"""
Agent-to-Agent MCP Connector — Producer side tests.

Tests the ``is_agent_to_agent`` flag on mcp_connector and the producer-side
discoverability surface (``GET /mcp-providers/discoverable-agents``):

  - Creating a connector with ``is_agent_to_agent=True`` (developer-gated via
    the superuser; a regular user needs agent-developer role).
  - The flag is stored and reflected in GET / list / update.
  - ``GET /mcp-providers/discoverable-agents`` returns connectors where the
    calling user is owner or in ``allowed_user_ids``.
  - Hidden from non-ACL users.
  - Excludes the consumer's own agent when ``consumer_agent_id`` is provided.
  - Inactive (``is_active=False``) connectors are excluded from discovery.
  - Non-agent2agent connectors are excluded from discovery.
  - Unauthenticated discovery request is rejected (401).
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import (
    create_mcp_connector,
    list_mcp_connectors,
    get_mcp_connector,
    update_mcp_connector,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer

_MCP_PROVIDERS_BASE = f"{settings.API_V1_STR}/mcp-providers"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "Producer Agent",
) -> dict:
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    return get_agent(client, token_headers, agent["id"])


def _create_a2a_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    name: str = "A2A Connector",
    allowed_user_ids: list | None = None,
    allow_token_access: bool = True,
    mode: str = "conversation",
) -> dict:
    """Create an agent2agent connector."""
    body: dict = {
        "name": name,
        "mode": mode,
        "is_agent_to_agent": True,
        "allow_token_access": allow_token_access,
        "allowed_user_ids": allowed_user_ids or [],
    }
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=token_headers,
        json=body,
    )
    assert r.status_code == 200, f"Create a2a connector failed: {r.text}"
    return r.json()


def _list_discoverable(
    client: TestClient,
    token_headers: dict[str, str],
    consumer_agent_id: str | None = None,
) -> dict:
    params = {}
    if consumer_agent_id:
        params["consumer_agent_id"] = consumer_agent_id
    r = client.get(
        f"{_MCP_PROVIDERS_BASE}/discoverable-agents",
        headers=token_headers,
        params=params,
    )
    assert r.status_code == 200, f"discoverable-agents failed: {r.text}"
    return r.json()


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_a2a_connector_flag_round_trip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    is_agent_to_agent flag persists and is reflected in create / GET / list /
    update responses:

      1. Create connector with is_agent_to_agent=True
      2. GET → flag is True
      3. List → flag is True
      4. Update to is_agent_to_agent=False
      5. GET → flag is False (non-a2a connector no longer included)
    """
    agent = _setup_agent(client, superuser_token_headers, "A2A Flag Round-Trip Agent")
    agent_id = agent["id"]

    # ── Phase 1: Create a2a connector ────────────────────────────────────
    connector = _create_a2a_connector(
        client, superuser_token_headers, agent_id, name="Round-Trip Connector"
    )
    assert connector["is_agent_to_agent"] is True
    assert connector["allow_token_access"] is True
    connector_id = connector["id"]

    # ── Phase 2: GET → flag retained ─────────────────────────────────────
    fetched = get_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id
    )
    assert fetched["is_agent_to_agent"] is True

    # ── Phase 3: List → flag present ─────────────────────────────────────
    listing = list_mcp_connectors(client, superuser_token_headers, agent_id)
    assert listing["count"] >= 1
    found = next((c for c in listing["data"] if c["id"] == connector_id), None)
    assert found is not None
    assert found["is_agent_to_agent"] is True

    # ── Phase 4: Update → clear a2a flag ─────────────────────────────────
    updated = update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_agent_to_agent=False,
    )
    assert updated["is_agent_to_agent"] is False

    # ── Phase 5: GET after update ─────────────────────────────────────────
    fetched2 = get_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id
    )
    assert fetched2["is_agent_to_agent"] is False


def test_discoverable_agents_owner_sees_own_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connector owner always sees their own a2a connectors in discoverable-agents,
    even with an empty allowed_user_ids list.
    """
    agent = _setup_agent(
        client, superuser_token_headers, "Discoverable Owner Agent"
    )
    agent_id = agent["id"]
    connector = _create_a2a_connector(
        client, superuser_token_headers, agent_id,
        name="Discoverable Connector",
        allowed_user_ids=[],
    )
    connector_id = connector["id"]

    result = _list_discoverable(client, superuser_token_headers)
    assert result["count"] >= 1
    ids = [str(e["connector_id"]) for e in result["data"]]
    assert str(connector_id) in ids

    # Response shape
    entry = next(e for e in result["data"] if str(e["connector_id"]) == str(connector_id))
    assert "agent_id" in entry
    assert "agent_name" in entry
    assert "connector_name" in entry
    assert "mode" in entry


def test_discoverable_agents_acl_user_sees_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user in allowed_user_ids sees the connector in discoverable-agents;
    a user NOT in allowed_user_ids does not.

      1. Create producer agent + a2a connector with user_a in allowed_user_ids
      2. user_a can discover it
      3. user_b (not in ACL) cannot discover it
    """
    producer_agent = _setup_agent(
        client, superuser_token_headers, "ACL Discovery Producer Agent"
    )
    agent_id = producer_agent["id"]

    user_a, user_a_headers = create_random_user_with_headers(client)
    user_b, user_b_headers = create_random_user_with_headers(client)

    connector = _create_a2a_connector(
        client, superuser_token_headers, agent_id,
        name="ACL Discoverable Connector",
        allowed_user_ids=[user_a["id"]],
    )
    connector_id = connector["id"]

    # ── user_a can discover ────────────────────────────────────────────────
    result_a = _list_discoverable(client, user_a_headers)
    ids_a = [str(e["connector_id"]) for e in result_a["data"]]
    assert str(connector_id) in ids_a, (
        "user_a in allowed_user_ids must see the connector in discoverable-agents"
    )

    # ── user_b cannot discover ─────────────────────────────────────────────
    result_b = _list_discoverable(client, user_b_headers)
    ids_b = [str(e["connector_id"]) for e in result_b["data"]]
    assert str(connector_id) not in ids_b, (
        "user_b NOT in allowed_user_ids must not see the connector"
    )


def test_discoverable_excludes_inactive_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deactivating a connector removes it from discoverable-agents:

      1. Create active a2a connector → appears in discovery
      2. Deactivate it → disappears from discovery
    """
    agent = _setup_agent(
        client, superuser_token_headers, "Inactive Discoverable Agent"
    )
    agent_id = agent["id"]
    connector = _create_a2a_connector(
        client, superuser_token_headers, agent_id, name="Will Deactivate"
    )
    connector_id = connector["id"]

    # ── Active → present ──────────────────────────────────────────────────
    result = _list_discoverable(client, superuser_token_headers)
    ids = [str(e["connector_id"]) for e in result["data"]]
    assert str(connector_id) in ids

    # ── Deactivate ─────────────────────────────────────────────────────────
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_active=False,
    )

    # ── Inactive → absent ─────────────────────────────────────────────────
    result2 = _list_discoverable(client, superuser_token_headers)
    ids2 = [str(e["connector_id"]) for e in result2["data"]]
    assert str(connector_id) not in ids2, (
        "Inactive connector must not appear in discoverable-agents"
    )


def test_discoverable_excludes_non_a2a_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A regular (non-agent2agent) connector does not appear in discoverable-agents,
    even if the user owns it.
    """
    agent = _setup_agent(
        client, superuser_token_headers, "Non-A2A Discovery Agent"
    )
    agent_id = agent["id"]

    # Create a normal connector (is_agent_to_agent=False, the default)
    connector = create_mcp_connector(
        client, superuser_token_headers, agent_id,
        name="Regular Connector",
    )
    connector_id = connector["id"]
    assert connector["is_agent_to_agent"] is False

    result = _list_discoverable(client, superuser_token_headers)
    ids = [str(e["connector_id"]) for e in result["data"]]
    assert str(connector_id) not in ids, (
        "Non-a2a connector must never appear in discoverable-agents"
    )


def test_discoverable_excludes_consumers_own_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When consumer_agent_id is supplied, the connector whose agent_id matches the
    consumer's agent is excluded (an agent connecting to itself is pointless).

      1. Create producer agent A with a2a connector
      2. Create consumer agent B (same owner)
      3. Discovery without consumer_agent_id → connector appears
      4. Discovery with consumer_agent_id=B → connector still appears (B ≠ A)
      5. Discovery with consumer_agent_id=A → connector absent (A == A)
    """
    agent_a = _setup_agent(
        client, superuser_token_headers, "Self-Exclude Producer Agent A"
    )
    agent_b = _setup_agent(
        client, superuser_token_headers, "Self-Exclude Consumer Agent B"
    )

    connector = _create_a2a_connector(
        client, superuser_token_headers, agent_a["id"],
        name="Self-Exclude Connector",
    )
    connector_id = connector["id"]

    # ── Phase 3: No consumer_agent_id → present ───────────────────────────
    result_no_filter = _list_discoverable(client, superuser_token_headers)
    assert str(connector_id) in [
        str(e["connector_id"]) for e in result_no_filter["data"]
    ]

    # ── Phase 4: consumer_agent_id=B → connector (on A) still present ─────
    result_b = _list_discoverable(
        client, superuser_token_headers, consumer_agent_id=agent_b["id"]
    )
    assert str(connector_id) in [
        str(e["connector_id"]) for e in result_b["data"]
    ], "Connector on agent A must appear when consumer is agent B"

    # ── Phase 5: consumer_agent_id=A → connector excluded ─────────────────
    result_a = _list_discoverable(
        client, superuser_token_headers, consumer_agent_id=agent_a["id"]
    )
    assert str(connector_id) not in [
        str(e["connector_id"]) for e in result_a["data"]
    ], "Connector on agent A must be excluded when consumer_agent_id=A (self-connect)"


def test_discoverable_agents_unauthenticated_rejected(
    client: TestClient,
) -> None:
    """Unauthenticated request to discoverable-agents returns 401."""
    r = client.get(f"{_MCP_PROVIDERS_BASE}/discoverable-agents")
    assert r.status_code == 401, (
        f"Expected 401 without auth, got {r.status_code}"
    )


def test_a2a_connector_partial_update_preserves_flag(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Updating only the name of an a2a connector must not clobber the
    is_agent_to_agent flag (exclude_unset=True semantics in the update handler).
    """
    agent = _setup_agent(
        client, superuser_token_headers, "Partial Update A2A Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, agent["id"],
        name="Stable A2A Name",
    )
    connector_id = connector["id"]

    # Update only the name
    updated = update_mcp_connector(
        client, superuser_token_headers, agent["id"], connector_id,
        name="Renamed A2A Connector",
    )
    assert updated["name"] == "Renamed A2A Connector"
    assert updated["is_agent_to_agent"] is True, (
        "is_agent_to_agent must survive a name-only update"
    )
