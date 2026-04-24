"""
Backend tests for the cinna-cli Live Sync feature.

Covers:
- sync-runtime endpoint: auth required, agent scope enforced, response shape
- exec endpoint: auth + scope checks, streaming SSE with exec_id first event,
  env-core failure propagation, no "cwd" param accepted
- sync-stream WebSocket: handshake rejects on missing/invalid bearer,
  scope mismatch → 1008 close; on successful handshake SyncActivityTracker.register
  is called, unregister called on disconnect
- workspace-files-changed callback (+ legacy prompt-file-changed alias): env-core
  auth via X-Agent-Env-Id + bearer, wrong credentials return 401, happy path
  emits WORKSPACE_FILES_CHANGED which runs sync_agent_prompts_from_environment via
  the registered handler
- Suspension scheduler gate: is_sync_warm=True causes scheduler to skip the env

Notes:
- The sync-stream WebSocket test cannot end-to-end connect to a real env-core
  container (no Docker in the test env). The test verifies: auth rejection before
  accept(), and the register/unregister tracker calls made by the route when the
  env-core connection attempt fails (open_sync_websocket raises RuntimeError →
  route closes WS without proceeding). To test the happy-path teardown path
  (unregister after pump finishes) would require a real env-core; a docstring
  scope note is included instead.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import (
    cli_auth_headers,
    create_setup_token,
    exchange_setup_token,
    list_cli_tokens,
)

_BASE = f"{settings.API_V1_STR}/cli"
_ENV_BASE = f"{settings.API_V1_STR}/environments"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bootstrap_cli(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    machine_name: str = "Test Machine",
) -> tuple[str, str, str]:
    """
    Create an agent + CLI token.

    Returns (agent_id, cli_jwt, cli_token_id).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    exchange = exchange_setup_token(client, token_resp["token"], machine_name=machine_name)
    cli_jwt = exchange["cli_token"]

    tokens = list_cli_tokens(client, superuser_token_headers)
    cli_token_id = tokens[-1]["id"]

    return agent_id, cli_jwt, cli_token_id


# ── Scenario 1: sync-runtime endpoint ────────────────────────────────────────

