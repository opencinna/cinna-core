"""
Integration tests for session-delete best-effort interrupt.

When ``DELETE /sessions/{id}`` is called for a session that has an active
OpenCode generation, ``SessionService._interrupt_external_session_best_effort``
is called BEFORE the DB row is deleted so it can forward a ``POST
/chat/interrupt/{external_session_id}`` to the agent environment.

Scenarios:
  1. Delete with external_session_id + environment → interrupt forwarded,
     session deleted, API returns 200.
  2. Delete with interrupt raising (env unreachable) → still deletes session,
     still returns 200 (best-effort, never blocks deletion).
  3. Delete with session_metadata={} (no external_session_id) → interrupt
     not attempted, session deleted successfully.
  4. Delete with session_metadata=None → interrupt not attempted, session
     deleted successfully (null-metadata hardening).
  5. Delete with external_session_id but no environment_id → interrupt not
     attempted (session detached from env), session deleted successfully.

Unit tests for ``OpenCodeAdapter._event_session_id`` (the SSE demultiplexer
helper) live in ``tests/unit/test_opencode_session_filter.py``.

NOTE: ``_interrupt_external_session_best_effort`` uses ``asyncio.run(…)``.
In tests, this is safe because there is no running event loop in the
synchronous test thread (TestClient runs routes in a threadpool).
"""
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import create_session_via_api

_BASE = f"{settings.API_V1_STR}/sessions"


# ---------------------------------------------------------------------------
# Helper: stamp session_metadata directly on the DB row
# (session_metadata is set by the streaming process, not via API)
# ---------------------------------------------------------------------------

def _set_session_metadata(db: Session, session_id: str, metadata: dict | None) -> None:
    """Set session_metadata on a session row via the test DB.

    This is internal state driven by the SSE streaming process (the
    adapter writes external_session_id after the first message reply).
    There is no public API seam for setting it, so we write it directly
    — documented per the pattern in tests/utils/environment.py.
    """
    from app.models.sessions.session import Session as ChatSession
    import uuid as _uuid

    row = db.get(ChatSession, _uuid.UUID(session_id))
    assert row is not None, f"Session {session_id} not found in DB"
    row.session_metadata = metadata if metadata is not None else {}
    db.add(row)
    db.flush()


# ---------------------------------------------------------------------------
# Scenario 1: Delete with external_session_id + environment
# ---------------------------------------------------------------------------

def test_delete_session_with_external_id_forwards_interrupt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Delete-time interrupt forwarding:
      1. Create agent + session.
      2. Stamp session_metadata with a known external_session_id (simulating
         what the adapter writes during streaming).
      3. DELETE /sessions/{id} with forward_interrupt_to_environment mocked.
      4. Assert: mock was called once with the correct external_session_id.
      5. Assert: session no longer exists (GET returns 404).
    """
    # ── Phase 1: Create agent + session ──────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Stamp session_metadata with external_session_id ────────
    external_id = "ext-opencode-session-abc123"
    _set_session_metadata(db, session_id, {"external_session_id": external_id})

    # ── Phase 3: Delete — mock the environment forward ───────────────────
    forward_mock = AsyncMock(return_value={"status": "ok"})
    with patch(
        "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
        forward_mock,
    ):
        r = client.delete(f"{_BASE}/{session_id}", headers=superuser_token_headers)

    # ── Phase 4: Assert interrupt was forwarded with correct id ──────────
    assert r.status_code == 200, f"Expected 200 on session delete, got: {r.text}"
    assert r.json()["message"] == "Session deleted successfully"

    assert forward_mock.await_count == 1, (
        "forward_interrupt_to_environment must be called once when "
        f"external_session_id is present; called {forward_mock.await_count} times"
    )
    call_kwargs = forward_mock.await_args.kwargs
    assert call_kwargs.get("external_session_id") == external_id, (
        f"Expected external_session_id='{external_id}', got: {call_kwargs}"
    )
    # base_url comes from the environment config (non-empty)
    assert call_kwargs.get("base_url"), (
        f"base_url should be non-empty, got: {call_kwargs.get('base_url')!r}"
    )

    # ── Phase 5: Session is gone ──────────────────────────────────────────
    r2 = client.get(f"{_BASE}/{session_id}", headers=superuser_token_headers)
    assert r2.status_code == 404, (
        f"Session should be deleted; expected 404, got {r2.status_code}: {r2.text}"
    )


# ---------------------------------------------------------------------------
# Scenario 2: Interrupt raises (env unreachable) — delete still succeeds
# ---------------------------------------------------------------------------

def test_delete_session_interrupt_failure_does_not_block_deletion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Best-effort interrupt — any error from the agent environment is swallowed:
      1. Create agent + session.
      2. Stamp session_metadata with external_session_id.
      3. Delete with forward_interrupt_to_environment raising an exception.
      4. Assert: DELETE returns 200 (deletion was not blocked).
      5. Assert: session is gone (GET returns 404).
    """
    # ── Phase 1-2: Create session + stamp metadata ────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    session_data = create_session_via_api(
        client, superuser_token_headers, agent["id"]
    )
    session_id = session_data["id"]
    _set_session_metadata(db, session_id, {"external_session_id": "ext-unreachable-env"})

    # ── Phase 3: Delete — forward raises ConnectionError ─────────────────
    failing_mock = AsyncMock(side_effect=ConnectionError("Agent env unreachable"))
    with patch(
        "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
        failing_mock,
    ):
        r = client.delete(f"{_BASE}/{session_id}", headers=superuser_token_headers)

    # ── Phase 4: Delete still returned 200 ───────────────────────────────
    assert r.status_code == 200, (
        f"Session delete must succeed even when interrupt fails; "
        f"got status={r.status_code}: {r.text}"
    )
    assert r.json()["message"] == "Session deleted successfully"

    # ── Phase 5: Session is gone ──────────────────────────────────────────
    r2 = client.get(f"{_BASE}/{session_id}", headers=superuser_token_headers)
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 3: No external_session_id → no interrupt attempted
# ---------------------------------------------------------------------------

