"""
Integration tests: /run command execution and streaming (Plan #2).

Tests cover:
  1. /run (no args) — list mode: returns markdown table of cached commands
  2. /run (no args) with empty cache — inline error, no queue
  3. /run:<name> — exec mode: returns queued response, system message persisted
  4. /run <name> (space form) — behaves identically to /run:<name>
  5. /run:<unknown> — inline error before queueing
  6. /run with invalid name format — inline error before queueing
  7. collect_pending_batches — correctly partitions mixed pending messages
  8. Command stream system message has correct metadata after streaming

Notes:
  - These tests use the environment adapter stub (auto-patched by agents/conftest.py)
  - The StubAgentEnvConnector is extended with stream_command() support.
  - No agent-env Docker container is needed.
"""
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector, build_command_stream_events
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages, send_message
from tests.utils.session import create_session_via_api


# Sample CLI_COMMANDS.yaml content used across tests (as bytes — workspace_files stores bytes)
_CLI_COMMANDS_YAML = (
    b"commands:\n"
    b"  - name: check\n"
    b"    command: uv run /app/workspace/scripts/check.py\n"
    b"    description: Run the check script\n"
    b"  - name: validate\n"
    b"    command: python /app/workspace/scripts/validate.py\n"
    b"    description: Validate workspace files\n"
)


def _get_system_command_messages(client, headers, session_id):
    """Return system messages with command=True and routing=command_stream."""
    all_msgs = list_messages(client, headers, session_id)
    return [
        m for m in all_msgs
        if m["role"] == "system"
        and (m.get("message_metadata") or {}).get("command") is True
        and (m.get("message_metadata") or {}).get("routing") == "command_stream"
    ]


def _get_sync_command_messages(client, headers, session_id):
    """Return system messages with command=True but no command_stream routing (sync commands)."""
    all_msgs = list_messages(client, headers, session_id)
    return [
        m for m in all_msgs
        if m["role"] == "system"
        and (m.get("message_metadata") or {}).get("command") is True
        and (m.get("message_metadata") or {}).get("routing") != "command_stream"
    ]




def test_run_list_mode_returns_table(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /run with no args returns a markdown table of available commands.

    Scenario:
      1. Create agent, populate CLI commands cache
      2. Send /run → synchronous response with command table
      3. Verify response has command_executed=True (not queued)
      4. Verify system message contains markdown table with command names
    """
    # ── Phase 1: Create agent and populate CLI commands cache ──────────────
    EnvironmentTestAdapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}

    try:
        agent = create_agent_via_api(client, superuser_token_headers)
        drain_tasks()
        agent = get_agent(client, superuser_token_headers, agent["id"])
        session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
        session_id = session_data["id"]

        # Trigger cache population via commands endpoint
        client.get(
            f"{settings.API_V1_STR}/sessions/{session_id}/commands",
            headers=superuser_token_headers,
        )
        drain_tasks()

        stub = StubAgentEnvConnector(response_text="ok")

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            # ── Phase 2: /run → list mode ─────────────────────────────────────
            result = send_message(
                client, superuser_token_headers, session_id, content="/run",
            )
            drain_tasks()

        # ── Phase 3: Verify synchronous response ─────────────────────────────
        assert result.get("command_executed") is True, f"Expected command_executed, got: {result}"
        assert not result.get("queued"), "List mode should NOT queue"

        # ── Phase 4: Verify system message content ────────────────────────────
        sync_msgs = _get_sync_command_messages(client, superuser_token_headers, session_id)
        assert len(sync_msgs) >= 1, "Expected at least one sync command message"
        content = sync_msgs[-1]["content"]
        assert "check" in content, f"Expected 'check' in table, got: {content}"
        assert "validate" in content, f"Expected 'validate' in table, got: {content}"
        assert "|" in content, "Expected markdown table with | characters"

        # No LLM calls should have been made
        assert len(stub.stream_calls) == 0, "List mode must not call LLM"

    finally:
        EnvironmentTestAdapter.workspace_files = {}


def test_run_list_mode_empty_cache_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /run with no args when cache is empty returns an inline error — no queue created.

    Scenario:
      1. Create agent (no CLI_COMMANDS.yaml)
      2. Send /run → synchronous error response
      3. Verify command_executed=True, message contains "No commands configured"
    """
    # ── Phase 1: Create agent (empty cache) ───────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    stub = StubAgentEnvConnector(response_text="ok")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: /run with empty cache ───────────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/run",
        )
        drain_tasks()

    # ── Phase 3: Verify error response ────────────────────────────────────
    assert result.get("command_executed") is True, f"Expected command_executed, got: {result}"
    assert not result.get("queued"), "Error in list mode should NOT queue"

    sync_msgs = _get_sync_command_messages(client, superuser_token_headers, session_id)
    assert len(sync_msgs) >= 1
    content = sync_msgs[-1]["content"]
    assert "No commands" in content or "CLI_COMMANDS" in content, (
        f"Expected 'No commands configured' error, got: {content}"
    )

    # No LLM calls
    assert len(stub.stream_calls) == 0


