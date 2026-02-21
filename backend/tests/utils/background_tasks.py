"""Shared background task collector for tests.

Provides BackgroundTaskCollector (replaces create_task_with_error_logging)
and drain_tasks() to run collected tasks from the test thread.
"""
import asyncio

_collector = None


class BackgroundTaskCollector:
    """Collects fire-and-forget asyncio tasks for deferred execution.

    Replaces create_task_with_error_logging so that background coroutines
    (e.g. process_pending_messages, auto_generate_session_title) are captured
    instead of scheduled on the event loop.  Test utilities call run_all()
    to drain them synchronously from the test thread.
    """

    def __init__(self):
        self.pending: list[tuple] = []

    def __call__(self, coro, task_name="background_task"):
        self.pending.append((coro, task_name))

    def run_all(self, max_rounds: int = 10):
        """Run all collected tasks synchronously, draining cascading tasks."""
        for _ in range(max_rounds):
            if not self.pending:
                return
            batch = list(self.pending)
            self.pending.clear()
            for coro, _name in batch:
                asyncio.run(coro)
        if self.pending:
            names = [name for _, name in self.pending]
            raise RuntimeError(
                f"run_all: still pending after {max_rounds} rounds: {names}"
            )

    def cleanup(self):
        """Close any unrun coroutines to prevent RuntimeWarning."""
        for coro, _name in self.pending:
            coro.close()
        self.pending.clear()


def set_collector(collector):
    global _collector
    _collector = collector


def drain_tasks():
    """Drain all collected background tasks synchronously.

    Must be called from the test thread (not from inside the ASGI event loop).
    Handles cascading tasks (tasks spawned during execution).
    """
    if not _collector:
        return
    _collector.run_all()
