"""
Integration test: /files command via A2A (streaming).

Tests the full workflow:
  1. A2A client sends /files on empty workspace → "No files found"
  2. A2A client sends regular message → agent replies "File generated"
  3. A2A client sends /files → file listing with token-based links
  4. File link is accessible via shared workspace endpoint
  5. Verify /files commands never triggered LLM calls

Only agent-env HTTP is stubbed (via StubAgentEnvConnector).
"""
import re
from unittest.mock import patch
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.a2a import (
    build_streaming_request,
    parse_sse_events,
    setup_a2a_agent,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages


# Workspace tree returned after agent "generates" a file
_WORKSPACE_TREE_WITH_FILE = {
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
}

# File content served by the shared workspace download endpoint
_FILE_CONTENT = b"id,name,value\n1,test,42\n"


def _a2a_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _extract_event_text(events: list[dict]) -> str:
    """Extract concatenated text from A2A SSE event status message parts."""
    parts_text: list[str] = []
    for e in events:
        msg = e.get("result", {}).get("status", {}).get("message")
        if not msg or "parts" not in msg:
            continue
        for part in msg["parts"]:
            text = part.get("text") or (part.get("root", {}) or {}).get("text", "")
            if text:
                parts_text.append(text)
    return "\n".join(parts_text)


def test_files_command_a2a_full_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full /files command scenario via A2A streaming:
      1. Setup agent with A2A access token
      2. Send /files via A2A → "No files found"
      3. Send regular message → agent replies "File generated"
      4. Send /files → listing with backend URLs and workspace tokens
      5. Verify file is accessible via shared workspace endpoint
      6. Verify /files commands never triggered LLM calls
    """
    # ── Phase 1: Setup A2A agent ──────────────────────────────────────
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Files Command Agent",
    )
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    a2a_token = token_data["token"]

    # Shared adapter for controlling workspace tree between calls
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # Single stub to track all LLM calls
    stub = StubAgentEnvConnector(response_text="File generated")

    with patch("app.services.message_service.agent_env_connector", stub):
        # ── Phase 2: /files on empty workspace via A2A ────────────────
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=_a2a_headers(a2a_token),
            json=build_streaming_request("/files"),
        )
        drain_tasks()
        assert resp.status_code == 200

        events = parse_sse_events(resp.text)
        assert len(events) >= 1

        # Single completed event with "No files found"
        first = events[0]["result"]
        assert first["status"]["state"] == "completed"
        assert first["final"] is True
        response_text = _extract_event_text(events)
        assert "No files found" in response_text

        # /files must NOT call the LLM
        assert len(stub.stream_calls) == 0

        # Extract task_id (= session_id) for continuing the session
        task_id = first["taskId"]

        # Command response is a system message with command metadata
        user_msgs = get_messages_by_role(client, superuser_token_headers, task_id, "user")
        all_msgs = list_messages(client, superuser_token_headers, task_id)
        cmd_msgs = [
            m for m in all_msgs
            if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
        ]
        assert len(cmd_msgs) == 1
        assert cmd_msgs[0]["answers_to_message_id"] == user_msgs[0]["id"]
        assert cmd_msgs[0]["message_metadata"]["command"] is True
        assert cmd_msgs[0]["message_metadata"]["command_name"] == "/files"

        # ── Phase 3: Regular message → agent replies ──────────────────
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=_a2a_headers(a2a_token),
            json=build_streaming_request(
                "Generate a file for me", task_id=task_id,
            ),
        )
        drain_tasks()
        assert resp.status_code == 200

        events = parse_sse_events(resp.text)
        assert len(events) >= 2  # working + completed

        agent_text = _extract_event_text(events)
        assert "File generated" in agent_text
        # Regular message DID call the LLM
        assert len(stub.stream_calls) == 1

        # ── Phase 4: Modify workspace and download behavior ──────────

        async def _workspace_with_file():
            return _WORKSPACE_TREE_WITH_FILE

        async def _download_item(path):
            """Async generator that yields file content directly."""
            yield _FILE_CONTENT

        shared_adapter.get_workspace_tree = _workspace_with_file
        shared_adapter.download_workspace_item = _download_item

        # ── Phase 5: /files → listing with A2A links ─────────────────
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=_a2a_headers(a2a_token),
            json=build_streaming_request("/files", task_id=task_id),
        )
        drain_tasks()
        assert resp.status_code == 200

        events = parse_sse_events(resp.text)
        assert len(events) >= 1

        files_response = _extract_event_text(events)

        # File listing present
        assert "**Files**" in files_response
        assert "data.csv" in files_response
        assert "(1.0 KB)" in files_response

        # Links are A2A-style with workspace token
        assert "?token=" in files_response
        assert f"/shared/workspace/{env_id}/view/" in files_response

        # Command response links back to the user command message
        user_msgs = get_messages_by_role(client, superuser_token_headers, task_id, "user")
        all_msgs = list_messages(client, superuser_token_headers, task_id)
        cmd_msgs = [
            m for m in all_msgs
            if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
        ]
        assert len(cmd_msgs) == 2
        assert cmd_msgs[1]["answers_to_message_id"] == user_msgs[2]["id"]
        assert cmd_msgs[1]["message_metadata"]["command"] is True

        # ── Phase 6: Verify file is accessible via link ───────────────
        url_match = re.search(r"\[data\.csv\]\(([^)]+)\)", files_response)
        assert url_match is not None, (
            f"File URL not found in response: {files_response}"
        )
        file_url = url_match.group(1)

        # Strip host to get path+query for TestClient
        parsed = urlparse(file_url)
        file_path = f"{parsed.path}?{parsed.query}"

        file_resp = client.get(file_path)
        assert file_resp.status_code == 200
        assert _FILE_CONTENT in file_resp.content

        # ── Phase 7: /files still did not call the LLM ───────────────
        assert len(stub.stream_calls) == 1  # unchanged from phase 3