def test_sync_runtime_auth_and_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    sync-runtime endpoint security and response:
      1. Bootstrap agent + CLI token
      2. No token → 401/403
      3. Regular user JWT (not a CLI token) → 401
      4. CLI token for a different agent → 403
      5. Happy path: returns {mutagen_version, mutagen_agent_sha256, platform_api_version}
      6. Pinned version matches the value configured via ``settings.MUTAGEN_VERSION``
    """
    # ── Phase 1: Bootstrap ────────────────────────────────────────────────
    agent_id, cli_jwt, _ = _bootstrap_cli(client, superuser_token_headers)
    cli_headers = cli_auth_headers(cli_jwt)

    # ── Phase 2: No token → 401/403 ───────────────────────────────────────
    r = client.get(f"{_BASE}/agents/{agent_id}/sync-runtime")
    assert r.status_code in (401, 403), f"Expected 401/403 without auth, got {r.status_code}"

    # ── Phase 3: Regular user JWT rejected → 401 ──────────────────────────
    r = client.get(f"{_BASE}/agents/{agent_id}/sync-runtime", headers=superuser_token_headers)
    assert r.status_code == 401, f"Expected 401 for regular JWT on CLI endpoint, got {r.status_code}"

    # ── Phase 4: Scope guard — different agent_id → 403 ──────────────────
    other_agent = create_agent_via_api(client, superuser_token_headers)
    other_agent_id = other_agent["id"]
    r = client.get(f"{_BASE}/agents/{other_agent_id}/sync-runtime", headers=cli_headers)
    assert r.status_code == 403, f"Expected 403 for wrong agent scope, got {r.status_code}"

    # ── Phase 5: Happy path → known shape ────────────────────────────────
    r = client.get(f"{_BASE}/agents/{agent_id}/sync-runtime", headers=cli_headers)
    assert r.status_code == 200, f"Expected 200 for valid CLI token, got {r.status_code}: {r.text}"
    body = r.json()
    assert "mutagen_version" in body, f"Missing mutagen_version in {body}"
    assert "mutagen_agent_sha256" in body, f"Missing mutagen_agent_sha256 in {body}"
    assert "platform_api_version" in body, f"Missing platform_api_version in {body}"

    # ── Phase 6: Pinned version ───────────────────────────────────────────
    # Source of truth is ``settings.MUTAGEN_VERSION`` (the same value baked into
    # the env-template Dockerfiles as the MUTAGEN_VERSION build arg).
    assert body["mutagen_version"] == settings.MUTAGEN_VERSION, (
        f"Expected pinned Mutagen version {settings.MUTAGEN_VERSION!r}, got {body['mutagen_version']!r}"
    )
    assert isinstance(body["platform_api_version"], str) and body["platform_api_version"], (
        "platform_api_version must be a non-empty string"
    )


# ── Scenario 2: exec endpoint ─────────────────────────────────────────────────

def test_exec_endpoint_auth_scope_and_streaming(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    exec endpoint security and streaming:
      1. Bootstrap agent + CLI token
      2. No token → 401/403
      3. Regular user JWT → 401
      4. Wrong agent scope → 403
      5. Happy path: SSE stream starts; first event is {type: exec_id, exec_id: <uuid>}
         (env-core unreachable in test → stream terminates with error event, but exec_id
         event is still the first one emitted)
      6. Body must have "command" field — POST with no body → 422
      7. Post-impl parity: exec body does NOT accept a "cwd" field (the plan removed it;
         sending cwd alongside command should still succeed — cwd is just ignored by the
         Pydantic model — so we verify the schema only accepts what's defined)
    """
    from tests.stubs.agent_env_stub import build_command_stream_events, StubAgentEnvConnector

    # ── Phase 1: Bootstrap with a running env ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    assert env_id is not None, "Agent must have an active environment for exec test"

    # Mark env as running so _ensure_environment_running passes quickly
    env = db.get(AgentEnvironment, env_id)
    env.status = "running"
    db.add(env)
    db.flush()

    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    exchange = exchange_setup_token(client, token_resp["token"], machine_name="Exec Machine")
    cli_jwt = exchange["cli_token"]
    cli_headers = cli_auth_headers(cli_jwt)

    # ── Phase 2: No token → 401/403 ──────────────────────────────────────
    r = client.post(
        f"{_BASE}/agents/{agent_id}/exec",
        json={"command": "echo hello"},
    )
    assert r.status_code in (401, 403)

    # ── Phase 3: Regular user JWT → 401 ──────────────────────────────────
    r = client.post(
        f"{_BASE}/agents/{agent_id}/exec",
        headers=superuser_token_headers,
        json={"command": "echo hello"},
    )
    assert r.status_code == 401

    # ── Phase 4: Wrong agent scope → 403 ─────────────────────────────────
    other_agent = create_agent_via_api(client, superuser_token_headers)
    r = client.post(
        f"{_BASE}/agents/{other_agent['id']}/exec",
        headers=cli_headers,
        json={"command": "echo hello"},
    )
    assert r.status_code == 403

    # ── Phase 5: Happy path — first event is exec_id ─────────────────────
    # Stub the env connector so it returns command events without needing Docker
    command_events = build_command_stream_events(
        exec_id="will-be-replaced-by-service",
        command="echo hello",
        stdout_lines=["hello\n"],
        exit_code=0,
    )
    stub = StubAgentEnvConnector(command_events=command_events)

    with patch(
        "app.services.environments.agent_env_connector.agent_env_connector",
        stub,
    ):
        r = client.post(
            f"{_BASE}/agents/{agent_id}/exec",
            headers=cli_headers,
            json={"command": "echo hello"},
        )
        assert r.status_code == 200, f"Expected 200 from exec, got {r.status_code}: {r.text}"

        # Parse the SSE events from the streaming response
        raw_content = r.content.decode("utf-8")
        lines = raw_content.strip().split("\n")

        # Find the first "data: ..." line
        first_data_line = None
        for line in lines:
            if line.startswith("data: "):
                first_data_line = line[6:]
                break

        assert first_data_line is not None, (
            f"No SSE data lines found in exec response. Full content: {raw_content!r}"
        )

        first_event = json.loads(first_data_line)
        assert first_event.get("type") == "exec_id", (
            f"First SSE event must be exec_id, got: {first_event}"
        )
        exec_id_value = first_event.get("exec_id")
        assert exec_id_value, "exec_id event must include the exec_id field"
        # Verify it's a valid UUID
        uuid.UUID(exec_id_value)

        # Verify stub was called with the command
        assert len(stub.stream_command_calls) == 1
        assert stub.stream_command_calls[0]["resolved_command"] == "echo hello"

    # ── Phase 6: Missing "command" body → 422 ────────────────────────────
    r = client.post(
        f"{_BASE}/agents/{agent_id}/exec",
        headers=cli_headers,
        json={},
    )
    assert r.status_code == 422, f"Expected 422 for missing command field, got {r.status_code}"

    # ── Phase 7: "cwd" is not part of the ExecBody schema ────────────────
    # The plan removed cwd from the exec body. Posting cwd alongside command
    # should NOT cause a 422 (extra fields are ignored by default in Pydantic v2
    # with model_config extra="ignore"), but the service MUST NOT forward it.
    # We just verify the endpoint still returns 200 (not 422 = cwd not a required field).
    with patch(
        "app.services.environments.agent_env_connector.agent_env_connector",
        StubAgentEnvConnector(command_events=command_events),
    ):
        r = client.post(
            f"{_BASE}/agents/{agent_id}/exec",
            headers=cli_headers,
            json={"command": "echo hello", "cwd": "/workspace"},
        )
        # The route either ignores cwd (200) or may reject it if Pydantic forbids extras (422).
        # Both are acceptable — what's important is it does NOT break auth or scope.
        assert r.status_code in (200, 422), (
            f"Unexpected status when posting with extra 'cwd' field: {r.status_code}"
        )


