"""
Integration tests for POST /api/v1/agents/create-flow — env-config field propagation.

Covers the six new optional fields added to AgentCreateFlowRequest in Phase 5:
  - env_name
  - model_override_conversation
  - model_override_building
  - use_default_ai_credentials
  - conversation_ai_credential_id
  - building_ai_credential_id

Design note — SSE streaming + polling problem
─────────────────────────────────────────────
`create_agent_flow` is an async generator that streams SSE events and polls
`environment.status` in a loop with `asyncio.sleep(2)`.  In the normal test
setup the environment creation background task is captured but not drained
until after the response completes — creating a deadlock where the polling
loop never sees status="running".

To break this deadlock we patch `EnvironmentService.create_environment` with
`_StubCreateEnvironment`, which:
  1. Creates a real `AgentEnvironment` DB row with ``status="running"`` immediately
     (so the polling loop terminates after its first refresh).
  2. Records the ``AgentEnvironmentCreate`` data it received for per-test assertions.

Because the stub writes to the DB (via the session passed to it) and we're
inside the test transaction, the environment row is visible for subsequent
`GET /agents/{id}/environments` calls and is rolled back after the test.

After the streaming response returns we call drain_tasks() to flush any
residual background tasks (e.g., event handlers).
"""

import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment, AgentEnvironmentCreate
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks


API = settings.API_V1_STR


# ── Stub for EnvironmentService.create_environment ───────────────────────────


class _StubCreateEnvironment:
    """Captures AgentEnvironmentCreate and creates a ready environment row.

    Replaces ``EnvironmentService.create_environment`` per-test so the
    streaming polling loop sees status="running" immediately, avoiding the
    asyncio.sleep deadlock inherent to the streaming SSE endpoint.

    Attributes:
        captured_data: The ``AgentEnvironmentCreate`` objects passed on each call.
    """

    def __init__(self):
        self.captured_data: list[AgentEnvironmentCreate] = []

    async def __call__(
        self,
        session,
        agent_id,
        data: AgentEnvironmentCreate,
        user,
        auto_start: bool = False,
        source_environment_id=None,
    ) -> AgentEnvironment:
        # Record the incoming data so tests can assert on it.
        self.captured_data.append(data)

        # Create a real DB row with status="running" so the polling loop exits
        # immediately on its first environment.status check.
        environment = AgentEnvironment(
            agent_id=agent_id,
            env_name=data.env_name,
            env_version=data.env_version,
            instance_name=data.instance_name or "stub-instance",
            type=data.type,
            config=data.config if data.config is not None else {},
            agent_sdk_conversation=data.agent_sdk_conversation,
            agent_sdk_building=data.agent_sdk_building,
            model_override_conversation=data.model_override_conversation,
            model_override_building=data.model_override_building,
            use_default_ai_credentials=data.use_default_ai_credentials,
            conversation_ai_credential_id=data.conversation_ai_credential_id,
            building_ai_credential_id=data.building_ai_credential_id,
            status="running",
            is_active=True,
        )
        session.add(environment)
        session.commit()
        session.refresh(environment)
        return environment


# ── Local helpers ─────────────────────────────────────────────────────────────


def _parse_sse_events(response_text: str) -> list[dict]:
    """Parse ``data: {...}`` lines from a raw SSE response body into dicts."""
    events = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _call_create_flow(
    client: TestClient,
    token_headers: dict[str, str],
    stub: _StubCreateEnvironment,
    body: dict,
) -> tuple[list[dict], int]:
    """POST /agents/create-flow with the given stub patched in.

    Returns (parsed_sse_events, http_status_code).
    """
    with patch(
        "app.services.agents.agent_service.EnvironmentService.create_environment",
        stub,
    ):
        r = client.post(
            f"{API}/agents/create-flow",
            headers=token_headers,
            json=body,
        )
    drain_tasks()
    return _parse_sse_events(r.text), r.status_code


