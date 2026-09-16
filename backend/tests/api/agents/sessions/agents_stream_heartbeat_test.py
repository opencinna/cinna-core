"""
Integration tests for the per-turn stream heartbeat against real rows
(plan: a2a_crash_recovery_plan.md D10).

The ticker is off under pytest (the API harness shares one DB session across
threads), so these tests drive the real ``StreamHeartbeat`` through the seam in
``tests/utils/session.py``: ``to_thread`` runs inline, so every write goes
through the real SQL (row lock, JSON rewrite) serially on the test session.
The orphan pass is driven through ``RepairTick``, as in
``agents_orphaned_stream_repair_test.py``. Rows and stale heartbeats are
forged with the same seams that file uses.

Pure-logic coverage (cancel handling, no clobbering) lives in
``tests/unit/test_stream_heartbeat.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role
from tests.utils.session import (
    beat_stream_heartbeat,
    create_session_via_api,
    force_orphaned_agent_message,
    force_session_interaction_claim,
    get_session,
    make_stream_heartbeat,
    run_stream_heartbeat_ticker,
    stop_stream_heartbeat,
)
from tests.utils.status_repair import RepairTick


def _setup_session(client: TestClient, headers: dict[str, str]) -> str:
    agent = create_agent_via_api(client, headers, name="Heartbeat Agent")
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None
    return create_session_via_api(client, headers, agent["id"])["id"]


def _age(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_ticker_stamps_session_and_agent_row_and_clears_session_stamp_at_end(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    session_id = _setup_session(client, superuser_token_headers)
    created = force_orphaned_agent_message(
        db, session_id, timestamp=_age(10), heartbeat_at=_age(9),
    )
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
        stream_heartbeat_at=_age(9),
        set_stream_heartbeat=True,
    )
    before = get_session(client, superuser_token_headers, session_id)
    assert "stream_heartbeat_at" in (before.get("session_metadata") or {})

    heartbeat = make_stream_heartbeat(db, session_id, message_id=created["id"])
    seen = run_stream_heartbeat_ticker(heartbeat, beats=3)

    assert len(seen) >= 3 and all(seen)
    assert _parse(seen[-1]) > _parse(seen[0]), "each beat moves the stamp"

    message = get_messages_by_role(
        client, superuser_token_headers, session_id, role="agent",
    )[0]
    stamp = _parse(message["message_metadata"]["stream_heartbeat_at"])
    assert stamp > _age(1), "the agent row carries a fresh beat"
    assert message["message_metadata"]["streaming_in_progress"] is True

    # The fresh beat protects the turn from the orphan pass...
    assert RepairTick(db).run_orphaned_streams() == 0
    # ...and the normal turn end removed the session-side stamp.
    session = get_session(client, superuser_token_headers, session_id)
    assert "stream_heartbeat_at" not in (session.get("session_metadata") or {})
    assert session["interaction_status"] == "running"


def test_beat_reopens_a_row_the_orphan_pass_sealed(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    session_id = _setup_session(client, superuser_token_headers)
    created = force_orphaned_agent_message(
        db, session_id, timestamp=_age(10), heartbeat_at=_age(5),
    )
    assert RepairTick(db).run_orphaned_streams() == 1
    sealed = get_messages_by_role(client, superuser_token_headers, session_id, role="agent")[0]
    assert sealed["status"] == "aborted"

    # The turn was only stalled: its next beat undoes the seal.
    heartbeat = make_stream_heartbeat(
        db, session_id, message_id=created["id"], started_at=_age(10),
    )
    beat_stream_heartbeat(heartbeat)

    reopened = get_messages_by_role(client, superuser_token_headers, session_id, role="agent")[0]
    assert reopened["status"] == ""
    assert reopened["message_metadata"]["streaming_in_progress"] is True
    assert _parse(reopened["message_metadata"]["stream_heartbeat_at"]) > _age(1)


def test_beat_restores_running_on_a_session_the_orphan_pass_cleared(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
        stream_heartbeat_at=_age(4),
        set_stream_heartbeat=True,
    )
    assert RepairTick(db).run_orphaned_streams() == 1
    assert get_session(client, superuser_token_headers, session_id)["interaction_status"] == ""

    heartbeat = make_stream_heartbeat(db, session_id, started_at=_age(10))
    beat_stream_heartbeat(heartbeat)

    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == "running"
    # The original start comes back, so the hard-cap clock is not reset.
    restored = _parse(session["streaming_started_at"])
    if restored.tzinfo is None:
        restored = restored.replace(tzinfo=UTC)
    assert restored < _age(9)


def test_beat_of_a_later_turn_does_not_restore_an_earlier_clear(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
        stream_heartbeat_at=_age(4),
        set_stream_heartbeat=True,
    )
    assert RepairTick(db).run_orphaned_streams() == 1

    # A new turn that started after the judged heartbeat.
    heartbeat = make_stream_heartbeat(db, session_id)
    beat_stream_heartbeat(heartbeat)

    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == ""


def test_pass_b_reaps_a_beating_turn_past_the_hard_cap(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """A stream hung on a connected environment keeps beating; Pass B still
    clears it past ``STATUS_REPAIR_STREAM_HARD_MAX_AGE_HOURS``.
    """
    from app.core.config import settings

    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(settings.STATUS_REPAIR_STREAM_HARD_MAX_AGE_HOURS * 60 + 5),
        stream_heartbeat_at=_age(0.2),
        set_stream_heartbeat=True,
    )

    assert RepairTick(db).run_sessions() == 1
    assert get_session(client, superuser_token_headers, session_id)["interaction_status"] == ""


def test_turn_end_does_not_delete_another_workers_session_stamp(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    """Overlapping turns on two workers: turn A ends while turn B's beat owns
    the session stamp. A's stop() must leave B's stamp, or Pass B could reap B.
    """
    session_id = _setup_session(client, superuser_token_headers)
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
    )
    turn_a = make_stream_heartbeat(db, session_id)
    beat_stream_heartbeat(turn_a)

    foreign = _age(-0.1)  # turn B's newer beat
    force_session_interaction_claim(
        db, session_id,
        interaction_status="running",
        streaming_started_at=_age(10),
        stream_heartbeat_at=foreign,
        set_stream_heartbeat=True,
    )
    stop_stream_heartbeat(turn_a)

    session = get_session(client, superuser_token_headers, session_id)
    assert _parse(session["session_metadata"]["stream_heartbeat_at"]) == foreign


def test_turn_end_deletes_its_own_session_stamp(
    client: TestClient, superuser_token_headers: dict, db,
) -> None:
    session_id = _setup_session(client, superuser_token_headers)
    turn = make_stream_heartbeat(db, session_id)
    beat_stream_heartbeat(turn)
    assert "stream_heartbeat_at" in get_session(
        client, superuser_token_headers, session_id,
    )["session_metadata"]

    stop_stream_heartbeat(turn)

    session = get_session(client, superuser_token_headers, session_id)
    assert "stream_heartbeat_at" not in (session.get("session_metadata") or {})
