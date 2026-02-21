"""
Integration test: prompt sync lifecycle (backend ↔ environment).

Exercises the full bidirectional prompt sync through FastAPI TestClient:
- Backend → Environment: user updates prompts, syncs to running environment
- Environment → Backend: building session completes, auto-sync pulls updated prompts

Agent-env HTTP is stubbed via EnvironmentTestAdapter (persistent instance)
and StubAgentEnvConnector (for streaming).
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import (
    create_agent_via_api,
    get_agent,
    update_agent,
    sync_agent_prompts,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, send_message
from tests.utils.session import create_session_via_api


def test_prompt_sync_building_session_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full prompt sync lifecycle:
      1. Create agent (auto-creates environment)
      2. Update agent prompts via API
      3. Sync prompts to environment (backend → env)
      4. Verify prompts reached the environment adapter
      5. Simulate agent updating prompts in environment during building session
      6. Send building mode message, agent replies
      7. Stream completes → auto-sync pulls updated prompts (env → backend)
      8. Verify agent model has new prompts from environment
    """
    # ── Setup: persistent adapter so state survives across calls ───────
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 1: Create agent ─────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    # ── Phase 2: Update prompts via PUT /agents/{id} ──────────────────
    initial_workflow = "## Invoice Parser Workflow\nRun parse_invoices.py then summarize."
    initial_entrypoint = "Check my inbox for invoices"
    initial_refiner = "## Defaults\n- Date range: last 7 days"

    updated = update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=initial_workflow,
        entrypoint_prompt=initial_entrypoint,
        refiner_prompt=initial_refiner,
    )
    assert updated["workflow_prompt"] == initial_workflow
    assert updated["entrypoint_prompt"] == initial_entrypoint
    assert updated["refiner_prompt"] == initial_refiner

    # ── Phase 3: Sync prompts to environment (backend → env) ──────────
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    assert shared_adapter.prompts_set.get("workflow_prompt") == initial_workflow
    assert shared_adapter.prompts_set.get("entrypoint_prompt") == initial_entrypoint
    assert shared_adapter.prompts_set.get("refiner_prompt") == initial_refiner

    # ── Phase 4: Simulate agent updating prompts in the environment ───
    # In reality, the building agent modifies WORKFLOW_PROMPT.md etc.
    env_updated_workflow = (
        "## Invoice Parser Workflow v2\n"
        "1. Run parse_invoices.py --input=./files/emails.json\n"
        "2. Run summarize_invoices.py --data=./files/parsed.csv\n"
        "3. Present summary to user in natural language."
    )
    env_updated_entrypoint = "Parse my latest invoices and show a summary"
    env_updated_refiner = "## Defaults\n- Date range: last 30 days\n- Output: summary table"

    shared_adapter.prompts_set["workflow_prompt"] = env_updated_workflow
    shared_adapter.prompts_set["entrypoint_prompt"] = env_updated_entrypoint
    shared_adapter.prompts_set["refiner_prompt"] = env_updated_refiner

    # ── Phase 5: Create building session and send message ─────────────
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    session_id = session_data["id"]

    agent_response = "I've updated the workflow to include invoice parsing and summarization."
    stub_agent_env = StubAgentEnvConnector(response_text=agent_response)

    with patch("app.services.message_service.agent_env_connector", stub_agent_env):
        send_message(
            client, superuser_token_headers, session_id,
            content="Build an invoice parser workflow",
        )
        # Drain tasks: process_pending_messages → streaming → STREAM_COMPLETED
        # → handle_stream_completed_event → sync_agent_prompts_from_environment
        drain_tasks()

    # ── Phase 6: Verify agent response was stored ─────────────────────
    agent_msgs = get_messages_by_role(
        client, superuser_token_headers, session_id, "agent"
    )
    assert len(agent_msgs) >= 1
    assert agent_response in agent_msgs[0]["content"]

    # ── Phase 7: Verify auto-sync pulled updated prompts (env → backend)
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == env_updated_workflow
    assert agent_after["entrypoint_prompt"] == env_updated_entrypoint
    assert agent_after["refiner_prompt"] == env_updated_refiner