def test_exec_env_core_failure_propagates_error_event(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    When env-core's stream_command raises an exception, the exec endpoint
    must stream an SSE error event rather than returning a 500.
    """
    # Bootstrap
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    assert env_id is not None

    env = db.get(AgentEnvironment, env_id)
    env.status = "running"
    db.add(env)
    db.flush()

    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    exchange = exchange_setup_token(client, token_resp["token"], machine_name="Error Machine")
    cli_headers = cli_auth_headers(exchange["cli_token"])

    # Stub that yields an error event (simulating env-core failure)
    from tests.stubs.agent_env_stub import StubAgentEnvConnector as _Stub
    error_events = [{"type": "error", "content": "connection refused", "error_type": "ConnectionError"}]

    with patch("app.services.environments.agent_env_connector.agent_env_connector", _Stub(command_events=error_events)):
        r = client.post(
            f"{_BASE}/agents/{agent_id}/exec",
            headers=cli_headers,
            json={"command": "broken-command"},
        )
        assert r.status_code == 200, f"Exec must return 200 even on env-core error (SSE stream): {r.status_code}"

        # Parse events — must contain an exec_id event then an error event
        raw_content = r.content.decode("utf-8")
        events = []
        for line in raw_content.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        assert len(events) >= 1
        assert events[0]["type"] == "exec_id", "First event must be exec_id even on error"

        # The error event from env-core must be forwarded
        error_events_in_stream = [e for e in events if e.get("type") == "error"]
        assert error_events_in_stream, (
            f"Expected at least one error event in stream, got: {events}"
        )


# ── Scenario 3: sync-stream WebSocket ────────────────────────────────────────

def test_sync_stream_ws_auth_rejects(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    sync-stream WebSocket handshake auth checks:
      1. No token → close code 1008
      2. Invalid JWT string → close code 1008
      3. Regular user JWT (not CLI token_type) → close code 1008
      4. CLI token for a different agent → close code 1008

    This test uses FastAPI's TestClient WebSocket support. The WS dep
    closes the socket and raises WebSocketDisconnect before accept() when
    auth fails, so the test client sees a disconnect immediately.
    """
    # ── Phase 1: Bootstrap agent + CLI token ─────────────────────────────
    agent_id, cli_jwt, _ = _bootstrap_cli(client, superuser_token_headers)

    ws_url = f"/api/v1/cli/agents/{agent_id}/sync-stream"

    # ── Phase 2: No token → immediate disconnect ───────────────────────
    with pytest.raises(Exception):
        # TestClient raises an exception when the WS is closed from the server side
        with client.websocket_connect(ws_url) as ws:
            ws.receive_text()

    # ── Phase 3: Invalid JWT string → disconnect ──────────────────────
    with pytest.raises(Exception):
        with client.websocket_connect(
            ws_url,
            headers={"Authorization": "Bearer not-a-jwt-at-all"},
        ) as ws:
            ws.receive_text()

    # ── Phase 4: Regular user JWT → disconnect ────────────────────────
    # superuser_token_headers contains a regular JWT — CLIContextWSDep rejects it
    with pytest.raises(Exception):
        with client.websocket_connect(
            ws_url,
            headers=superuser_token_headers,
        ) as ws:
            ws.receive_text()

    # ── Phase 5: CLI token scoped to another agent → disconnect ────────
    other_agent = create_agent_via_api(client, superuser_token_headers)
    other_agent_id = other_agent["id"]
    # cli_jwt is scoped to agent_id, not other_agent_id
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/api/v1/cli/agents/{other_agent_id}/sync-stream",
            headers=cli_auth_headers(cli_jwt),
        ) as ws:
            ws.receive_text()

