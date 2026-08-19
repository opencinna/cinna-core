"""
Integration tests: AgentPublic capability flags — has_email_integration,
has_mcp_connectors, has_webhooks.

Verifies that GET /agents/ (the list endpoint) returns correct computed boolean
capability flags per agent:

  1. Both flags default to False on fresh agents (no integrations configured).
  2. Enabling email integration, creating an MCP connector, and creating a
     webhook on agent A flips all three flags to True for that agent.
  3. Agent B (same owner, no integrations) keeps all flags False throughout —
     proving the batched capability query is isolated per-agent and does not
     bleed across agents.
  4. Disabling the webhook (setting enabled=False) flips has_webhooks back to
     False for agent A — validates the "actively enabled" predicate, not mere
     existence.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import (
    configure_email_integration,
    create_agent_via_api,
    enable_email_integration,
    list_agents,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.mail_server import create_imap_server, create_smtp_server
from tests.utils.mcp import create_mcp_connector
from tests.utils.webhook import create_session_webhook, update_webhook

API = settings.API_V1_STR


def test_capability_flags_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Scenario: capability flags on GET /agents/ list.

      1. Create two agents (A and B) for the same user.
      2. GET /agents/ — both agents appear; all three flags are False on both.
      3. For agent A: enable email integration (IMAP+SMTP+configure+enable),
         create an active MCP connector, create an enabled session webhook.
      4. GET /agents/ — agent A has all three flags True; agent B still has all
         three flags False (per-agent isolation proof).
      5. Disable the webhook on agent A (enabled=False).
      6. GET /agents/ — agent A now has has_webhooks=False; email and MCP flags
         remain True (validates the "actively enabled" predicate).
    """

    # ── Phase 1: Create two agents ────────────────────────────────────────

    agent_a = create_agent_via_api(client, superuser_token_headers, name="Cap Flags Agent A")
    drain_tasks()
    # Re-fetch to get the active_environment_id set by the background env-create task
    r = client.get(
        f"{API}/agents/{agent_a['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    agent_a = r.json()
    agent_a_id: str = agent_a["id"]
    assert agent_a["active_environment_id"] is not None, (
        "Agent A must have an active environment before email-integration can be configured"
    )

    agent_b = create_agent_via_api(client, superuser_token_headers, name="Cap Flags Agent B")
    drain_tasks()
    agent_b_id: str = agent_b["id"]

    # ── Phase 2: Baseline — all flags False ───────────────────────────────

    body = list_agents(client, superuser_token_headers)
    assert "data" in body
    assert "count" in body

    agents_by_id = {a["id"]: a for a in body["data"]}

    # Both agents must appear in the list
    assert agent_a_id in agents_by_id, "Agent A missing from /agents/ list"
    assert agent_b_id in agents_by_id, "Agent B missing from /agents/ list"

    for flag in (
        "has_email_integration",
        "has_mcp_connectors",
        "has_webhooks",
        "git_versioning_enabled",
    ):
        assert agents_by_id[agent_a_id][flag] is False, (
            f"Agent A: expected {flag}=False at baseline, got {agents_by_id[agent_a_id][flag]}"
        )
        assert agents_by_id[agent_b_id][flag] is False, (
            f"Agent B: expected {flag}=False at baseline, got {agents_by_id[agent_b_id][flag]}"
        )

    # ── Phase 3: Enable integrations on agent A ───────────────────────────

    # 3a. Email integration: create mail servers → configure → enable
    imap_server = create_imap_server(client, superuser_token_headers)
    smtp_server = create_smtp_server(client, superuser_token_headers)
    configure_email_integration(
        client,
        superuser_token_headers,
        agent_a_id,
        incoming_server_id=imap_server["id"],
        outgoing_server_id=smtp_server["id"],
    )
    enable_email_integration(client, superuser_token_headers, agent_a_id)

    # 3b. MCP connector — created with is_active=True by default
    create_mcp_connector(client, superuser_token_headers, agent_a_id, name="Test MCP")

    # 3c. Session webhook — created with enabled=True by default
    webhook = create_session_webhook(
        client, superuser_token_headers, agent_a_id, name="Test Session Webhook"
    )
    webhook_pk: str = webhook["id"]

    # ── Phase 4: Flags True on A, False on B ──────────────────────────────

    body = list_agents(client, superuser_token_headers)
    agents_by_id = {a["id"]: a for a in body["data"]}

    assert agents_by_id[agent_a_id]["has_email_integration"] is True, (
        "Agent A: expected has_email_integration=True after enabling integration"
    )
    assert agents_by_id[agent_a_id]["has_mcp_connectors"] is True, (
        "Agent A: expected has_mcp_connectors=True after creating active MCP connector"
    )
    assert agents_by_id[agent_a_id]["has_webhooks"] is True, (
        "Agent A: expected has_webhooks=True after creating enabled webhook"
    )

    # Agent B must be completely unaffected
    assert agents_by_id[agent_b_id]["has_email_integration"] is False, (
        "Agent B: has_email_integration must remain False (cross-agent bleed)"
    )
    assert agents_by_id[agent_b_id]["has_mcp_connectors"] is False, (
        "Agent B: has_mcp_connectors must remain False (cross-agent bleed)"
    )
    assert agents_by_id[agent_b_id]["has_webhooks"] is False, (
        "Agent B: has_webhooks must remain False (cross-agent bleed)"
    )

    # ── Phase 5: Disable webhook → has_webhooks flips back to False ───────

    update_webhook(
        client, superuser_token_headers, agent_a_id, webhook_pk, enabled=False
    )

    body = list_agents(client, superuser_token_headers)
    agents_by_id = {a["id"]: a for a in body["data"]}

    assert agents_by_id[agent_a_id]["has_webhooks"] is False, (
        "Agent A: expected has_webhooks=False after disabling the webhook"
    )
    # Email and MCP flags must still be True — disabling webhook only
    assert agents_by_id[agent_a_id]["has_email_integration"] is True, (
        "Agent A: has_email_integration should remain True after webhook disable"
    )
    assert agents_by_id[agent_a_id]["has_mcp_connectors"] is True, (
        "Agent A: has_mcp_connectors should remain True after webhook disable"
    )
