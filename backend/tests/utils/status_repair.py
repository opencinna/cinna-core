"""Drive the status-repair sweep's pass functions directly, for tests.

EXEMPTION from Rule 1 (``backend/tests/README.md`` — no ``app.services``
imports in ``tests/api/``), and a deliberate, structural one. The scheduler
that would normally run these passes never ticks under pytest — it is
gated behind ``settings.TESTING`` exactly like the other 17 in this codebase —
and every pass module's own docstring says what that leaves behind: "a test
builds ``RepairContext(session=db)`` and awaits this directly." There is no
HTTP surface for a sweep tick at all, so importing the pass functions is not
a shortcut around the API — it is the only test surface this feature has.
This module is where that necessary import lives, the same way
``tests/utils/session.py``'s active-streaming-manager helpers and
``tests/utils/server_channel.py``'s ``flush_pending_bindings`` isolate their
own ``app.services`` imports so individual test files stay free of them.

``RepairTick`` wraps one ``RepairContext`` so a test can run several passes in
the sweep's own order within the SAME tick — exactly what
``status_repair_scheduler.run_repair_passes`` does — and inspect what one pass
left in the context for the next to see. That cross-pass state
(``drained_environment_ids`` / ``sessions_in_motion``) is the whole reason the
three priority regressions in
``docs/plans/system_status_repair_plan.md`` were bugs in the first place, so
a test asserting on them needs a shared context across pass calls, not a
fresh one per pass.
"""
import asyncio
import uuid


class RepairTick:
    """One sweep tick's shared ``RepairContext``, one pass call at a time.

    Each ``run_*`` method awaits the named pass against the SAME context (and
    therefore the same ``db`` session/transaction the test itself uses) and
    returns the number of rows that pass repaired — the pass's own return
    value, unchanged. Passes may be called in any order and any number of
    times; the sweep's own registration order (environments, sessions,
    input_tasks, channel_deliveries) is a test's responsibility to honor when
    it wants the real hand-off behavior, not something this wrapper enforces,
    since some tests deliberately want to run a pass alone.
    """

    def __init__(self, db):
        from app.services.system.status_repair_context import RepairContext

        self._ctx = RepairContext(session=db)

    def run_environments(self) -> int:
        from app.services.system.status_repair_environments import (
            repair_environments,
        )

        return asyncio.run(repair_environments(self._ctx))

    def run_sessions(self) -> int:
        from app.services.system.status_repair_sessions import repair_sessions

        return asyncio.run(repair_sessions(self._ctx))

    def run_input_tasks(self) -> int:
        from app.services.system.status_repair_tasks import repair_input_tasks

        return asyncio.run(repair_input_tasks(self._ctx))

    def run_channel_deliveries(self) -> int:
        from app.services.system.status_repair_channels import (
            repair_channel_deliveries,
        )

        return asyncio.run(repair_channel_deliveries(self._ctx))

    def is_environment_draining(self, env_id: str | uuid.UUID) -> bool:
        """True if a Pass A call on this tick repaired this env to running."""
        if isinstance(env_id, str):
            env_id = uuid.UUID(env_id)
        return self._ctx.is_environment_draining(env_id)

    def is_session_in_motion(self, session_id: str | uuid.UUID) -> bool:
        """True if a Pass B call on this tick touched this session."""
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)
        return self._ctx.is_session_in_motion(session_id)


def set_environment_probe(env_id: str | uuid.UUID, status: str):
    """Route ONE environment's adapter lookups to a controllable stub.

    EXEMPTION, same posture as the module docstring. Pass A's repair decision
    hinges on ``adapter.get_status()`` ("running" → repair to running,
    "stopped" → repair to error, anything else → inconclusive, leave it), and
    the environment-adapter test fixture
    (``tests/utils/fixtures.py::setup_environment_adapter``) hands back a
    BRAND NEW ``EnvironmentTestAdapter`` (default status "stopped") on every
    ``get_adapter()`` call unless the domain's conftest opted into
    ``persistent_adapter=True`` — which the domains that also patch
    ``CREATE_SESSION_TARGETS_AGENT`` / ``BACKGROUND_TASK_TARGETS_FULL`` (needed
    for Pass B/C) do not. Rather than requiring a conftest combination that
    does not exist anywhere in the suite, this monkeypatches the ALREADY
    installed lifecycle manager's ``get_adapter`` for one environment id only,
    delegating every other environment to whatever ``get_adapter`` was doing
    before — so a test can put one specific environment's "container" in a
    chosen state (``"running"``, ``"stopped"``, or any other string to
    exercise the inconclusive-probe path) without disturbing every other
    environment created in the same test.

    Returns the ``EnvironmentTestAdapter`` instance so a test can also inspect
    call history (``.start_calls`` etc.) if it needs to.
    """
    from app.services.environments.environment_service import EnvironmentService
    from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter

    if isinstance(env_id, str):
        env_id = uuid.UUID(env_id)

    lm = EnvironmentService.get_lifecycle_manager()
    adapter = EnvironmentTestAdapter()
    adapter._status = status
    original_get_adapter = lm.get_adapter

    def _dispatch(environment, _original=original_get_adapter, _env_id=env_id, _adapter=adapter):
        if environment.id == _env_id:
            return _adapter
        return _original(environment)

    lm.get_adapter = _dispatch
    return adapter