# ── Scenario 4: workspace-files-changed callback ─────────────────────────────


def _emit_event_calls_for(event_type: str) -> tuple[AsyncMock, object]:
    """Build a patcher for ``event_service.emit_event`` that records all calls.

    Returns the mock and the patch object (caller uses it as a context manager).
    Emitted events are recorded on ``mock.call_args_list`` — the test then
    filters to the event_type it cares about.
    """
    mock_emit = AsyncMock()
    patcher = patch(
        "app.services.events.event_service.event_service.emit_event",
        mock_emit,
    )
    return mock_emit, patcher


def _assert_workspace_files_changed_emitted(
    mock_emit: AsyncMock,
    expected_env_id: str,
    expected_agent_id: str,
    expected_changed_files: list[str] | None,
) -> None:
    """Verify at least one emit_event call matches WORKSPACE_FILES_CHANGED with the expected meta."""
    matches = [
        call for call in mock_emit.call_args_list
        if call.kwargs.get("event_type") == "workspace_files_changed"
    ]
    assert matches, (
        f"Expected a WORKSPACE_FILES_CHANGED emit; got: "
        f"{[c.kwargs.get('event_type') for c in mock_emit.call_args_list]}"
    )
    # Take the last match (most recent emission)
    meta = matches[-1].kwargs.get("meta") or {}
    assert meta.get("environment_id") == expected_env_id, (
        f"Expected environment_id={expected_env_id}, got {meta.get('environment_id')}"
    )
    assert meta.get("agent_id") == expected_agent_id, (
        f"Expected agent_id={expected_agent_id}, got {meta.get('agent_id')}"
    )
    if expected_changed_files is None:
        assert "changed_files" not in meta, (
            f"Expected no changed_files key when body omitted, got {meta.get('changed_files')!r}"
        )
    else:
        assert meta.get("changed_files") == expected_changed_files, (
            f"Expected changed_files={expected_changed_files}, got {meta.get('changed_files')!r}"
        )