def test_run_exec_mode_queues_and_streams(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /run:<name> queues a command message and executes it via stream_command.

    Scenario:
      1. Create agent, populate CLI commands cache with "check" command
      2. Send /run:check → response has queued=True
      3. Verify user message was created as pending, then marked sent
      4. Verify system message was created with correct metadata after drain
      5. Verify stream_command was called on the stub with resolved_command
      6. Verify system message has exec_exit_code=0 in metadata after stream
    """
    # ── Phase 1: Create agent and populate CLI commands cache ──────────────
    shared_adapter = EnvironmentTestAdapter()
    shared_adapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    # Trigger cache population
    client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=superuser_token_headers,
    )
    drain_tasks()

    # Build stub command events for "check"
    exec_id = str(uuid.uuid4())
    command_output = "Checking workspace...\nAll checks passed.\n"
    cmd_events = build_command_stream_events(
        exec_id=exec_id,
        command="uv run /app/workspace/scripts/check.py",
        stdout_lines=[command_output],
        exit_code=0,
        duration_seconds=1.2,
    )
    stub = StubAgentEnvConnector(
        response_text="ok",
        command_events=cmd_events,
    )

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: Send /run:check ──────────────────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/run:check",
        )
        # Phase 3: Drain tasks to process pending command message
        drain_tasks()

    # ── Phase 4: Verify queued response ───────────────────────────────────
    assert result.get("queued") is True, f"Expected queued=True, got: {result}"
    assert not result.get("command_executed"), "Exec mode should not set command_executed"

    # ── Phase 5: Verify stream_command was called ─────────────────────────
    assert len(stub.stream_command_calls) == 1, (
        f"Expected 1 stream_command call, got {len(stub.stream_command_calls)}"
    )
    cmd_call = stub.stream_command_calls[0]
    assert "check.py" in cmd_call["resolved_command"], (
        f"Expected check.py in resolved_command, got: {cmd_call['resolved_command']}"
    )

    # LLM stream_chat should NOT have been called
    assert len(stub.stream_calls) == 0, "Command exec must not call LLM stream"

    # ── Phase 6: Verify system command message metadata ───────────────────
    cmd_stream_msgs = _get_system_command_messages(client, superuser_token_headers, session_id)
    assert len(cmd_stream_msgs) == 1, (
        f"Expected 1 command stream system message, got {len(cmd_stream_msgs)}"
    )
    msg = cmd_stream_msgs[0]
    meta = msg.get("message_metadata") or {}
    assert meta.get("command") is True
    assert meta.get("routing") == "command_stream"
    assert meta.get("synthesized") is True
    assert meta.get("streaming_in_progress") is False, "Streaming should be complete after drain"
    assert meta.get("exec_exit_code") == 0, f"Expected exit_code=0, got: {meta.get('exec_exit_code')}"
    assert "check.py" in (meta.get("resolved_command") or ""), (
        f"Expected check.py in resolved_command metadata"
    )
    assert command_output in msg["content"], (
        f"Expected command output in message content, got: {msg['content'][:200]}"
    )


def test_run_space_form_behaves_like_colon_form(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /run validate (space form) behaves identically to /run:validate (colon form).

    Scenario:
      1. Create agent, populate CLI commands cache
      2. Send "/run validate" → response has queued=True
      3. Verify stream_command called with validate's resolved_command
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    shared_adapter = EnvironmentTestAdapter()
    shared_adapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=superuser_token_headers,
    )
    drain_tasks()

    cmd_events = build_command_stream_events(
        exec_id=str(uuid.uuid4()),
        command="python /app/workspace/scripts/validate.py",
        stdout_lines=["Validation OK\n"],
        exit_code=0,
    )
    stub = StubAgentEnvConnector(command_events=cmd_events)

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: Send "/run validate" (space form) ────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/run validate",
        )
        drain_tasks()

    # ── Phase 3: Verify ───────────────────────────────────────────────────
    assert result.get("queued") is True, f"Expected queued=True, got: {result}"

    assert len(stub.stream_command_calls) == 1
    cmd_call = stub.stream_command_calls[0]
    assert "validate.py" in cmd_call["resolved_command"], (
        f"Expected validate.py, got: {cmd_call['resolved_command']}"
    )
    assert len(stub.stream_calls) == 0, "Space form must not call LLM"


def test_run_unknown_command_returns_inline_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /run:<unknown> returns an inline error without queuing any message.

    Scenario:
      1. Create agent, populate CLI commands cache with "check" only
      2. Send /run:notexists → synchronous error, command_executed=True, not queued
      3. Verify no command_stream system message was created
      4. Verify error message mentions the unknown name
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    shared_adapter = EnvironmentTestAdapter()
    shared_adapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=superuser_token_headers,
    )
    drain_tasks()

    stub = StubAgentEnvConnector()

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: /run:notexists ────────────────────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/run:notexists",
        )
        drain_tasks()

    # ── Phase 3: Verify error response ────────────────────────────────────
    assert result.get("command_executed") is True, f"Expected command_executed, got: {result}"
    assert not result.get("queued"), "Unknown command must not queue"

    # No command_stream system message
    cmd_stream_msgs = _get_system_command_messages(client, superuser_token_headers, session_id)
    assert len(cmd_stream_msgs) == 0, "No command_stream message should be created for unknown command"

    # Sync error message exists
    sync_msgs = _get_sync_command_messages(client, superuser_token_headers, session_id)
    assert len(sync_msgs) >= 1
    assert "notexists" in sync_msgs[-1]["content"], (
        f"Error message should mention unknown name, got: {sync_msgs[-1]['content']}"
    )

    # No LLM and no command streaming
    assert len(stub.stream_calls) == 0
    assert len(stub.stream_command_calls) == 0


def test_run_invalid_name_format_returns_inline_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /run:<invalid> (bad name format) returns an inline error without queuing.

    Names must match ^[a-zA-Z0-9_-]{1,64}$. A name with spaces or special chars
    should trigger a validation error.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    stub = StubAgentEnvConnector()

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # Name contains shell metacharacter
        result = send_message(
            client, superuser_token_headers, session_id, content="/run:check;rm -rf /",
        )
        drain_tasks()

    assert result.get("command_executed") is True or result.get("queued") is not True, (
        f"Bad name should not queue: {result}"
    )
    # Ensure no command stream calls
    assert len(stub.stream_command_calls) == 0


def test_run_non_zero_exit_code_not_an_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    A command that exits with non-zero code should have message.status="" (not "error").

    Non-zero exit is a normal outcome. Only infrastructure failures set status="error".
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    shared_adapter = EnvironmentTestAdapter()
    shared_adapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=superuser_token_headers,
    )
    drain_tasks()

    # Command exits with code 1
    cmd_events = build_command_stream_events(
        exec_id=str(uuid.uuid4()),
        command="uv run /app/workspace/scripts/check.py",
        stdout_lines=["Check failed: missing file\n"],
        exit_code=1,
        duration_seconds=0.3,
    )
    stub = StubAgentEnvConnector(command_events=cmd_events)

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        result = send_message(
            client, superuser_token_headers, session_id, content="/run:check",
        )
        drain_tasks()

    assert result.get("queued") is True

    cmd_stream_msgs = _get_system_command_messages(client, superuser_token_headers, session_id)
    assert len(cmd_stream_msgs) == 1
    msg = cmd_stream_msgs[0]
    meta = msg.get("message_metadata") or {}

    # exit_code=1 is stored in metadata
    assert meta.get("exec_exit_code") == 1
    # BUT message.status should NOT be "error"
    assert msg.get("status", "") == "", (
        f"Non-zero exit should not set message status='error', got: {msg.get('status')}"
    )
    assert meta.get("streaming_in_progress") is False


def test_synthesized_flag_in_tool_event(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The tool event emitted for /run command streaming has synthesized=True in metadata.

    This is stored in streaming_events in the system message.
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    shared_adapter = EnvironmentTestAdapter()
    shared_adapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=superuser_token_headers,
    )
    drain_tasks()

    cmd_events = build_command_stream_events(
        exec_id=str(uuid.uuid4()),
        command="uv run /app/workspace/scripts/check.py",
        stdout_lines=["ok\n"],
        exit_code=0,
    )
    stub = StubAgentEnvConnector(command_events=cmd_events)

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        result = send_message(
            client, superuser_token_headers, session_id, content="/run:check",
        )
        drain_tasks()

    assert result.get("queued") is True

    cmd_stream_msgs = _get_system_command_messages(client, superuser_token_headers, session_id)
    assert len(cmd_stream_msgs) == 1
    meta = cmd_stream_msgs[0].get("message_metadata") or {}

    # Verify streaming_events contains the tool event with synthesized=True
    streaming_events = meta.get("streaming_events") or []
    tool_events = [e for e in streaming_events if e.get("type") == "tool"]
    assert len(tool_events) == 1, f"Expected 1 tool event, got: {len(tool_events)}"
    assert tool_events[0].get("metadata", {}).get("synthesized") is True, (
        f"Tool event should have synthesized=True, got: {tool_events[0]}"
    )
    assert tool_events[0].get("tool_name") == "bash"
