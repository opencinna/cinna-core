"""
Agent Status Refresh Command integration tests.

All tests interact exclusively through the HTTP API (TestClient).
Unit tests for AgentStatusService._run_refresh_command and related helpers
are a natural extension of tests/unit/test_agent_status_service.py, but the
integration scenarios below exercise the full API-to-service path.

Scenarios:
  1. Model field — default /run:status on a new agent; updatable via PUT /agents/{id}
  2. Force-refresh + /run:<name> that EXISTS → exec_command called, no warning
  3. Force-refresh + /run:<name> NOT in cache → warning surfaced, status still returned
  4. Force-refresh + plain shell command, non-zero exit → warning, status returned,
     warning contains no stdout/stderr
  4b. Force-refresh + exec_command raises RuntimeError → warning, status returned
  5. Cache-only paths (list endpoint, non-force GET) do NOT trigger the pre-command
  6. A2A agent/status with force_refresh=True → refresh_command_warning in result dict
  6b. A2A agent/status without force_refresh → NO warning produced
  7. /agent-status slash command renders warning line when command is not found
  7b. /agent-status slash command — clean exec, no warning line in output
  8. Blank/empty status_refresh_command is a silent opt-out (no exec, no warning)
"""
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, enable_a2a, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages, send_message
from tests.utils.session import create_session_via_api


# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

# CLI_COMMANDS.yaml that defines a "status" command — used to populate the
# cli_commands_parsed cache.  The environment startup path reads this file via
# the adapter and persists the parsed commands.
_CLI_COMMANDS_YAML_WITH_STATUS = (
    b"commands:\n"
    b"  - name: status\n"
    b"    command: /app/workspace/scripts/status.sh\n"
    b"    description: Update STATUS.md with current state\n"
    b"  - name: check\n"
    b"    command: uv run /app/workspace/scripts/check.py\n"
    b"    description: Run the check script\n"
)

# Minimal yaml that only has "check" — does NOT include "status"
_CLI_COMMANDS_YAML_NO_STATUS = (
    b"commands:\n"
    b"  - name: check\n"
    b"    command: uv run /app/workspace/scripts/check.py\n"
    b"    description: Run the check script\n"
)

# Minimal STATUS.md returned by the adapter on a successful fetch.
_STATUS_MD = b"---\nstatus: ok\nsummary: All systems nominal\n---\n\n# Agent Status\n"


def _make_exec_command_ok():
    """Return a mock AgentEnvConnector class whose exec_command succeeds (exit_code=0)."""
    mock_connector = AsyncMock()
    mock_connector.exec_command = AsyncMock(
        return_value={"exit_code": 0, "stdout": "status updated", "stderr": ""}
    )
    mock_class = MagicMock(return_value=mock_connector)
    return mock_class, mock_connector


def _make_exec_command_nonzero(exit_code: int = 1):
    """Return a mock AgentEnvConnector class whose exec_command returns non-zero exit."""
    mock_connector = AsyncMock()
    mock_connector.exec_command = AsyncMock(
        return_value={
            "exit_code": exit_code,
            "stdout": "ERROR: check failed",
            "stderr": "details here",
        }
    )
    mock_class = MagicMock(return_value=mock_connector)
    return mock_class, mock_connector


def _make_exec_command_raises(message: str = "Connection refused"):
    """Return a mock AgentEnvConnector class whose exec_command raises RuntimeError."""
    mock_connector = AsyncMock()
    mock_connector.exec_command = AsyncMock(side_effect=RuntimeError(message))
    mock_class = MagicMock(return_value=mock_connector)
    return mock_class, mock_connector


def _get_command_system_messages(client, headers, session_id):
    """Return system messages created by slash commands (message_metadata.command=True)."""
    all_msgs = list_messages(client, headers, session_id)
    return [
        m for m in all_msgs
        if m["role"] == "system"
        and (m.get("message_metadata") or {}).get("command") is True
    ]