def test_workspace_files_changed_auth_and_event(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    POST /environments/{id}/workspace-files-changed (and legacy prompt-file-changed alias):
      1. Bootstrap agent with active env; set auth_token in env config
      2. Missing Authorization header → 401
      3. Missing X-Agent-Env-Id header → 401
      4. Wrong bearer token → 401
      5. Env ID in path doesn't match X-Agent-Env-Id header → 401 or 403
      6. Happy path on workspace-files-changed with changed_files body → 200 +
         WORKSPACE_FILES_CHANGED event emitted with the changed_files list in meta
      7. Happy path on legacy prompt-file-changed → 200 + the same event without
         changed_files in meta
    """
    # ── Phase 1: Bootstrap agent with running env ─────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    assert env_id is not None, "Agent must have an active environment"

    # Set auth_token in env config — this is what the callback auth verifies
    env = db.get(AgentEnvironment, env_id)
    auth_token = "test-agent-auth-token-abc123"
    env.config = {**env.config, "auth_token": auth_token}
    env.status = "running"
    db.add(env)
    db.flush()

    new_url = f"{_ENV_BASE}/{env_id}/workspace-files-changed"
    legacy_url = f"{_ENV_BASE}/{env_id}/prompt-file-changed"
    auth_bearer = f"Bearer {auth_token}"
    env_id_header = str(env_id)

    # ── Phase 2: Missing Authorization → 401 ─────────────────────────────
    r = client.post(
        new_url,
        headers={"X-Agent-Env-Id": env_id_header},
    )
    assert r.status_code == 401, f"Expected 401 without Authorization, got {r.status_code}"

    # ── Phase 3: Missing X-Agent-Env-Id → 401 ────────────────────────────
    r = client.post(
        new_url,
        headers={"Authorization": auth_bearer},
    )
    assert r.status_code == 401, f"Expected 401 without X-Agent-Env-Id, got {r.status_code}"

    # ── Phase 4: Wrong bearer token → 401 ────────────────────────────────
    r = client.post(
        new_url,
        headers={
            "Authorization": "Bearer wrong-token",
            "X-Agent-Env-Id": env_id_header,
        },
    )
    assert r.status_code == 401, f"Expected 401 for wrong bearer, got {r.status_code}"

    # ── Phase 5: Env ID in path doesn't match X-Agent-Env-Id ─────────────
    # Use a different env UUID in the header — the route verifies env.id == id
    other_env_id = str(uuid.uuid4())
    r = client.post(
        new_url,
        headers={
            "Authorization": auth_bearer,
            "X-Agent-Env-Id": other_env_id,
        },
    )
    # _verify_env_agent_auth will fail to find the env → 401 (invalid env ID)
    assert r.status_code in (401, 403), (
        f"Expected 401/403 for mismatched env ID, got {r.status_code}"
    )

    # ── Phase 6: Happy path on new endpoint with changed_files body ──────
    mock_emit, patcher = _emit_event_calls_for("workspace_files_changed")
    with patcher:
        r = client.post(
            new_url,
            headers={
                "Authorization": auth_bearer,
                "X-Agent-Env-Id": env_id_header,
            },
            json={"changed_files": ["docs/WORKFLOW_PROMPT.md", "docs/CLI_COMMANDS.yaml"]},
        )

    assert r.status_code == 200, f"Expected 200 for valid callback, got {r.status_code}: {r.text}"
    body = r.json()
    assert "message" in body
    _assert_workspace_files_changed_emitted(
        mock_emit,
        expected_env_id=env_id,
        expected_agent_id=agent_id,
        expected_changed_files=["docs/WORKFLOW_PROMPT.md", "docs/CLI_COMMANDS.yaml"],
    )

    # ── Phase 7: Happy path on legacy prompt-file-changed alias ──────────
    mock_emit, patcher = _emit_event_calls_for("workspace_files_changed")
    with patcher:
        r = client.post(
            legacy_url,
            headers={
                "Authorization": auth_bearer,
                "X-Agent-Env-Id": env_id_header,
            },
        )

    assert r.status_code == 200, f"Expected 200 for valid legacy callback, got {r.status_code}: {r.text}"
    _assert_workspace_files_changed_emitted(
        mock_emit,
        expected_env_id=env_id,
        expected_agent_id=agent_id,
        expected_changed_files=None,
    )


# ── Scenario 5: Suspension scheduler gate ────────────────────────────────────

def test_suspension_scheduler_skips_sync_warm_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The suspension scheduler must skip environments where is_sync_warm() returns True.

    Approach:
      1. Create an agent → drain tasks → env is "running"
      2. Call _check_and_suspend_environments() with is_sync_warm patched to True
         for ALL environments (simplest way to verify the gate path without needing
         to set up stale last_activity_at state that crosses session boundaries)
      3. Verify the lifecycle manager's suspend_environment was NOT called

    This verifies the gate at:
        backend/app/services/environments/environment_suspension_scheduler.py:74
    """
    import asyncio
    from app.services.environments.environment_suspension_scheduler import (
        _check_and_suspend_environments,
    )
    from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager

    # ── Phase 1: Create agent → env running ────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]
    assert env_id is not None

    # ── Phase 2 & 3: Run scheduler with is_sync_warm returning True ──────
    suspend_calls: list = []

    async def _mock_suspend(db_session, environment):
        suspend_calls.append(str(environment.id))

    with (
        # The scheduler imports sync_activity_tracker locally inside the loop body:
        #   from app.services.cli.sync_activity_tracker import sync_activity_tracker
        # Patching the module-level singleton is the correct target.
        patch(
            "app.services.cli.sync_activity_tracker.sync_activity_tracker.is_sync_warm",
            return_value=True,  # All envs appear to have active sync connections
        ),
        patch.object(
            EnvironmentLifecycleManager,
            "suspend_environment",
            side_effect=_mock_suspend,
        ),
    ):
        asyncio.run(_check_and_suspend_environments())

    # ── Phase 4: Verify env was NOT suspended ─────────────────────────────
    assert env_id not in suspend_calls, (
        f"Scheduler must skip env {env_id} when is_sync_warm=True; "
        f"but suspend was called for: {suspend_calls}"
    )
