"""
Integration tests for the orphaned-streams status-repair pass (C3 + D10,
plan: a2a_crash_recovery_plan.md §3.4, §6 T3).

Drives ``repair_orphaned_streams`` directly via ``RepairTick`` — the
documented exemption from Rule 1 for status-repair passes (see
``tests/utils/status_repair.py`` and ``agents_status_repair_test.py``'s
module docstring): the sweep never ticks under pytest, so the pass function
itself is the only test surface.

**Seams used.** A genuinely orphaned agent row (``streaming_in_progress``
stuck ``True`` with no live writer) only happens when the whole *process*
dies mid-stream — nothing a single pytest process can produce, since the
defensive ``finally`` in ``stream_message_with_events`` seals anything this
process can do to its own task (see ``force_orphaned_agent_message``'s
docstring in ``tests/utils/session.py``). These tests construct the row and
its ``stream_heartbeat_at`` directly via that seam, and age it via
``timestamp`` — the same "age it, don't fabricate the reachable parts"
posture ``force_session_interaction_claim`` already uses for Pass B.

A row whose stream is not registered in this process, but whose heartbeat is
fresh, stands in for a turn running on a sibling backend worker.

The heartbeat writer itself is covered by
``tests/unit/test_stream_heartbeat.py``.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role
from tests.utils.session import (
    create_session_via_api,
    force_orphaned_agent_message,
    force_session_interaction_claim,
    get_session,
    register_active_stream,
    unregister_active_stream,
)
from tests.utils.status_repair import RepairTick


def _setup_session(client: TestClient, headers: dict[str, str]) -> str:
    agent = create_agent_via_api(client, headers, name="Orphan Repair Agent")
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None
    session = create_session_via_api(client, headers, agent["id"])
    return session["id"]


def _age(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _agent_messages(client: TestClient, headers: dict, session_id: str) -> list[dict]:
    return get_messages_by_role(client, headers, session_id, role="agent")


# ── Stale heartbeat: orphaned ───────────────────────────────────────────────


def test_stale_heartbeat_seals_message_aborted_and_clears_session(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """Row and session both last beat 3 minutes ago (past the 2-minute
    bound): the message is sealed ``aborted`` and the ``running`` claim is
    cleared, in one tick.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(5),
        stream_heartbeat_at=_age(3),
        set_stream_heartbeat=True,
    )
    force_orphaned_agent_message(db, session_id, timestamp=_age(5), heartbeat_at=_age(3))

    repaired = RepairTick(db).run_orphaned_streams()
    assert repaired == 2, "expected both the message and the session repaired"

    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert len(messages) == 1
    assert messages[0]["status"] == "aborted"
    assert messages[0]["message_metadata"]["streaming_in_progress"] is False

    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] != "running"


