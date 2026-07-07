"""
Unit test: ``MessageService.process_pending_messages`` serializes two
same-session calls on ONE real event loop via the shared
``get_session_lock`` singleton.

Why this test lives in ``tests/unit/`` and not ``tests/api/``:

The API-only harness (see backend/tests/README.md "Testing
Session-Driven Flows") drains background tasks sequentially, each in
its OWN ``asyncio.run()`` loop (``tests/utils/background_tasks.py``).
Two same-session ``process_pending_messages`` calls therefore never
actually overlap in that harness, no matter what the lock does --
there is no way to prove genuine serialization (as opposed to mere
sequential execution) through the API layer. Proving the fix requires
running two real coroutines concurrently on a single event loop via
``asyncio.gather``/``asyncio.ensure_future`` and controlling their
timing with an ``asyncio.Event`` -- exactly the pattern this file uses.

This test also cannot be a pure "no I/O" unit test in the strictest
sense: ``process_pending_messages`` needs a DB-session-shaped object
and a ``SessionStreamProcessor``. Rather than touching a real Postgres
connection (which ``tests/unit/`` explicitly forbids -- see
``tests/unit/conftest.py``'s no-op ``setup_db`` override), everything
around the lock is replaced with lightweight fakes/mocks:

- ``get_fresh_db_session`` is a fake context manager whose ``db.get()``
  returns a truthy stand-in "session" object (never touched, because...)
- ``MessageService.collect_pending_messages`` is mocked to always report
  "there is pending work", so the quick-check inside the lock always
  proceeds to construct a processor instead of short-circuiting.
- ``SessionStreamProcessor.process`` is mocked to control exactly when
  each call "finishes its stream" (call #1 blocks on an
  ``asyncio.Event`` until released; call #2 returns immediately).
- ``SessionService.clear_interaction_status`` (the ``finally`` teardown)
  is mocked to just record when it ran, instead of writing to a real
  session row.

The ONE thing that is real and NOT mocked is ``get_session_lock`` from
``app.services.sessions.stream_processor`` -- the exact same singleton
the production code acquires. That is what actually causes the
serialization observed below; nothing in this test hand-waves the lock
behavior itself.

API-observable behavior of the fixed pipeline (message never stranded,
correct final session state) is covered end-to-end in
``tests/api/agents/agents_session_stream_concurrency_test.py``.

This is the test that would FAIL before the fix (both calls' ``process()``
mocks would run essentially back-to-back with no lock forcing call #2 to
wait for call #1's teardown) and PASSES after it.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from app.services.sessions.message_service import MessageService
from app.services.sessions.session_service import SessionService
from app.services.sessions.stream_processor import SessionStreamProcessor


def test_process_pending_messages_serializes_same_session_calls() -> None:
    """
    Launch two ``process_pending_messages(session_id, ...)`` coroutines for
    the SAME session on one event loop. Call #1's (mocked) stream blocks on
    an ``asyncio.Event``, simulating the long-running turn from the bug
    report. Assert:

      1. While call #1 holds the lock and is blocked mid-stream, call #2
         has NOT started its own stream -- it is parked waiting for the
         lock, not running concurrently.
      2. Call #1's ``finally`` teardown (the ``interaction_status`` clear)
         runs BEFORE call #2's stream starts -- proving the lock wraps the
         teardown too, not just ``process()`` (the exact bug: if teardown
         ran outside the lock, it could race call #2's freshly-set
         "running" state and falsely clear it while call #2 still worked).
      3. Both calls eventually complete, each running its own teardown.
    """
    session_id = uuid.uuid4()

    @contextmanager
    def fake_db_session():
        # `.get(Model, id)` just needs to return something truthy; the
        # session-lock quick-check never inspects/mutates it because
        # collect_pending_messages is mocked to report "pending work
        # exists", which skips the assignment branch entirely.
        yield SimpleNamespace(get=lambda model, sid: SimpleNamespace())

    unblock_first_stream = asyncio.Event()
    events_log: list[str] = []
    call_count = {"n": 0}

    async def fake_process(self) -> str:  # noqa: ANN001 - mirrors SessionStreamProcessor.process
        call_index = call_count["n"]
        call_count["n"] += 1
        if call_index == 0:
            events_log.append("call1_process_start")
            await unblock_first_stream.wait()
            events_log.append("call1_process_end")
        else:
            events_log.append("call2_process_start")
        return ""

    async def fake_clear_interaction_status(_session_id, reason: str = "") -> None:
        # Tag the teardown by how many process() calls have started so
        # far: exactly 1 while call #1 is tearing down (call #2 has not
        # been able to start yet), 2 once call #2 has started/finished.
        events_log.append("call1_teardown" if call_count["n"] == 1 else "call2_teardown")

    with (
        patch.object(
            MessageService,
            "collect_pending_messages",
            return_value=("pending content", [SimpleNamespace()]),
        ),
        patch.object(SessionStreamProcessor, "process", fake_process),
        patch.object(SessionService, "clear_interaction_status", fake_clear_interaction_status),
    ):

        async def run() -> None:
            task1 = asyncio.ensure_future(
                MessageService.process_pending_messages(session_id, fake_db_session)
            )
            task2 = asyncio.ensure_future(
                MessageService.process_pending_messages(session_id, fake_db_session)
            )

            # Give both tasks a chance to run up to their respective
            # blocking points (call #1 inside its mocked stream, call #2
            # parked on the lock -- acquiring an already-held asyncio.Lock
            # suspends without any explicit signal, so a handful of
            # scheduler turnovers is enough to prove it never got further).
            for _ in range(10):
                await asyncio.sleep(0)

            # INVARIANT (1): call #2 must still be blocked on the lock --
            # it must NOT have entered its stream while call #1 is
            # mid-flight. This is the core regression: pre-fix, the UI
            # path built its processor with no lock at all, so both calls
            # would reach `process()` independently.
            assert events_log == ["call1_process_start"], (
                f"call #2 must not start while call #1 is still streaming "
                f"and holding the session lock, got: {events_log}"
            )

            # Release call #1's stream. Its `finally` teardown must run
            # and complete BEFORE call #2's `async with lock` unblocks.
            unblock_first_stream.set()
            await asyncio.gather(task1, task2)

        asyncio.run(run())

    # INVARIANT (2): full serialization order, teardown included. If the
    # lock only wrapped `process()` (as it did for the pre-fix `processor`
    # internal lock, unused by the UI path), call #2 could start before
    # call #1's teardown clears interaction_status -- exactly the false
    # "done" bug. Wrapping the whole body forces this exact order.
    assert events_log == [
        "call1_process_start",
        "call1_process_end",
        "call1_teardown",
        "call2_process_start",
        "call2_teardown",
    ], f"lock did not fully serialize the two calls (incl. teardown): {events_log}"