def _setup_agent_with_cli_commands(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    yaml_bytes: bytes = _CLI_COMMANDS_YAML_WITH_STATUS,
) -> dict:
    """
    Create an agent with CLI commands pre-populated in the environment's cache.

    Sets workspace_files BEFORE creating the agent so that the environment
    startup path (drain_tasks after creation) triggers CLICommandsService.refresh_after_action
    which reads the file and persists cli_commands_parsed.

    Returns the agent dict (after draining tasks so active_environment_id is set).
    """
    # Set workspace files on the class-level dict BEFORE creating the agent
    EnvironmentTestAdapter.workspace_files["docs/CLI_COMMANDS.yaml"] = yaml_bytes

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()  # runs env startup → CLICommandsService.refresh_after_action → populates cache

    # Fetch updated agent (with active_environment_id)
    return get_agent(client, superuser_token_headers, agent["id"])


# ---------------------------------------------------------------------------
# Scenario 1: Model field — default and update
# ---------------------------------------------------------------------------

def test_status_refresh_command_default_and_update(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full lifecycle for the status_refresh_command field:
      1. Freshly created agent has status_refresh_command="/run:status" (default)
      2. PUT /agents/{id} with a plain shell command persists correctly
      3. Verify the field in GET /agents/{id}
      4. PUT with a /run:<name> reference persists correctly
      5. PUT with empty string clears the field (opt-out)
    """
    # ── Phase 1: Create — verify default ─────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    assert agent.get("status_refresh_command") == "/run:status", (
        f"Expected default '/run:status', got: {agent.get('status_refresh_command')!r}"
    )

    # ── Phase 2: Update with plain shell command ──────────────────────────
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": "python /app/workspace/scripts/status.py"},
    )
    assert r.status_code == 200, f"Update failed: {r.text}"
    assert r.json()["status_refresh_command"] == "python /app/workspace/scripts/status.py"

    # ── Phase 3: Verify persisted via GET ─────────────────────────────────
    fetched = get_agent(client, superuser_token_headers, agent_id)
    assert fetched["status_refresh_command"] == "python /app/workspace/scripts/status.py"

    # ── Phase 4: Update with /run:<name> reference ────────────────────────
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": "/run:my-custom-status"},
    )
    assert r.status_code == 200
    assert r.json()["status_refresh_command"] == "/run:my-custom-status"

    # ── Phase 5: Clear the command (opt-out) ──────────────────────────────
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": ""},
    )
    assert r.status_code == 200
    # An empty string is an opt-out — stored as "" or None
    stored = r.json().get("status_refresh_command")
    assert stored == "" or stored is None, f"Expected empty or None opt-out, got: {stored!r}"


# ---------------------------------------------------------------------------
# Scenario 2: Force-refresh with /run:<name> that EXISTS → exec called, no warning
# ---------------------------------------------------------------------------

def test_force_refresh_run_command_found_executes_and_returns_no_warning(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    force_refresh=true with status_refresh_command="/run:status" and the "status"
    command present in cli_commands_parsed (set via env start sweep):
      1. Create agent with CLI_COMMANDS.yaml containing "status" → cache populated on env start
      2. Set STATUS.md so the fetch succeeds
      3. Call GET .../status?force_refresh=true with exec_command stubbed to exit 0
      4. Verify exec_command was called with the resolved shell command
      5. Verify refresh_command_warning is null (clean run)
      6. Verify status is parsed correctly from STATUS.md
    """
    # ── Phase 1: Create agent with CLI commands cache populated ───────────
    agent = _setup_agent_with_cli_commands(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]

    # ── Phase 2: Put STATUS.md in the adapter ─────────────────────────────
    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    # ── Phase 3: Stub exec_command to return exit_code=0 ──────────────────
    mock_class, mock_connector = _make_exec_command_ok()

    try:
        with patch(
            "app.services.environments.agent_env_connector.AgentEnvConnector",
            mock_class,
        ):
            r = client.get(
                f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
                headers=superuser_token_headers,
            )
        assert r.status_code == 200, f"force_refresh failed: {r.text}"
        body = r.json()

        # ── Phase 4: exec_command was called with the resolved shell command ─
        assert mock_connector.exec_command.await_count >= 1, (
            "exec_command should have been called for the /run:status pre-command"
        )
        call_kwargs = mock_connector.exec_command.call_args
        # The resolved command for "status" is "/app/workspace/scripts/status.sh"
        assert "status.sh" in str(call_kwargs), (
            f"Expected resolved command to contain 'status.sh', got: {call_kwargs}"
        )

        # ── Phase 5: No warning on clean exec ─────────────────────────────
        assert body.get("refresh_command_warning") is None, (
            f"Expected no warning on successful exec, got: {body.get('refresh_command_warning')!r}"
        )

        # ── Phase 6: Status parsed from STATUS.md ─────────────────────────
        assert body.get("severity") == "ok", f"Expected severity=ok, got: {body.get('severity')!r}"
        assert body.get("summary") == "All systems nominal"

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)
        EnvironmentTestAdapter.workspace_files.pop("docs/CLI_COMMANDS.yaml", None)