def test_stale_heartbeat_session_without_agent_row_is_cleared(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """A turn that died before its first assistant event leaves only the
    session claim. A stale session heartbeat is enough to clear it.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(4),
        stream_heartbeat_at=_age(4),
        set_stream_heartbeat=True,
    )

    tick = RepairTick(db)
    repaired = tick.run_orphaned_streams()

    assert repaired == 1
    assert _agent_messages(client, superuser_token_headers, session_id) == []
    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == ""
    assert tick.is_session_in_motion(session_id), "Pass D must be told"


def test_stale_heartbeat_but_stream_registered_here_is_left_untouched(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """In-process state is a skip guard: this worker still has the stream,
    so a stale heartbeat (e.g. a slow DB write) is not acted on.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_orphaned_agent_message(db, session_id, timestamp=_age(5), heartbeat_at=_age(3))

    register_active_stream(uuid.UUID(session_id), "ext-still-live")
    try:
        repaired = RepairTick(db).run_orphaned_streams()
    finally:
        unregister_active_stream(uuid.UUID(session_id))

    assert repaired == 0
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["message_metadata"]["streaming_in_progress"] is True
    assert messages[0]["status"] == ""


# ── Fresh heartbeat: alive, wherever it runs ────────────────────────────────


def test_fresh_heartbeat_on_another_workers_turn_is_left_untouched(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """Multi-worker regression: the turn is not registered in this process
    (it runs on a sibling worker) and is well past the 2-minute bound, but
    its heartbeat is fresh. Neither the row nor the session may be touched.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
        stream_heartbeat_at=_age(0.2),
        set_stream_heartbeat=True,
    )
    force_orphaned_agent_message(db, session_id, timestamp=_age(10), heartbeat_at=_age(0.2))

    repaired = RepairTick(db).run_orphaned_streams()

    assert repaired == 0
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["message_metadata"]["streaming_in_progress"] is True
    assert messages[0]["status"] == ""
    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == "running"


def test_live_turn_older_than_max_age_with_fresh_heartbeat_is_left_untouched(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """A turn running for 2.5 hours that still beats: past the 120-minute
    bound, yet neither the orphan pass nor Pass B touches it.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(150),
        stream_heartbeat_at=_age(0.3),
        set_stream_heartbeat=True,
    )
    force_orphaned_agent_message(db, session_id, timestamp=_age(150), heartbeat_at=_age(0.3))

    tick = RepairTick(db)
    assert tick.run_sessions() == 0
    assert tick.run_orphaned_streams() == 0

    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["message_metadata"]["streaming_in_progress"] is True
    assert messages[0]["status"] == ""
    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == "running"


# ── Legacy rows (no heartbeat): the long bound ──────────────────────────────


def test_legacy_row_past_min_age_but_not_max_age_is_left_untouched(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """No heartbeat at all (written before D10): the short bound is not
    evidence. Must wait for the 120-minute bound.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_orphaned_agent_message(db, session_id, timestamp=_age(3), heartbeat_at=None)

    assert RepairTick(db).run_orphaned_streams() == 0
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["message_metadata"]["streaming_in_progress"] is True


def test_legacy_session_with_no_heartbeat_is_left_to_pass_b(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """A ``running`` session without a heartbeat is not cleared by the orphan
    pass; Pass B's 120-minute rule owns it.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
        stream_heartbeat_at=None,
        set_stream_heartbeat=True,
    )

    assert RepairTick(db).run_orphaned_streams() == 0
    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == "running"


def test_legacy_row_past_max_age_on_idle_session_seals_aborted(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """No heartbeat, past the 120-minute bound, session not ``running`` and
    not touched by this tick: repaired.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_orphaned_agent_message(db, session_id, timestamp=_age(121), heartbeat_at=None)

    assert RepairTick(db).run_orphaned_streams() == 1
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["status"] == "aborted"


def test_legacy_row_past_max_age_but_session_in_motion_is_left_untouched(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """Same as above, except this tick already moved the session (e.g. Pass
    B cleared it): no action on stale evidence.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_orphaned_agent_message(db, session_id, timestamp=_age(121), heartbeat_at=None)

    tick = RepairTick(db)
    tick.note_session_in_motion(session_id)
    assert tick.run_orphaned_streams() == 0
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["message_metadata"]["streaming_in_progress"] is True


# ── Idempotence ─────────────────────────────────────────────────────────────


def test_stale_row_is_sealed_once(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """A second tick finds nothing: the sealed row is no longer a candidate."""
    session_id = _setup_session(client, superuser_token_headers)
    force_orphaned_agent_message(db, session_id, timestamp=_age(10), heartbeat_at=_age(9))

    assert RepairTick(db).run_orphaned_streams() == 1
    assert RepairTick(db).run_orphaned_streams() == 0
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["status"] == "aborted"


def test_already_finalized_row_is_never_selected(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """A row that is not ``streaming_in_progress`` is excluded by the pass's
    own selection query, whatever its heartbeat says.
    """
    from app.models.sessions.session import SessionMessage

    session_id = _setup_session(client, superuser_token_headers)
    created = force_orphaned_agent_message(
        db, session_id, timestamp=_age(200), heartbeat_at=_age(199),
    )

    row = db.get(SessionMessage, uuid.UUID(created["id"]))
    metadata = dict(row.message_metadata)
    metadata["streaming_in_progress"] = False
    row.message_metadata = metadata
    row.status = "aborted"
    db.add(row)
    db.commit()

    assert RepairTick(db).run_orphaned_streams() == 0
    messages = _agent_messages(client, superuser_token_headers, session_id)
    assert messages[0]["status"] == "aborted"
    assert messages[0]["message_metadata"]["streaming_in_progress"] is False