def _get_agent_environment(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Return the first environment for an agent via GET /agents/{id}/environments."""
    r = client.get(
        f"{API}/agents/{agent_id}/environments",
        headers=token_headers,
    )
    assert r.status_code == 200, f"GET environments failed: {r.text}"
    envs = r.json()["data"]
    assert len(envs) >= 1, "Expected at least one environment"
    return envs[0]


def _find_event(events: list[dict], step: str) -> dict | None:
    """Return the first SSE event with the given ``step`` value, or None."""
    return next((e for e in events if e.get("step") == step), None)


def _assert_stream_success(events: list[dict]) -> dict:
    """Assert the stream contains 'agent_created' and 'environment_ready' events.

    Returns the ``agent_created`` event dict (which carries ``agent_id``).
    """
    agent_created = _find_event(events, "agent_created")
    assert agent_created is not None, (
        f"Expected 'agent_created' event in stream, got steps: "
        f"{[e.get('step') for e in events]}"
    )
    env_ready = _find_event(events, "environment_ready")
    assert env_ready is not None, (
        f"Expected 'environment_ready' event in stream, got steps: "
        f"{[e.get('step') for e in events]}"
    )
    return agent_created


# ── Test cases ────────────────────────────────────────────────────────────────


def test_create_flow_legacy_minimal_call_uses_default_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Regression: minimal POST with only description + mode still works.

    1. POST with only required fields (no env-config fields)
    2. Assert stream succeeds (agent_created + environment_ready)
    3. Assert captured AgentEnvironmentCreate.env_name == settings.DEFAULT_AGENT_ENV_NAME
    4. Assert model_override_* are None, use_default_ai_credentials is True,
       and both credential IDs are None
    5. Verify environment accessible via REST

    This ensures existing callers without the new fields are not broken.
    """
    stub = _StubCreateEnvironment()
    body = {
        "description": "A simple test agent",
        "mode": "building",
        "auto_create_session": False,
    }

    # ── Phase 1: POST and consume stream ─────────────────────────────────
    events, status_code = _call_create_flow(client, superuser_token_headers, stub, body)
    assert status_code == 200, f"Expected 200, got {status_code}"

    # ── Phase 2: Stream contains the expected milestones ──────────────────
    agent_created = _assert_stream_success(events)
    agent_id = agent_created["agent_id"]

    # ── Phase 3: Captured AgentEnvironmentCreate has correct defaults ─────
    assert len(stub.captured_data) == 1, "Expected exactly one create_environment call"
    captured = stub.captured_data[0]
    assert captured.env_name == settings.DEFAULT_AGENT_ENV_NAME, (
        f"Expected env_name={settings.DEFAULT_AGENT_ENV_NAME!r}, "
        f"got {captured.env_name!r}"
    )
    assert captured.model_override_conversation is None
    assert captured.model_override_building is None
    assert captured.use_default_ai_credentials is True
    assert captured.conversation_ai_credential_id is None
    assert captured.building_ai_credential_id is None

    # ── Phase 4: Environment accessible via REST ──────────────────────────
    env = _get_agent_environment(client, superuser_token_headers, agent_id)
    assert env["env_name"] == settings.DEFAULT_AGENT_ENV_NAME


def test_create_flow_env_name_override_propagates(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    env_name override propagates into AgentEnvironmentCreate.

    1. POST with env_name="general-env"
    2. Assert stream succeeds
    3. Assert captured env_name == "general-env" (not the default)
    4. Verify env_name on the created environment row via REST
    """
    stub = _StubCreateEnvironment()
    body = {
        "description": "Agent with custom env template",
        "mode": "building",
        "auto_create_session": False,
        "env_name": "general-env",
    }

    # ── Phase 1: POST and consume stream ─────────────────────────────────
    events, status_code = _call_create_flow(client, superuser_token_headers, stub, body)
    assert status_code == 200, f"Expected 200, got {status_code}"

    # ── Phase 2: Stream milestones ────────────────────────────────────────
    agent_created = _assert_stream_success(events)
    agent_id = agent_created["agent_id"]

    # ── Phase 3: env_name in captured data ───────────────────────────────
    assert len(stub.captured_data) == 1
    captured = stub.captured_data[0]
    assert captured.env_name == "general-env", (
        f"Expected env_name='general-env', got {captured.env_name!r}"
    )
    assert captured.env_name != settings.DEFAULT_AGENT_ENV_NAME, (
        "env_name must differ from the default when explicitly overridden"
    )

    # ── Phase 4: env_name persisted on the environment row ───────────────
    env = _get_agent_environment(client, superuser_token_headers, agent_id)
    assert env["env_name"] == "general-env"


def test_create_flow_model_overrides_propagate(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    model_override_conversation and model_override_building propagate.

    1. POST with both overrides set
    2. Assert stream succeeds
    3. Assert captured model_override_* match the posted values
    4. Verify both fields on the created environment row via REST
    """
    stub = _StubCreateEnvironment()
    body = {
        "description": "Agent with model overrides",
        "mode": "building",
        "auto_create_session": False,
        "model_override_conversation": "claude-haiku-4-5",
        "model_override_building": "claude-opus-4",
    }

    # ── Phase 1: POST and consume stream ─────────────────────────────────
    events, status_code = _call_create_flow(client, superuser_token_headers, stub, body)
    assert status_code == 200, f"Expected 200, got {status_code}"

    # ── Phase 2: Stream milestones ────────────────────────────────────────
    agent_created = _assert_stream_success(events)
    agent_id = agent_created["agent_id"]

    # ── Phase 3: Model overrides in captured data ─────────────────────────
    assert len(stub.captured_data) == 1
    captured = stub.captured_data[0]
    assert captured.model_override_conversation == "claude-haiku-4-5", (
        f"Expected model_override_conversation='claude-haiku-4-5', "
        f"got {captured.model_override_conversation!r}"
    )
    assert captured.model_override_building == "claude-opus-4", (
        f"Expected model_override_building='claude-opus-4', "
        f"got {captured.model_override_building!r}"
    )

    # ── Phase 4: Model overrides persisted on the environment row ─────────
    env = _get_agent_environment(client, superuser_token_headers, agent_id)
    assert env["model_override_conversation"] == "claude-haiku-4-5"
    assert env["model_override_building"] == "claude-opus-4"


def test_create_flow_explicit_credentials_propagate(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Explicit credential IDs with use_default_ai_credentials=False propagate.

    1. Pre-create two AI credentials (conversation + building)
    2. POST with use_default_ai_credentials=False and both credential IDs
    3. Assert stream succeeds
    4. Assert captured use_default_ai_credentials is False and both IDs match
    5. Verify fields on the created environment row via REST

    Note: the stub bypasses EnvironmentService credential-ownership validation,
    so any valid AI credential UUID can be used regardless of default/non-default status.
    """
    # ── Phase 1: Create two AI credentials for this test ─────────────────
    conv_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-conv-key",
        name="test-cred-conv",
    )
    build_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-build-key",
        name="test-cred-build",
    )
    conv_cred_id = conv_cred["id"]
    build_cred_id = build_cred["id"]

    # ── Phase 2: POST with explicit credentials ───────────────────────────
    stub = _StubCreateEnvironment()
    body = {
        "description": "Agent with explicit credentials",
        "mode": "building",
        "auto_create_session": False,
        "use_default_ai_credentials": False,
        "conversation_ai_credential_id": conv_cred_id,
        "building_ai_credential_id": build_cred_id,
    }

    events, status_code = _call_create_flow(client, superuser_token_headers, stub, body)
    assert status_code == 200, f"Expected 200, got {status_code}"

    # ── Phase 3: Stream milestones ────────────────────────────────────────
    agent_created = _assert_stream_success(events)
    agent_id = agent_created["agent_id"]

    # ── Phase 4: Credential fields in captured data ───────────────────────
    assert len(stub.captured_data) == 1
    captured = stub.captured_data[0]
    assert captured.use_default_ai_credentials is False, (
        f"Expected use_default_ai_credentials=False, "
        f"got {captured.use_default_ai_credentials!r}"
    )
    assert str(captured.conversation_ai_credential_id) == conv_cred_id, (
        f"Expected conversation_ai_credential_id={conv_cred_id!r}, "
        f"got {captured.conversation_ai_credential_id!r}"
    )
    assert str(captured.building_ai_credential_id) == build_cred_id, (
        f"Expected building_ai_credential_id={build_cred_id!r}, "
        f"got {captured.building_ai_credential_id!r}"
    )

    # ── Phase 5: Credential fields persisted on the environment row ───────
    env = _get_agent_environment(client, superuser_token_headers, agent_id)
    assert env["use_default_ai_credentials"] is False
    assert env["conversation_ai_credential_id"] == conv_cred_id
    assert env["building_ai_credential_id"] == build_cred_id


def test_create_flow_malformed_credential_uuid_returns_422(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Pydantic rejects a non-UUID string for conversation_ai_credential_id with 422.

    This validates the URL-roundtrip path where a malformed UUID could arrive
    from the frontend search-params coercion (Phase 4.2) or direct API calls.

    1. POST with conversation_ai_credential_id="not-a-uuid"
    2. Assert HTTP 422 (Pydantic validation error before any service code runs)
    3. No need for the environment stub (Pydantic rejects at route layer)
    """
    body = {
        "description": "Should fail with 422",
        "mode": "building",
        "auto_create_session": False,
        "conversation_ai_credential_id": "not-a-uuid",
    }

    r = client.post(
        f"{API}/agents/create-flow",
        headers=superuser_token_headers,
        json=body,
    )
    assert r.status_code == 422, (
        f"Expected 422 for malformed UUID, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", [])
    # Pydantic v2 returns a list of error dicts for validation failures
    assert isinstance(detail, list) and len(detail) > 0, (
        f"Expected validation error detail list, got: {detail!r}"
    )


def test_create_flow_combined_all_new_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    All six new env-config fields propagate correctly when combined.

    1. Pre-create two AI credentials
    2. POST with all six new fields set, plus agent_sdk_conversation + agent_sdk_building
    3. Assert stream succeeds
    4. Assert every new field in the captured AgentEnvironmentCreate matches the request
    5. Verify all fields on the environment row via REST

    This is the full-path regression that catches any field omitted in the
    route → service → AgentEnvironmentCreate wiring chain.
    """
    # ── Phase 1: Create two AI credentials ───────────────────────────────
    conv_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-full-conv",
        name="test-full-conv-cred",
    )
    build_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-full-build",
        name="test-full-build-cred",
    )
    conv_cred_id = conv_cred["id"]
    build_cred_id = build_cred["id"]

    # ── Phase 2: POST with all six new fields ─────────────────────────────
    stub = _StubCreateEnvironment()
    body = {
        "description": "Full featured agent with all env-config fields",
        "mode": "building",
        "auto_create_session": False,
        # Legacy SDK fields (kept from original implementation)
        "agent_sdk_conversation": "claude-code/anthropic",
        "agent_sdk_building": "claude-code/anthropic",
        # Six new env-config fields
        "env_name": "python-env-advanced",
        "model_override_conversation": "claude-haiku-4-5",
        "model_override_building": "claude-sonnet-4-5",
        "use_default_ai_credentials": False,
        "conversation_ai_credential_id": conv_cred_id,
        "building_ai_credential_id": build_cred_id,
    }

    events, status_code = _call_create_flow(client, superuser_token_headers, stub, body)
    assert status_code == 200, f"Expected 200, got {status_code}"

    # ── Phase 3: Stream milestones ────────────────────────────────────────
    agent_created = _assert_stream_success(events)
    agent_id = agent_created["agent_id"]

    # ── Phase 4: Every new field in captured AgentEnvironmentCreate ───────
    assert len(stub.captured_data) == 1
    captured = stub.captured_data[0]

    assert captured.env_name == "python-env-advanced", (
        f"env_name mismatch: {captured.env_name!r}"
    )
    assert captured.model_override_conversation == "claude-haiku-4-5", (
        f"model_override_conversation mismatch: {captured.model_override_conversation!r}"
    )
    assert captured.model_override_building == "claude-sonnet-4-5", (
        f"model_override_building mismatch: {captured.model_override_building!r}"
    )
    assert captured.use_default_ai_credentials is False, (
        f"use_default_ai_credentials mismatch: {captured.use_default_ai_credentials!r}"
    )
    assert str(captured.conversation_ai_credential_id) == conv_cred_id, (
        f"conversation_ai_credential_id mismatch: {captured.conversation_ai_credential_id!r}"
    )
    assert str(captured.building_ai_credential_id) == build_cred_id, (
        f"building_ai_credential_id mismatch: {captured.building_ai_credential_id!r}"
    )
    # Also verify the legacy SDK fields pass through
    assert captured.agent_sdk_conversation == "claude-code/anthropic", (
        f"agent_sdk_conversation mismatch: {captured.agent_sdk_conversation!r}"
    )
    assert captured.agent_sdk_building == "claude-code/anthropic", (
        f"agent_sdk_building mismatch: {captured.agent_sdk_building!r}"
    )

    # ── Phase 5: All fields persisted on the environment row via REST ──────
    env = _get_agent_environment(client, superuser_token_headers, agent_id)
    assert env["env_name"] == "python-env-advanced"
    assert env["model_override_conversation"] == "claude-haiku-4-5"
    assert env["model_override_building"] == "claude-sonnet-4-5"
    assert env["use_default_ai_credentials"] is False
    assert env["conversation_ai_credential_id"] == conv_cred_id
    assert env["building_ai_credential_id"] == build_cred_id