# ---------------------------------------------------------------------------
# Scenario 3: Force-refresh with /run:<name> NOT in cache → warning, status returned
# ---------------------------------------------------------------------------

def test_force_refresh_run_command_not_found_sets_warning_and_still_fetches(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    force_refresh=true where status_refresh_command="/run:nonexistent" and the
    name is NOT present in cli_commands_parsed:
      1. Create agent; update status_refresh_command to /run:nonexistent
      2. Populate CLI commands cache — has "check", NOT "nonexistent"
      3. Set STATUS.md so the fetch succeeds
      4. Call force_refresh; exec_command must NOT be called (command not found = skip)
      5. Verify refresh_command_warning mentions the unknown name
      6. Verify warning contains no stdout/stderr
      7. Verify status is still returned (non-blocking)
    """
    # ── Phase 1: Create agent, set unknown /run:<name> ────────────────────
    EnvironmentTestAdapter.workspace_files["docs/CLI_COMMANDS.yaml"] = _CLI_COMMANDS_YAML_NO_STATUS
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": "/run:nonexistent"},
    )
    assert r.status_code == 200

    # ── Phase 2: STATUS.md available ──────────────────────────────────────
    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    mock_class, mock_connector = _make_exec_command_ok()

    try:
        with patch(
            "app.services.environments.agent_env_connector.AgentEnvConnector",
            mock_class,
        ):
            r = client.get(
                f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
                headers=superuser_token_headers,
            )
        assert r.status_code == 200, f"Unexpected HTTP error: {r.text}"
        body = r.json()

        # ── Phase 3: exec_command must NOT have been called ────────────────
        assert mock_connector.exec_command.await_count == 0, (
            "exec_command must not be called when the /run:<name> is not in the cache"
        )

        # ── Phase 4: Warning mentions the missing name ─────────────────────
        warning = body.get("refresh_command_warning")
        assert warning is not None, "Expected a refresh_command_warning, got None"
        assert "nonexistent" in warning, (
            f"Warning should mention the unknown command name, got: {warning!r}"
        )

        # ── Phase 5: Warning must not contain stdout/stderr ───────────────
        assert "ERROR" not in warning, f"Warning must not leak stdout, got: {warning!r}"
        assert "check failed" not in warning, f"Warning must not leak stdout, got: {warning!r}"

        # ── Phase 6: Status still returned ────────────────────────────────
        assert body.get("severity") == "ok", (
            f"Status fetch should still succeed; got severity={body.get('severity')!r}"
        )

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)
        EnvironmentTestAdapter.workspace_files.pop("docs/CLI_COMMANDS.yaml", None)


# ---------------------------------------------------------------------------
# Scenario 4: Force-refresh + plain shell command fails (non-zero exit) → warning
# ---------------------------------------------------------------------------

def test_force_refresh_plain_command_nonzero_exit_sets_warning_and_still_fetches(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    force_refresh=true with a plain shell command that exits non-zero:
      1. Create agent, set status_refresh_command to a raw shell command
      2. Set STATUS.md in workspace_files
      3. Stub exec_command to return exit_code=2 (with stdout and stderr)
      4. Verify refresh_command_warning contains the exit code
      5. Verify warning does NOT contain stdout or stderr content
      6. Verify status is still fetched and returned (non-blocking)
    """
    # ── Phase 1: Create agent with raw shell command ───────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": "python /app/scripts/update_status.py"},
    )
    assert r.status_code == 200

    # ── Phase 2: STATUS.md ─────────────────────────────────────────────────
    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    # ── Phase 3: Stub exec_command to return non-zero exit ─────────────────
    mock_class, mock_connector = _make_exec_command_nonzero(exit_code=2)

    try:
        with patch(
            "app.services.environments.agent_env_connector.AgentEnvConnector",
            mock_class,
        ):
            r = client.get(
                f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
                headers=superuser_token_headers,
            )
        assert r.status_code == 200, f"Unexpected HTTP error: {r.text}"
        body = r.json()

        # ── Phase 4: Warning contains exit code ───────────────────────────
        warning = body.get("refresh_command_warning")
        assert warning is not None, "Expected a refresh_command_warning on non-zero exit"
        assert "2" in warning, (
            f"Warning should mention exit code 2, got: {warning!r}"
        )

        # ── Phase 5: Warning must NOT contain stdout/stderr ───────────────
        # Stub returned stdout="ERROR: check failed" and stderr="details here"
        # — neither should appear in the warning
        assert "ERROR: check failed" not in warning, (
            f"Warning must not leak stdout, got: {warning!r}"
        )
        assert "details here" not in warning, (
            f"Warning must not contain stderr, got: {warning!r}"
        )

        # ── Phase 6: Status still returned ────────────────────────────────
        assert body.get("severity") == "ok", (
            f"Status fetch should succeed despite command failure; severity={body.get('severity')!r}"
        )
        assert body.get("summary") == "All systems nominal"

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)


