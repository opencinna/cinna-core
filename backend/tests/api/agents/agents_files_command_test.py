"""
Integration test: /files and /files-all commands via UI (messages route).

Tests the full workflow:
  1. User sends /files on empty workspace → "No files found"
  2. User sends regular message → agent replies "File generated"
  3. User sends /files → only files folder listing
  4. User sends /files-all → all folders listing
  5. Verify /files and /files-all commands never triggered LLM calls

Only agent-env HTTP is stubbed (via StubAgentEnvConnector).
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages, send_message
from tests.utils.session import create_session_via_api


# Workspace tree with files in multiple sections
_WORKSPACE_TREE_MULTI = {
    "files": {
        "name": "files",
        "type": "directory",
        "children": [
            {
                "name": "data.csv",
                "type": "file",
                "path": "files/data.csv",
                "size": 1024,
            },
        ],
    },
    "scripts": {
        "name": "scripts",
        "type": "directory",
        "children": [
            {
                "name": "process.py",
                "type": "file",
                "path": "scripts/process.py",
                "size": 2048,
            },
        ],
    },
    "logs": {
        "name": "logs",
        "type": "directory",
        "children": [
            {
                "name": "session.log",
                "type": "file",
                "path": "logs/session.log",
                "size": 512,
            },
        ],
    },
}


def _get_command_messages(client, headers, session_id):
    """Get system messages that are command responses (have command metadata)."""
    all_msgs = list_messages(client, headers, session_id)
    return [
        m for m in all_msgs
        if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
    ]


def test_files_command_ui_full_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full /files and /files-all command scenario via UI messages route:
      1. Create agent and session
      2. Send /files → "No files found" (empty workspace)
      3. Send regular message → agent replies "File generated"
      4. Send /files → only files folder (not scripts/logs)
      5. Send /files-all → all folders
      6. Verify no LLM calls were made for command messages
    """
    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # Shared adapter so we can modify workspace tree between calls
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # Single stub to track all LLM calls throughout the test
    stub = StubAgentEnvConnector(response_text="File generated")

    with patch("app.services.message_service.agent_env_connector", stub):
        # ── Phase 2: /files on empty workspace ────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/files",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        cmd_msgs = _get_command_messages(client, superuser_token_headers, session_id)
        assert len(cmd_msgs) == 1
        assert "No files found" in cmd_msgs[0]["content"]
        assert cmd_msgs[0]["message_metadata"]["command"] is True
        assert cmd_msgs[0]["message_metadata"]["command_name"] == "/files"

        # /files must NOT call the LLM
        assert len(stub.stream_calls) == 0

        # ── Phase 3: Regular message → agent replies ──────────────────
        send_message(
            client, superuser_token_headers, session_id,
            content="Generate a file for me",
        )
        drain_tasks()

        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent",
        )
        assert len(agent_msgs) == 1
        assert "File generated" in agent_msgs[0]["content"]
        # Regular message DID call the LLM
        assert len(stub.stream_calls) == 1

        # ── Phase 4: Modify workspace to have files in multiple sections ──

        async def _workspace_multi():
            return _WORKSPACE_TREE_MULTI

        shared_adapter.get_workspace_tree = _workspace_multi

        # ── Phase 5: /files → only files folder ──────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/files",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        cmd_msgs = _get_command_messages(client, superuser_token_headers, session_id)
        assert len(cmd_msgs) == 2
        files_response = cmd_msgs[1]["content"]

        # /files shows only the files folder
        assert "**Files**" in files_response
        assert "data.csv" in files_response
        assert "(1.0 KB)" in files_response

        # /files does NOT show scripts or logs
        assert "**Scripts**" not in files_response
        assert "process.py" not in files_response
        assert "**Logs**" not in files_response
        assert "session.log" not in files_response

        # Links are frontend-style (no A2A token)
        assert f"/environment/{env_id}/file?path=" in files_response
        assert "?token=" not in files_response

        assert cmd_msgs[1]["message_metadata"]["command"] is True
        assert cmd_msgs[1]["message_metadata"]["command_name"] == "/files"

        # ── Phase 6: /files-all → all folders ─────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/files-all",
        )
        drain_tasks()
        assert result.get("command_executed") is True

        cmd_msgs = _get_command_messages(client, superuser_token_headers, session_id)
        assert len(cmd_msgs) == 3
        all_response = cmd_msgs[2]["content"]

        # /files-all shows all sections
        assert "**Files**" in all_response
        assert "data.csv" in all_response
        assert "**Scripts**" in all_response
        assert "process.py" in all_response
        assert "**Logs**" in all_response
        assert "session.log" in all_response

        assert cmd_msgs[2]["message_metadata"]["command"] is True
        assert cmd_msgs[2]["message_metadata"]["command_name"] == "/files-all"

        # ── Phase 7: Neither command called the LLM ───────────────────
        assert len(stub.stream_calls) == 1  # unchanged from phase 3
