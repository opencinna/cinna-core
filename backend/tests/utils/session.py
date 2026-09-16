"""Helpers to query sessions via API for tests."""
import asyncio
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings


def list_sessions(
    client: TestClient,
    token_headers: dict[str, str],
    limit: int = 100,
    guest_share_id: str | None = None,
) -> list[dict]:
    """List sessions via GET /api/v1/sessions/. Returns the data array."""
    url = f"{settings.API_V1_STR}/sessions/?limit={limit}"
    if guest_share_id:
        url += f"&guest_share_id={guest_share_id}"
    r = client.get(url, headers=token_headers)
    assert r.status_code == 200, f"List sessions failed: {r.text}"
    return r.json()["data"]


def create_session_via_api(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    mode: str = "conversation",
    guest_share_id: str | None = None,
) -> dict:
    """Create session via POST /api/v1/sessions/."""
    payload: dict = {"agent_id": agent_id, "mode": mode}
    if guest_share_id is not None:
        payload["guest_share_id"] = guest_share_id
    r = client.post(
        f"{settings.API_V1_STR}/sessions/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Create session failed: {r.text}"
    return r.json()


def get_session(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
) -> dict:
    """Get a single session via GET /api/v1/sessions/{id}.

    Returns the ``SessionPublicExtended`` JSON, which carries ``title`` and
    ``interaction_status`` among other fields.
    """
    r = client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get session failed: {r.text}"
    return r.json()


def get_agent_session(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Find the single session belonging to an agent. Asserts exactly one exists."""
    sessions = list_sessions(client, token_headers)
    agent_sessions = [s for s in sessions if s["agent_id"] == agent_id]
    assert len(agent_sessions) == 1, (
        f"Expected 1 session for agent {agent_id}, got {len(agent_sessions)}"
    )
    return agent_sessions[0]


def create_session_with_block(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    dashboard_block_id: str,
    mode: str = "conversation",
) -> dict:
    """Create a session tagged with a dashboard block via POST /api/v1/sessions/."""
    payload: dict = {
        "agent_id": agent_id,
        "mode": mode,
        "dashboard_block_id": dashboard_block_id,
    }
    r = client.post(
        f"{settings.API_V1_STR}/sessions/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Create session with block failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Active-streaming-manager helpers
#
# ActiveStreamingManager is internal state with no public API surface —
# there is no HTTP endpoint to register or unregister an active stream.
# These helpers isolate the app.services import to this utility module
# so individual test files remain free of app.services imports.
# ---------------------------------------------------------------------------

def register_active_stream(session_id: uuid.UUID, external_session_id: str) -> None:
    """Register a session as actively streaming in the in-process manager.

    Used by A2A cancel tests to simulate a still-running stream after the
    synchronous test call has completed.
    """
    from app.services.sessions.active_streaming_manager import active_streaming_manager

    asyncio.run(
        active_streaming_manager.register_stream(
            session_id=session_id,
            external_session_id=external_session_id,
        )
    )


def unregister_active_stream(session_id: uuid.UUID) -> None:
    """Unregister a session from the active-streaming manager.

    Call this in a ``finally`` block after ``register_active_stream`` to
    prevent leaking state across tests.
    """
    from app.services.sessions.active_streaming_manager import active_streaming_manager

    asyncio.run(active_streaming_manager.unregister_stream(session_id))


# ---------------------------------------------------------------------------
# Status-repair (Pass B) seam
#
# ``interaction_status`` / ``streaming_started_at`` / ``updated_at`` on the
# claim path the status-repair sweep reads (see
# ``app/services/system/status_repair_sessions.py``).
# ---------------------------------------------------------------------------

def force_session_interaction_claim(
    db,
    session_id: str | uuid.UUID,
    *,
    interaction_status: str,
    streaming_started_at=None,
    updated_at=None,
    pending_messages_count: int | None = None,
    stream_heartbeat_at=None,
    set_stream_heartbeat: bool = False,
) -> None:
    """Force a session's interaction claim directly on the test DB.

    Documented seam for the status-repair sweep's tests (Pass B,
    ``app/services/system/status_repair_sessions.py``). Two different
    honesty levels hide behind one signature:

    * ``interaction_status="pending_stream"`` IS reachable through the API —
      sending a message while the session's environment is not running
      leaves the row in exactly this state, with a real ``updated_at``. The
      only thing this seam does for that case is move ``updated_at`` back in
      time, because the sweep's threshold (15 min by default) is too long to
      block a test on, the same "age it, don't fabricate it" seam as
      ``age_environment_status_changed_at``.
    * ``interaction_status="running"`` left stuck is, by contrast,
      unreachable through this API in-process. ``process_pending_messages``
      clears it in a bare ``finally`` (not ``except Exception``) shielded
      against cancellation, so anything this test process can do to a
      streaming task — raise, cancel — is exactly what that `finally` already
      recovers from. Only a killed *process* leaves the claim standing, and
      nothing in a single pytest process can simulate that. This branch
      therefore does not "age a real state" — it constructs the state
      directly, the same way ``set_environment_status`` does for
      ``AgentEnvironment.status`` when the equivalent real path is stubbed
      out for tests.

    ``pending_messages_count``, when given, is set independent of the actual
    pending-message rows — so a test can put the counter deliberately out of
    sync and assert Pass B's recompute step corrects it.

    ``stream_heartbeat_at`` (a datetime; only written when
    ``set_stream_heartbeat=True``, so existing callers that don't pass it
    leave ``session_metadata`` alone) is the same seam extended for the
    orphaned-streams pass (plan D10, §3.4): that pass reads
    ``session_metadata["stream_heartbeat_at"]`` to decide whether a
    ``running`` session's turn is still beating on some worker. Pass ``None``
    to simulate a legacy session with no heartbeat at all.
    """
    from datetime import UTC, datetime

    from app.models.sessions.session import Session as ChatSession
    from app.services.sessions.stream_heartbeat import STREAM_HEARTBEAT_KEY

    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    chat_session = db.get(ChatSession, session_id)
    assert chat_session is not None, f"Session {session_id} not found"
    chat_session.interaction_status = interaction_status
    chat_session.streaming_started_at = streaming_started_at
    if updated_at is not None:
        chat_session.updated_at = updated_at
    else:
        chat_session.updated_at = datetime.now(UTC)
    if pending_messages_count is not None:
        chat_session.pending_messages_count = pending_messages_count
    if set_stream_heartbeat:
        from sqlalchemy.orm.attributes import flag_modified

        metadata = dict(chat_session.session_metadata or {})
        if stream_heartbeat_at is None:
            metadata.pop(STREAM_HEARTBEAT_KEY, None)
        else:
            metadata[STREAM_HEARTBEAT_KEY] = stream_heartbeat_at.isoformat()
        chat_session.session_metadata = metadata
        flag_modified(chat_session, "session_metadata")
    db.add(chat_session)
    db.commit()
    db.refresh(chat_session)


# ---------------------------------------------------------------------------
# Orphaned-streams pass seam
#
# A genuinely abandoned agent row (``streaming_in_progress`` stuck true) only
# happens when the whole process dies mid-stream — the defensive ``finally``
# in ``MessageService.stream_message_with_events`` seals anything this test
# process can do to a task (cancel, raise) via ``_seal_unfinished_turn``, the
# same reasoning ``force_session_interaction_claim`` documents above for the
# session-level claim. This constructs the row directly rather than trying
# to out-race that ``finally``.
# ---------------------------------------------------------------------------

def force_orphaned_agent_message(
    db,
    session_id: str | uuid.UUID,
    *,
    timestamp,
    heartbeat_at,
    content: str = "Orphaned partial reply",
    streaming_events: list[dict] | None = None,
) -> dict:
    """Create an agent message row in the exact shape the orphaned-streams
    status-repair pass reads: ``streaming_in_progress=True``, an optional
    ``stream_heartbeat_at`` (a datetime, or ``None`` for a legacy row written
    before the heartbeat existed), and an explicit ``timestamp`` (the pass's
    age check). Returns ``{"id": ..., "session_id": ...}``.
    """
    from app.services.sessions.message_service import MessageService

    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    metadata: dict = {
        "streaming_in_progress": True,
        "streaming_events": streaming_events or [],
    }
    if heartbeat_at is not None:
        from app.services.sessions.stream_heartbeat import STREAM_HEARTBEAT_KEY

        metadata[STREAM_HEARTBEAT_KEY] = heartbeat_at.isoformat()

    message = MessageService.create_message(
        session=db,
        session_id=session_id,
        role="agent",
        content=content,
        message_metadata=metadata,
    )
    message.timestamp = timestamp
    db.add(message)
    db.commit()
    db.refresh(message)
    return {"id": str(message.id), "session_id": str(message.session_id)}


# ---------------------------------------------------------------------------
# Stream heartbeat seam (plan D10)
#
# The per-turn ticker is off under pytest (``HEARTBEAT_ENABLED``), because the
# API harness hands every caller one shared DB session and the ticker writes
# from a worker thread. These helpers run the real ``StreamHeartbeat`` against
# the test session with ``asyncio.to_thread`` executed inline, so every write
# is serial and uses the real SQL (row lock, JSON rewrite).
# ---------------------------------------------------------------------------

def make_stream_heartbeat(
    db,
    session_id: str | uuid.UUID,
    *,
    message_id: str | uuid.UUID | None = None,
    started_at=None,
):
    """Build a ``StreamHeartbeat`` bound to the test session.

    ``started_at`` backdates the turn's first beat, for a turn that began
    before the stale heartbeat a test forged.
    """
    from app.services.sessions.stream_heartbeat import StreamHeartbeat
    from tests.utils.db_proxy import NonClosingSessionProxy

    heartbeat = StreamHeartbeat(
        _as_uuid(session_id), lambda: NonClosingSessionProxy(db),
    )
    if message_id is not None:
        heartbeat.attach_message(_as_uuid(message_id))
    if started_at is not None:
        heartbeat._started_at = started_at
    return heartbeat


def beat_stream_heartbeat(heartbeat) -> None:
    """Run exactly one beat, synchronously."""
    heartbeat._beat()


def stop_stream_heartbeat(heartbeat) -> None:
    """Run ``stop()`` (the turn-end stamp clear) with ``to_thread`` inline."""
    from unittest.mock import patch

    from app.services.sessions import stream_heartbeat as module

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    heartbeat._stopped.set()
    with patch.object(module.asyncio, "to_thread", inline_to_thread):
        asyncio.run(heartbeat.stop())


def run_stream_heartbeat_ticker(
    heartbeat, *, beats: int = 2, interval: float = 0.01,
) -> list:
    """Run the real ticker for at least ``beats`` beats, then ``stop()`` it.

    Returns the session-side heartbeat values seen after each beat, read
    through the same session.
    """
    from unittest.mock import patch

    from app.models.sessions.session import Session as ChatSession
    from app.services.sessions import stream_heartbeat as module

    db = heartbeat._get_db().__enter__()
    seen: list = []
    original_beat = heartbeat._beat

    def recording_beat() -> None:
        original_beat()
        row = db.get(ChatSession, heartbeat._session_id, populate_existing=True)
        seen.append((row.session_metadata or {}).get(module.STREAM_HEARTBEAT_KEY))

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def scenario() -> None:
        heartbeat._beat = recording_beat
        heartbeat.start()
        while len(seen) < beats:
            await asyncio.sleep(interval)
        await heartbeat.stop()

    with patch.object(module, "HEARTBEAT_ENABLED", True), \
            patch.object(module, "HEARTBEAT_INTERVAL_SECONDS", interval), \
            patch.object(module.asyncio, "to_thread", inline_to_thread):
        asyncio.run(scenario())
    return seen


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return uuid.UUID(value) if isinstance(value, str) else value


def create_aborted_agent_message(
    db,
    session_id: str | uuid.UUID,
    *,
    content: str = "Partial reply before crash",
) -> dict:
    """Create and immediately seal an agent row as ``aborted``.

    Runs the real ``MessageService._apply_aborted`` (the same finalize the
    orphan pass and a cancelled-task ``finally`` both call), for tests that
    need a genuinely finalized ``aborted`` row — e.g. to check how history /
    ``tasks/get`` render it — without caring how it got sealed. Returns
    ``{"id": ..., "session_id": ...}``.
    """
    from app.services.sessions.message_service import MessageService

    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    message = MessageService.create_message(
        session=db,
        session_id=session_id,
        role="agent",
        content=content,
        message_metadata={"streaming_in_progress": True, "streaming_events": []},
    )
    assert MessageService._apply_aborted(db, message, None, None) is True
    return {"id": str(message.id), "session_id": str(message.session_id)}


def force_pending_user_message(
    db,
    session_id: str | uuid.UUID,
    *,
    content: str,
    client_message_id: str | None = None,
) -> dict:
    """Create a ``user`` row stuck ``sent_to_agent_status="pending"`` with no
    turn ever kicked off for it — the "stored but never delivered" state a
    duplicate ``client_message_id`` send is meant to re-drive (plan D9).
    Reachable through the API only by racing message ingestion against an
    environment that never becomes ready, which this seam skips straight to.
    Returns ``{"id": ..., "session_id": ...}``.
    """
    from app.services.sessions.message_service import MessageService

    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    metadata = {"client_message_id": client_message_id} if client_message_id else {}
    message = MessageService.create_message(
        session=db,
        session_id=session_id,
        role="user",
        content=content,
        message_metadata=metadata,
        sent_to_agent_status="pending",
    )
    return {"id": str(message.id), "session_id": str(message.session_id)}
