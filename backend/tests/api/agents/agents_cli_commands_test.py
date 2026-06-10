"""
Integration tests: CLI Commands Sync and Discovery feature.

Tests:
  1. GET /sessions/{id}/commands returns no dynamic entries when cli_commands_parsed is null
  2. GET /sessions/{id}/commands returns dynamic /run:<name> entries when cache is populated
  3. Dynamic entries have resolved_command populated; static entries have it null
  4. Dynamic entries are is_available=False when environment is not "running"
  5. Dynamic entries are is_available=True when environment is "running"
  6. CLICommandsService.parse_commands_file — canonical scenario via API: fetch + verify endpoint

Notes:
  - These tests use the environment adapter stub (auto-patched by agents/conftest.py).
  - The parse_commands_file unit tests are in tests/unit/test_cli_commands_service.py.
  - This file covers the API-observable behaviors.
"""
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import create_session_via_api


@pytest.fixture
def set_workspace_file() -> Iterator[Callable[[str, bytes], None]]:
    """Set files on the shared ``EnvironmentTestAdapter.workspace_files`` dict and
    restore the original contents afterwards.

    ``workspace_files`` is a class-level mutable dict shared across all tests, so
    mutating it directly leaks state into later tests. This fixture snapshots and
    restores it so the cache-populating files are isolated per test.
    """
    original = dict(EnvironmentTestAdapter.workspace_files)

    def _set(path: str, content: bytes) -> None:
        EnvironmentTestAdapter.workspace_files[path] = content

    try:
        yield _set
    finally:
        EnvironmentTestAdapter.workspace_files.clear()
        EnvironmentTestAdapter.workspace_files.update(original)


def _list_session_commands(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
) -> tuple[int, dict]:
    """Call GET /api/v1/sessions/{session_id}/commands. Returns (status_code, json)."""
    r = client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=token_headers,
    )
    return r.status_code, r.json()


def test_commands_no_dynamic_entries_when_cache_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When cli_commands_parsed is null (no cache), the endpoint returns only static
    commands. No /run:<name> entries appear.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    status, data = _list_session_commands(client, superuser_token_headers, session_id)

    assert status == 200
    commands = data["commands"]
    dynamic_names = [cmd["name"] for cmd in commands if cmd["name"].startswith("/run:")]
    assert dynamic_names == [], f"Expected no dynamic /run: commands, got: {dynamic_names}"


def test_commands_includes_dynamic_entries_when_cache_populated(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    set_workspace_file: Callable[[str, bytes], None],
) -> None:
    """
    When the environment has cli_commands_parsed populated, dynamic /run:<name>
    entries appear in the commands list.

    Scenario:
      1. Seed docs/CLI_COMMANDS.yaml on the stub adapter
      2. Create agent → the ENVIRONMENT_ACTIVATED handler fetches + parses the
         file and caches it on the environment (real fetch→cache flow)
      3. Fetch commands → verify dynamic entries are present
      4. Verify resolved_command is populated on dynamic entries
      5. Verify static entries have resolved_command=None
    """
    # ── Phase 1: Seed the CLI_COMMANDS.yaml the adapter will serve ─────────
    set_workspace_file(
        "docs/CLI_COMMANDS.yaml",
        b"commands:\n"
        b"  - name: check\n"
        b"    command: uv run /app/workspace/scripts/check.py\n"
        b"    description: Monthly data quality check\n"
        b"  - name: report\n"
        b"    command: uv run /app/workspace/scripts/report.py\n",
    )

    # ── Phase 2: Create agent + session; the ENVIRONMENT_ACTIVATED handler
    # fetches and caches CLI_COMMANDS.yaml during drain_tasks (real flow). ──
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]
    drain_tasks()

    # ── Phase 3: Fetch commands and verify dynamic entries ─────────────────
    status, data = _list_session_commands(client, superuser_token_headers, session_id)
    assert status == 200

    commands_by_name = {cmd["name"]: cmd for cmd in data["commands"]}

    assert "/run:check" in commands_by_name, (
        f"Expected /run:check in commands. Got: {list(commands_by_name.keys())}"
    )
    assert "/run:report" in commands_by_name

    # ── Phase 4: resolved_command is set on dynamic entries ────────────────
    check_cmd = commands_by_name["/run:check"]
    assert check_cmd["resolved_command"] == "uv run /app/workspace/scripts/check.py"
    assert check_cmd["description"] == "Monthly data quality check"

    report_cmd = commands_by_name["/run:report"]
    assert report_cmd["resolved_command"] == "uv run /app/workspace/scripts/report.py"
    # No description → falls back to truncated command (first 80 chars)
    assert report_cmd["description"] is not None

    # ── Phase 5: Static entries have resolved_command=None ─────────────────
    static_cmd = commands_by_name.get("/files")
    if static_cmd:
        assert static_cmd.get("resolved_command") is None


def test_commands_static_entries_have_null_resolved_command(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Static slash commands (/files, /rebuild-env, etc.) always have
    resolved_command=None in the response.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    status, data = _list_session_commands(client, superuser_token_headers, session_id)

    assert status == 200
    for cmd in data["commands"]:
        if not cmd["name"].startswith("/run:"):
            assert cmd.get("resolved_command") is None, (
                f"Static command {cmd['name']} should have resolved_command=None, "
                f"got: {cmd.get('resolved_command')}"
            )


def test_commands_dynamic_entries_always_available(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    set_workspace_file: Callable[[str, bytes], None],
) -> None:
    """
    Dynamic /run:<name> entries are always is_available=True regardless of the
    environment status. When the user invokes one, the execution path (plan #2)
    activates a stopped environment the same way a regular message does.

    Drives the real fetch→cache→endpoint flow via the stub adapter (no patching
    of the service-under-test).
    """
    set_workspace_file(
        "docs/CLI_COMMANDS.yaml",
        b"commands:\n"
        b"  - name: check\n"
        b"    command: uv run /app/workspace/check.py\n"
        b"    description: Check script\n",
    )

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]
    drain_tasks()

    status, data = _list_session_commands(client, superuser_token_headers, session_id)

    assert status == 200
    dynamic_cmds = [c for c in data["commands"] if c["name"].startswith("/run:")]
    assert dynamic_cmds, "Expected at least one dynamic /run:* command from the cache"
    for cmd in dynamic_cmds:
        assert cmd["is_available"] is True, (
            f"Dynamic command {cmd['name']} should always be available; "
            f"execution-time activation is plan #2's responsibility"
        )