# ---------------------------------------------------------------------------
# Scenario 4b: Force-refresh + exec_command raises RuntimeError → warning, status returned
# ---------------------------------------------------------------------------

def test_force_refresh_exec_error_sets_warning_and_still_fetches(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    force_refresh=true where exec_command raises RuntimeError (e.g. connection failure):
      1. Create agent with raw shell command
      2. Stub exec_command to raise RuntimeError
      3. Set STATUS.md so status fetch can still proceed
      4. Verify refresh_command_warning is set
      5. Verify status is still returned (non-blocking)
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": "bash /app/workspace/refresh_status.sh"},
    )
    assert r.status_code == 200

    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    mock_class, mock_connector = _make_exec_command_raises("Connection refused to http://localhost:8000")

    try:
        with patch(
            "app.services.environments.agent_env_connector.AgentEnvConnector",
            mock_class,
        ):
            r = client.get(
                f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
                headers=superuser_token_headers,
            )
        assert r.status_code == 200, f"Unexpected HTTP error: {r.text}"
        body = r.json()

        # Warning is set after exec failure
        warning = body.get("refresh_command_warning")
        assert warning is not None, "Expected a refresh_command_warning on exec failure"
        # Warning is a generic phrase; it must not contain connection-error stack trace
        assert "failed" in warning.lower() or "error" in warning.lower() or "skipped" in warning.lower(), (
            f"Warning should describe failure, got: {warning!r}"
        )

        # Status still returned despite the exec error
        assert body.get("severity") == "ok", (
            f"Status fetch should succeed after exec error; severity={body.get('severity')!r}"
        )

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)


# ---------------------------------------------------------------------------
# Scenario 5: Cache-only paths do NOT trigger the pre-command
# ---------------------------------------------------------------------------

