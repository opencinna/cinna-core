"""
Integration test: /webapp command via UI (messages route).

Tests:
  1. /webapp when webapp_enabled=False → "No Web App available"
  2. /webapp when webapp_enabled=True but no shares → "No Web App available"
  3. /webapp when webapp_enabled=True and active share exists → returns share URL
  4. /webapp when share has a security code → returns URL and access code
  5. /webapp never triggers an LLM call
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, send_message
from tests.utils.session import create_session_via_api
from tests.utils.webapp_share import create_webapp_share, list_webapp_shares, update_webapp_share


def test_webapp_command_full_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Full /webapp command scenario via UI messages route:
      1. Create agent (webapp_enabled=False by default), create session
      2. Send /webapp → "No Web App available" (webapp disabled)
      3. Enable webapp on agent, send /webapp → "No Web App available" (no shares)
      4. Create a webapp share, send /webapp → returns the share URL (no code)
      5. Create a share with security code, send /webapp → returns URL and access code
      6. Verify /webapp never triggered an LLM call
    """
    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    stub = StubAgentEnvConnector(response_text="OK")

    with patch("app.services.message_service.agent_env_connector", stub):
        # ── Phase 2: /webapp with webapp_enabled=False ────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/webapp",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent",
        )
        assert len(agent_msgs) == 1
        assert "No Web App available for this agent" in agent_msgs[0]["content"]
        assert agent_msgs[0]["message_metadata"]["command"] is True
        assert agent_msgs[0]["message_metadata"]["command_name"] == "/webapp"

        # ── Phase 3: Enable webapp, but no shares ─────────────────────
        update_agent(client, superuser_token_headers, agent_id, webapp_enabled=True)

        result = send_message(
            client, superuser_token_headers, session_id, content="/webapp",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent",
        )
        assert len(agent_msgs) == 2
        assert "No Web App available for this agent" in agent_msgs[1]["content"]

        # ── Phase 4: Create share, /webapp returns URL ────────────────
        share = create_webapp_share(
            client, superuser_token_headers, agent_id,
        )
        share_url = share["share_url"]

        result = send_message(
            client, superuser_token_headers, session_id, content="/webapp",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent",
        )
        assert len(agent_msgs) == 3
        assert share_url in agent_msgs[2]["content"]
        assert "Web App" in agent_msgs[2]["content"]
        assert "Access Code" not in agent_msgs[2]["content"]
        assert agent_msgs[2]["message_metadata"]["command"] is True
        assert agent_msgs[2]["message_metadata"]["command_name"] == "/webapp"

        # ── Phase 5: Deactivate first share, create one with security code ─
        # Deactivate the existing share so the new code-protected one is picked
        shares = list_webapp_shares(client, superuser_token_headers, agent_id)
        for s in shares:
            update_webapp_share(
                client, superuser_token_headers, agent_id, s["id"],
                is_active=False,
            )

        code_share = create_webapp_share(
            client, superuser_token_headers, agent_id,
            require_security_code=True,
        )
        code_share_url = code_share["share_url"]
        security_code = code_share["security_code"]
        assert security_code is not None

        result = send_message(
            client, superuser_token_headers, session_id, content="/webapp",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent",
        )
        assert len(agent_msgs) == 4
        assert code_share_url in agent_msgs[3]["content"]
        assert "Access Code" in agent_msgs[3]["content"]
        assert security_code in agent_msgs[3]["content"]

        # ── Phase 6: No LLM calls were made ──────────────────────────
        assert len(stub.stream_calls) == 0
