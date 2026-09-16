"""
Unit test for the status-repair scheduler's pass order (plan:
a2a_crash_recovery_plan.md §3.4, §6 T3): ``orphaned_streams`` must be
registered between ``sessions`` and ``input_tasks`` — after B so its "self
verdict, no local turn" check has the freshest local state, and before C so
the input-task pass re-derives status from the sessions this pass (not just
Pass B) has cleared.

Pure structural inspection of ``REPAIR_PASSES`` — no DB, no client.
"""
from __future__ import annotations

from app.services.system.status_repair_scheduler import REPAIR_PASSES


def test_orphaned_streams_is_registered_between_sessions_and_input_tasks() -> None:
    names = [name for name, _fn in REPAIR_PASSES]

    assert "orphaned_streams" in names
    assert "sessions" in names
    assert "input_tasks" in names

    sessions_idx = names.index("sessions")
    orphaned_idx = names.index("orphaned_streams")
    input_tasks_idx = names.index("input_tasks")

    assert sessions_idx < orphaned_idx < input_tasks_idx, (
        f"expected sessions < orphaned_streams < input_tasks, got order {names}"
    )
