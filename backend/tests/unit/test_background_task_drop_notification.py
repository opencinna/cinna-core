"""``create_task_with_error_logging`` must be able to tell a caller it gave up.

WHY THIS IS ITS OWN FILE
------------------------
The helper's cross-thread branch has always ended in a ``logger.warning`` and
``coro.close()``: the work simply does not happen, and nothing but a log line
records that. For most of its eleven callers that is a fine best-effort
contract. For one it is not — ``KeyProvisioningService.schedule_revocations``
hands it batches in which *every entry is a key that is live at a provider*, and
``admin.ai_credential.revoke_failed`` is documented as the durable record of
one. A dropped batch there is an undestroyed key that nothing anywhere names.

So ``on_drop`` exists, and its absence stays the historical behaviour: omitting
it means "a drop is tolerable here", which is a statement a caller makes rather
than a default nobody chose.

Tested at the helper rather than through the service because the drop branch is
only reachable with no running event loop *and* a failing anyio portal, which no
route can produce and the background-task collector deliberately never
simulates.
"""
from __future__ import annotations

import asyncio

from app.utils import create_task_with_error_logging


async def _never_runs() -> None:  # pragma: no cover - the point is it does not
    raise AssertionError("the dropped coroutine must not run")


def test_a_dropped_coroutine_notifies_its_caller(monkeypatch) -> None:
    """No running loop and no way to reach one: the caller is told."""
    dropped: list[str] = []

    def _explode(*_args, **_kwargs):
        raise RuntimeError("no portal available")

    monkeypatch.setattr("anyio.from_thread.run", _explode)

    result = create_task_with_error_logging(
        _never_runs(), "test_task", on_drop=lambda: dropped.append("told")
    )

    assert result is None
    assert dropped == ["told"], (
        "the coroutine was closed without running, so the caller must have been "
        "notified — this is the silent-drop path the parameter exists for"
    )


def test_omitting_the_callback_keeps_the_historical_silent_drop(monkeypatch) -> None:
    """Absence is representable: no callback, no notification, no exception."""

    def _explode(*_args, **_kwargs):
        raise RuntimeError("no portal available")

    monkeypatch.setattr("anyio.from_thread.run", _explode)

    assert create_task_with_error_logging(_never_runs(), "test_task") is None


def test_a_scheduled_coroutine_never_notifies(monkeypatch) -> None:
    """``on_drop`` must fire only on a drop, not on every cross-thread schedule.

    The distinction matters: a caller that recorded "this key was not revoked"
    every time the work was successfully handed to the loop would fill the audit
    trail with revocations that did happen.
    """
    dropped: list[str] = []
    scheduled: list[str] = []

    def _run(func, *args, **kwargs):
        scheduled.append("scheduled")
        # Mirrors anyio's contract closely enough for this branch: the callable
        # is a coroutine function that creates the task on the main loop.
        return None

    monkeypatch.setattr("anyio.from_thread.run", _run)

    coro = _never_runs()
    try:
        create_task_with_error_logging(
            coro, "test_task", on_drop=lambda: dropped.append("told")
        )
    finally:
        coro.close()

    assert scheduled == ["scheduled"]
    assert dropped == []


def test_inside_a_running_loop_nothing_is_dropped() -> None:
    """The ordinary path: a real task is returned and the callback never fires.

    Driven through ``asyncio.run`` rather than an ``async def`` test: this suite
    has no async plugin installed, so an async test function is *skipped* with a
    warning — green, and asserting nothing.
    """
    dropped: list[str] = []
    ran: list[str] = []

    async def _work() -> None:
        ran.append("ran")

    async def _drive() -> None:
        task = create_task_with_error_logging(
            _work(), "test_task", on_drop=lambda: dropped.append("told")
        )
        assert isinstance(task, asyncio.Task)
        await task

    asyncio.run(_drive())
    assert ran == ["ran"]
    assert dropped == []