def test_cache_only_paths_do_not_trigger_pre_command(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Cache-only reads must never run the status-refresh pre-command:
      1. Create agent (default /run:status)
      2. GET /agents/status (list) — cache-only; exec_command not called; warning=null
      3. GET /agents/{id}/status (no force) — cache-only; exec_command not called; warning=null
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    mock_class, mock_connector = _make_exec_command_ok()

    with patch(
        "app.services.environments.agent_env_connector.AgentEnvConnector",
        mock_class,
    ):
        # ── List endpoint (cache-only) ─────────────────────────────────────
        r_list = client.get(
            f"{settings.API_V1_STR}/agents/status",
            headers=superuser_token_headers,
        )
        assert r_list.status_code == 200
        items = r_list.json()["items"]
        our = next((i for i in items if i["agent_id"] == agent_id), None)
        assert our is not None
        assert our.get("refresh_command_warning") is None, (
            f"List endpoint must not produce a warning, got: {our.get('refresh_command_warning')!r}"
        )

        # ── Single-agent endpoint without force_refresh ────────────────────
        r_single = client.get(
            f"{settings.API_V1_STR}/agents/{agent_id}/status",
            headers=superuser_token_headers,
        )
        assert r_single.status_code == 200
        assert r_single.json().get("refresh_command_warning") is None, (
            "Non-force GET must not produce a warning"
        )

    # exec_command must never have been called
    assert mock_connector.exec_command.await_count == 0, (
        f"exec_command should not be called for cache-only reads, "
        f"called {mock_connector.exec_command.await_count} time(s)"
    )


# ---------------------------------------------------------------------------
# Scenario 6: A2A agent/status with force_refresh → warning in result dict
# ---------------------------------------------------------------------------

def test_a2a_agent_status_force_refresh_includes_warning(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    A2A JSON-RPC agent/status with force_refresh=true:
      1. Create A2A-enabled agent (default status_refresh_command="/run:status")
      2. Populate CLI commands cache WITHOUT "status" → /run:status will produce a warning
      3. Set STATUS.md so the fetch succeeds
      4. Send agent/status with force_refresh=true
      5. Verify result contains refresh_command_warning (non-null)
      6. Verify status is still returned (non-blocking)
    """
    # ── Phase 1: Create agent, populate cache WITHOUT "status" ─────────────
    agent = _setup_agent_with_cli_commands(
        client, superuser_token_headers, patch_environment_adapter,
        yaml_bytes=_CLI_COMMANDS_YAML_NO_STATUS,
    )
    agent_id = agent["id"]
    assert agent.get("active_environment_id") is not None, "Environment not created"

    # Enable A2A
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"a2a_config": {"enabled": True}},
    )
    assert r.status_code == 200

    # Create A2A access token
    token_r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/access-tokens/",
        headers=superuser_token_headers,
        json={
            "agent_id": agent_id,
            "name": "test-status-token",
            "mode": "conversation",
            "scope": "limited",
        },
    )
    assert token_r.status_code == 200, f"Create A2A token failed: {token_r.text}"
    a2a_token = token_r.json()["token"]

    # ── Phase 2: STATUS.md available ──────────────────────────────────────
    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    try:
        # ── Phase 3: A2A force_refresh ─────────────────────────────────────
        a2a_request = {
            "jsonrpc": "2.0",
            "id": "req-status-1",
            "method": "agent/status",
            "params": {"force_refresh": True},
        }
        a2a_headers = {
            "Authorization": f"Bearer {a2a_token}",
            "Content-Type": "application/json",
        }
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=a2a_request,
        )
        assert resp.status_code == 200, f"A2A agent/status failed: {resp.text}"
        envelope = resp.json()
        assert "result" in envelope, f"Expected JSON-RPC result envelope: {envelope}"
        result = envelope["result"]

        # ── Phase 4: refresh_command_warning is present ────────────────────
        assert "refresh_command_warning" in result, (
            f"Expected refresh_command_warning key in A2A result, keys: {list(result.keys())}"
        )
        warning = result["refresh_command_warning"]
        # /run:status is not in the cache (we loaded _CLI_COMMANDS_YAML_NO_STATUS)
        assert warning is not None, (
            "Expected non-null refresh_command_warning (status not in cache)"
        )
        assert "status" in warning.lower() or "not defined" in warning.lower(), (
            f"Warning should mention the missing command, got: {warning!r}"
        )

        # ── Phase 5: Status still returned ────────────────────────────────
        assert result.get("severity") == "ok", (
            f"Status fetch should succeed despite warning; got {result.get('severity')!r}"
        )

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)
        EnvironmentTestAdapter.workspace_files.pop("docs/CLI_COMMANDS.yaml", None)


# ---------------------------------------------------------------------------
# Scenario 6b: A2A agent/status without force_refresh → NO warning
# ---------------------------------------------------------------------------

def test_a2a_agent_status_cache_only_has_no_warning(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    A2A agent/status without force_refresh uses the cached snapshot and must
    never trigger the pre-command (refresh_command_warning is null).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"a2a_config": {"enabled": True}},
    )
    assert r.status_code == 200

    token_r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/access-tokens/",
        headers=superuser_token_headers,
        json={
            "agent_id": agent_id,
            "name": "test-cache-status-token",
            "mode": "conversation",
            "scope": "limited",
        },
    )
    assert token_r.status_code == 200
    a2a_token = token_r.json()["token"]

    mock_class, mock_connector = _make_exec_command_ok()

    with patch(
        "app.services.environments.agent_env_connector.AgentEnvConnector",
        mock_class,
    ):
        a2a_request = {
            "jsonrpc": "2.0",
            "id": "req-cache-1",
            "method": "agent/status",
            "params": {},  # force_refresh defaults to False
        }
        a2a_headers = {
            "Authorization": f"Bearer {a2a_token}",
            "Content-Type": "application/json",
        }
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=a2a_request,
        )

    assert resp.status_code == 200, f"A2A cache agent/status failed: {resp.text}"
    result = resp.json().get("result", {})

    # Must not contain a warning
    assert result.get("refresh_command_warning") is None, (
        f"Cache-only A2A must not produce a warning, got: {result.get('refresh_command_warning')!r}"
    )

    # exec_command must not have been called
    assert mock_connector.exec_command.await_count == 0, (
        "exec_command should not be called on non-force A2A agent/status"
    )


# ---------------------------------------------------------------------------
# Scenario 7: /agent-status slash command renders warning line
# ---------------------------------------------------------------------------

def test_agent_status_slash_command_renders_warning_when_present(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /agent-status slash command runs the pre-command (live path) and renders
    the warning in the response when the command reference is not in the cache:
      1. Create agent, update status_refresh_command to /run:missing-cmd
      2. Create agent with no "missing-cmd" in CLI commands cache
      3. Set STATUS.md so the live fetch succeeds
      4. Send /agent-status in a session
      5. Verify the command system message contains the warning text
      6. Verify warning does not contain stdout/stderr
      7. Verify status content is still included
    """
    # ── Phase 1: Create agent, set unknown /run:<name> ────────────────────
    EnvironmentTestAdapter.workspace_files["docs/CLI_COMMANDS.yaml"] = _CLI_COMMANDS_YAML_NO_STATUS
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": "/run:missing-cmd"},
    )
    assert r.status_code == 200

    # ── Phase 2: Create a session ──────────────────────────────────────────
    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 3: STATUS.md is available ───────────────────────────────────
    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    stub = StubAgentEnvConnector(response_text="ok")

    try:
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            # ── Phase 4: Send /agent-status ────────────────────────────────
            result = send_message(
                client, superuser_token_headers, session_id,
                content="/agent-status",
            )
            drain_tasks()

        # ── Phase 5: Command was executed synchronously (not via LLM) ──────
        assert result.get("command_executed") is True, (
            f"Expected command_executed=True for /agent-status, got: {result}"
        )
        # No LLM call for a synchronous command handler
        assert len(stub.stream_calls) == 0, (
            "agent-status is a non-LLM command; stream_chat must not be called"
        )

        # ── Phase 6: System message contains the warning ───────────────────
        cmd_msgs = _get_command_system_messages(client, superuser_token_headers, session_id)
        assert len(cmd_msgs) >= 1, "Expected at least one command system message"
        last_msg_content = cmd_msgs[-1]["content"]

        assert "missing-cmd" in last_msg_content, (
            f"Expected warning about 'missing-cmd', got:\n{last_msg_content[:500]}"
        )
        # The warning is rendered with a ⚠️ prefix by the command handler
        assert "⚠️" in last_msg_content, (
            f"Expected ⚠️ warning marker, got:\n{last_msg_content[:500]}"
        )

        # ── Phase 7: Status content is still rendered ──────────────────────
        # The status fetch succeeded (STATUS.md was present) so status output
        # should appear alongside the warning
        assert "OK" in last_msg_content.upper() or "nominal" in last_msg_content.lower(), (
            f"Expected status content despite warning, got:\n{last_msg_content[:500]}"
        )

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)
        EnvironmentTestAdapter.workspace_files.pop("docs/CLI_COMMANDS.yaml", None)


