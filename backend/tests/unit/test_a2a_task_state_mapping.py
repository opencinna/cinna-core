"""
Unit tests for ``A2AEventMapper.map_session_status_to_task_state`` (plan
``a2a_crash_recovery_plan.md`` §2.2) — the C2 decision table that backs
``tasks/get`` and the ``message/send`` poll.

Rules are evaluated in order and the first match wins; this file tests both
each rule in isolation and precedence between rules that could otherwise
both match the same call.

Pure logic — no DB, no client.
"""
from __future__ import annotations

import pytest
from a2a.types import TaskState

from app.services.a2a.a2a_event_mapper import A2AEventMapper

map_session_status_to_task_state = A2AEventMapper.map_session_status_to_task_state


# ---------------------------------------------------------------------------
# One case per rule, in isolation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs, expected, reason",
    [
        # Rule 1: unanswered tool question wins over everything else.
        (
            dict(status="active", interaction_status="", tool_questions_status="unanswered"),
            TaskState.input_required,
            "rule 1: unanswered tool question",
        ),
        # Rule 2: interaction_status == running.
        (
            dict(status="active", interaction_status="running"),
            TaskState.working,
            "rule 2: interaction_status running",
        ),
        # Rule 3: interaction_status == pending_stream.
        (
            dict(status="active", interaction_status="pending_stream"),
            TaskState.submitted,
            "rule 3: interaction_status pending_stream",
        ),
        # Rule 4: turn_in_flight (process-local lock/stream) with an
        # otherwise idle session.
        (
            dict(status="active", interaction_status="", turn_in_flight=True),
            TaskState.working,
            "rule 4: turn_in_flight",
        ),
        # Rule 4: has_pending (undelivered user message) with an otherwise
        # idle session.
        (
            dict(status="active", interaction_status="", has_pending=True),
            TaskState.working,
            "rule 4: has_pending",
        ),
        # Rule 5: session status == error.
        (
            dict(status="error", interaction_status=""),
            TaskState.failed,
            "rule 5: status error",
        ),
        # Rule 6: last agent message was user-interrupted.
        (
            dict(status="active", interaction_status="", last_agent_status="user_interrupted"),
            TaskState.canceled,
            "rule 6: last_agent_status user_interrupted",
        ),
        # Rule 6: last agent message was aborted (crash-recovery finalize).
        (
            dict(status="active", interaction_status="", last_agent_status="aborted"),
            TaskState.failed,
            "rule 6: last_agent_status aborted",
        ),
        # Rule 7: plain completed session.
        (
            dict(status="completed", interaction_status=""),
            TaskState.completed,
            "rule 7: status completed",
        ),
        # Rule 7: plain active/idle session.
        (
            dict(status="active", interaction_status=""),
            TaskState.completed,
            "rule 7: status active, idle",
        ),
        # Rule 8: unknown status falls back to the conservative answer.
        (
            dict(status="some_future_status", interaction_status=""),
            TaskState.working,
            "rule 8: unknown status",
        ),
    ],
)
def test_map_session_status_to_task_state_rules(kwargs, expected, reason) -> None:
    assert map_session_status_to_task_state(**kwargs) == expected, reason


# ---------------------------------------------------------------------------
# Precedence between rules that could otherwise both match
# ---------------------------------------------------------------------------

def test_unanswered_question_wins_over_turn_in_flight() -> None:
    """Rule 1 fires even when rule 4's conditions also hold."""
    assert (
        map_session_status_to_task_state(
            status="active",
            interaction_status="running",
            tool_questions_status="unanswered",
            turn_in_flight=True,
            has_pending=True,
        )
        == TaskState.input_required
    )


def test_running_interaction_status_wins_over_error_status() -> None:
    """Rule 2 fires before rule 5 gets a chance to look at ``status``."""
    assert (
        map_session_status_to_task_state(
            status="error",
            interaction_status="running",
        )
        == TaskState.working
    )


def test_turn_in_flight_wins_over_error_status() -> None:
    """Rule 4 fires before rule 5 — a turn currently running masks a stale
    ``error`` status left over from a previous turn.
    """
    assert (
        map_session_status_to_task_state(
            status="error",
            interaction_status="",
            turn_in_flight=True,
        )
        == TaskState.working
    )


def test_has_pending_wins_over_last_agent_status() -> None:
    """Rule 4 fires before rule 6 — a new pending message outranks the
    previous turn's terminal agent-message status.
    """
    assert (
        map_session_status_to_task_state(
            status="active",
            interaction_status="",
            has_pending=True,
            last_agent_status="aborted",
        )
        == TaskState.working
    )


def test_error_status_wins_over_last_agent_status() -> None:
    """Rule 5 fires before rule 6."""
    assert (
        map_session_status_to_task_state(
            status="error",
            interaction_status="",
            last_agent_status="user_interrupted",
        )
        == TaskState.failed
    )


def test_last_agent_status_wins_over_plain_completed_fallback() -> None:
    """Rule 6 fires before rule 7 — a completed *session* status does not
    paper over the last turn having been aborted.
    """
    assert (
        map_session_status_to_task_state(
            status="completed",
            interaction_status="",
            last_agent_status="aborted",
        )
        == TaskState.failed
    )