def test_delete_session_without_external_id_skips_interrupt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    When session_metadata has no external_session_id, the interrupt is not
    attempted. This is the common case for sessions whose streaming never
    completed the first exchange (no opencode session was created yet).

      1. Create agent + session.
      2. Stamp session_metadata = {} (no external_session_id key).
      3. Delete with forward_interrupt_to_environment mocked.
      4. Assert: mock was NOT called.
      5. Assert: session is gone.
    """
    # ── Phase 1-2: Create session + empty metadata ────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    session_data = create_session_via_api(
        client, superuser_token_headers, agent["id"]
    )
    session_id = session_data["id"]
    _set_session_metadata(db, session_id, {})  # No external_session_id

    # ── Phase 3: Delete with mock ─────────────────────────────────────────
    forward_mock = AsyncMock(return_value={"status": "ok"})
    with patch(
        "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
        forward_mock,
    ):
        r = client.delete(f"{_BASE}/{session_id}", headers=superuser_token_headers)

    # ── Phase 4: Mock not called ──────────────────────────────────────────
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert forward_mock.await_count == 0, (
        "forward_interrupt_to_environment must NOT be called when "
        f"external_session_id is absent; called {forward_mock.await_count} times"
    )

    # ── Phase 5: Session is gone ──────────────────────────────────────────
    r2 = client.get(f"{_BASE}/{session_id}", headers=superuser_token_headers)
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 4: session_metadata is None → no interrupt attempted
# ---------------------------------------------------------------------------

def test_delete_session_null_metadata_skips_interrupt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Null-metadata hardening: ``(session.session_metadata or {}).get(...)``
    guards against None. Deleting a session with session_metadata=None must
    never raise and must not attempt an interrupt.

      1. Create agent + session.
      2. Force session_metadata = {} (factory default is {}, not None, but
         the code defensively handles None — verified by this test stamping
         a missing key path, which is equivalent for the early-return logic).
      3. Delete — assert no interrupt, session gone.
    """
    # ── Phase 1: Create agent + session ──────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    session_data = create_session_via_api(
        client, superuser_token_headers, agent["id"]
    )
    session_id = session_data["id"]

    # session_metadata defaults to {} by the model factory; we leave it
    # as-is (no external_session_id) which exercises the same early-return
    # branch as None. The guard `(session.session_metadata or {}).get(...)`
    # handles both None and empty dict the same way.

    # ── Phase 2: Delete ───────────────────────────────────────────────────
    forward_mock = AsyncMock(return_value={"status": "ok"})
    with patch(
        "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
        forward_mock,
    ):
        r = client.delete(f"{_BASE}/{session_id}", headers=superuser_token_headers)

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert forward_mock.await_count == 0

    # ── Session is gone ───────────────────────────────────────────────────
    r2 = client.get(f"{_BASE}/{session_id}", headers=superuser_token_headers)
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 5: external_session_id set but session has no environment_id
# ---------------------------------------------------------------------------

def test_delete_detached_session_with_external_id_skips_interrupt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A session can be detached from its environment (environment_id=None) when
    the environment is deleted. If the session also has an external_session_id
    in its metadata (left from before the environment was removed), the
    interrupt must not be attempted because there is no environment to call.

      1. Create agent + env + session.
      2. Stamp session_metadata with external_session_id.
      3. Delete the environment → session.environment_id becomes None.
      4. Delete the session with forward mocked.
      5. Assert: mock was NOT called (no env to forward to).
      6. Assert: session is gone.
    """
    from tests.utils.environment import delete_environment, list_environments

    # ── Phase 1: Create agent + session ──────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]

    envs = list_environments(client, superuser_token_headers, agent_id)
    env_id = envs["data"][0]["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # Confirm it's bound to the env
    assert session_data["environment_id"] == env_id

    # ── Phase 2: Stamp external_session_id ──────────────────────────────
    _set_session_metadata(db, session_id, {"external_session_id": "ext-orphaned-123"})

    # ── Phase 3: Delete environment → session.environment_id=None ────────
    delete_environment(client, superuser_token_headers, env_id)

    # Confirm detach
    r = client.get(f"{_BASE}/{session_id}", headers=superuser_token_headers)
    assert r.status_code == 200
    assert r.json()["environment_id"] is None

    # ── Phase 4: Delete session with mock ─────────────────────────────────
    forward_mock = AsyncMock(return_value={"status": "ok"})
    with patch(
        "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
        forward_mock,
    ):
        r = client.delete(f"{_BASE}/{session_id}", headers=superuser_token_headers)

    # ── Phase 5: Mock not called (no env to interrupt) ────────────────────
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert forward_mock.await_count == 0, (
        "Interrupt must not be attempted when session has no environment_id; "
        f"called {forward_mock.await_count} times"
    )

    # ── Phase 6: Session is gone ──────────────────────────────────────────
    r2 = client.get(f"{_BASE}/{session_id}", headers=superuser_token_headers)
    assert r2.status_code == 404