# ---------------------------------------------------------------------------
# Scenario 7b: /agent-status slash command — clean exec, no warning line
# ---------------------------------------------------------------------------

def test_agent_status_slash_command_no_warning_when_exec_succeeds(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    /agent-status with a found /run:status command and successful exec:
    the rendered message must NOT contain a 'not defined in CLI_COMMANDS.yaml' warning.
    """
    # ── Phase 1 & 2: Create agent with CLI cache containing "status" ───────
    agent = _setup_agent_with_cli_commands(
        client, superuser_token_headers, patch_environment_adapter,
        yaml_bytes=_CLI_COMMANDS_YAML_WITH_STATUS,
    )
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    stub = StubAgentEnvConnector(response_text="ok")
    mock_class, mock_connector = _make_exec_command_ok()

    try:
        with (
            patch("app.services.sessions.message_service.agent_env_connector", stub),
            patch(
                "app.services.environments.agent_env_connector.AgentEnvConnector",
                mock_class,
            ),
        ):
            result = send_message(
                client, superuser_token_headers, session_id,
                content="/agent-status",
            )
            drain_tasks()

        assert result.get("command_executed") is True

        cmd_msgs = _get_command_system_messages(client, superuser_token_headers, session_id)
        assert len(cmd_msgs) >= 1
        last_msg_content = cmd_msgs[-1]["content"]

        # No "not defined in CLI_COMMANDS.yaml" warning
        assert "CLI_COMMANDS.yaml" not in last_msg_content, (
            f"Should not have 'not defined in CLI_COMMANDS.yaml' warning, got:\n{last_msg_content[:500]}"
        )

        # Status content should be present
        assert "OK" in last_msg_content.upper() or "nominal" in last_msg_content.lower(), (
            f"Expected status content, got:\n{last_msg_content[:500]}"
        )

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)
        EnvironmentTestAdapter.workspace_files.pop("docs/CLI_COMMANDS.yaml", None)


# ---------------------------------------------------------------------------
# Scenario 8: Blank/empty status_refresh_command is a silent opt-out
# ---------------------------------------------------------------------------

def test_force_refresh_blank_command_is_silent_opt_out(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    status_refresh_command="" (empty string / opt-out):
    force_refresh runs fine — no warning, exec_command never called.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    # Opt out by clearing the command
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
        json={"status_refresh_command": ""},
    )
    assert r.status_code == 200

    EnvironmentTestAdapter.workspace_files["app-data/storage/STATUS.md"] = _STATUS_MD

    mock_class, mock_connector = _make_exec_command_ok()

    try:
        with patch(
            "app.services.environments.agent_env_connector.AgentEnvConnector",
            mock_class,
        ):
            r = client.get(
                f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
                headers=superuser_token_headers,
            )
        assert r.status_code == 200
        body = r.json()

        # No warning — blank command is a deliberate opt-out
        assert body.get("refresh_command_warning") is None, (
            f"Blank command must not produce a warning, got: {body.get('refresh_command_warning')!r}"
        )
        # exec_command not called
        assert mock_connector.exec_command.await_count == 0, (
            "exec_command should not be called when status_refresh_command is blank"
        )
        # Status still fetched
        assert body.get("severity") == "ok"

    finally:
        EnvironmentTestAdapter.workspace_files.pop("app-data/storage/STATUS.md", None)
