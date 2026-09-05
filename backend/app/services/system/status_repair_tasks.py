"""Pass C of the status-repair sweep — input tasks stranded at ``in_progress``.

An ``InputTask``'s execution status is **derived**, not owned: it follows the
state of the sessions the task spawned, through
``InputTaskService.compute_status_from_sessions``. What drives that derivation
is the stream lifecycle events (``handle_stream_completed`` and friends), and
those are exactly the events a dying process never emits. The result is a
two-level cascade — a wedged environment wedges its session, and a wedged
session strands its task at ``in_progress`` — with the task the only level a
user actually looks at on their task board.

**This pass adds no judgement of its own.** It re-runs the platform's existing
derivation against the session states Pass B has just repaired and writes
whatever that derivation now says. The age threshold decides only *which tasks
are worth re-deriving*; a task whose sessions really are still streaming
re-derives to ``in_progress`` and nothing is written. That is why registration
order matters: run before Pass B and this pass would faithfully re-derive from
the stale session rows and conclude nothing had changed.

**Why ``update_task_status`` and not ``sync_task_status_from_sessions``.** The
latter is the ordinary derived-sync entry point, but it writes through
``update_status``, which changes the column without recording anything. A
transition nobody can account for is the wrong thing to leave on a task that a
human is going to open and ask "why did this move?". ``update_task_status``
writes the immutable ``TaskStatusHistory`` row, posts the system comment that
puts the change in the activity feed, validates the transition, and emits
``TASK_STATUS_CHANGED`` — the same trail every other status change leaves. A
repair is a status change like any other and is recorded like one, with
``changed_by_system=True`` and a reason that names the repairer.

**Idempotence** comes for free from the derivation rather than from a claim
check: the only write happens when the derived status *differs* from
``in_progress``, and a task that has been repaired is no longer ``in_progress``,
so it is no longer a candidate.

**A task the derivation still calls ``in_progress`` is a no-op every tick, and
it is not free.** Re-deriving one task is a sessions query plus a per-session
unanswered-message query plus a subtask-progress read — N+1 per candidate, on
the sweep's pinned leader connection, every couple of minutes for as long as the
task legitimately runs. That is why the age predicate is in SQL and why a tick
takes a bounded number of candidates, oldest first: the sweep must cost the same
whether a deployment has three long-running tasks or three thousand.

**One exposure worth naming.** The derivation is the platform's own, and this
pass adds no judgement to it — but the *trigger* is new. A task a human set to
``in_progress`` by hand, whose sessions are ``active`` and idle, derives to
COMPLETED after 30 minutes: a status change on a human-owned row that no stream
event asked for. It is recorded like any other change (history row, system
comment, ``changed_by_system=True``, a reason naming the repairer), so it is
visible and explicable rather than silent. Changing the derivation's semantics
to distinguish "a session the platform is driving" from "a session sitting
there" is a larger question than this pass and is tracked separately — do not
quietly narrow it here.
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.config import settings
from app.models.tasks.input_task import InputTask, InputTaskStatus
from app.services.system.status_repair_context import RepairContext
from app.services.tasks.input_task_service import InputTaskService
from app.utils import as_utc

logger = logging.getLogger(__name__)

_REPAIR_REASON = "Re-derived from session state by status repair"

# How many stranded tasks one tick will re-derive. A CONSTANT, not a setting:
# it bounds the sweep's cost rather than tuning its behaviour. Each candidate
# costs a sessions query plus a per-session unanswered-message query plus a
# subtask read, all on the pinned leader connection every pass shares, and the
# candidate set is unbounded by nature — every legitimately long-running task in
# the deployment is in it, every tick, forever.
#
# Taking the oldest ``executed_at`` first is what makes a cap safe: the tasks
# most likely to be genuinely stranded are reconciled first, and anything past
# the cap is simply looked at on a later tick. Nothing is skipped permanently.
_MAX_CANDIDATES_PER_TICK = 200


class _Claim(NamedTuple):
    """One task's execution claim, as observed by the candidate query.

    ``executed_at`` is a *start* mark, not a heartbeat — it is written once, on
    the transition into ``in_progress``, and never moved — so it answers "how
    long has this been running" and nothing else. The claim's second half is
    ``updated_at``, which every write to the task bumps: it is what tells a row
    somebody has touched since we looked from one nobody has.
    """

    task_id: UUID
    status: str
    updated_at: datetime
    executed_at: datetime


def _claim_still_held(task: InputTask, claim: _Claim) -> bool:
    """True if the row is still sitting on the exact claim we verified."""
    if task.status != claim.status:
        return False
    if as_utc(task.updated_at) != claim.updated_at:
        logger.debug(
            "Status repair: task %s was touched while we looked — leaving it "
            "for the next tick",
            task.id,
        )
        return False
    return True


def _repair_task(session: Session, claim: _Claim, now: datetime) -> bool:
    """Re-derive one task's status and write it if it changed."""
    task = session.get(InputTask, claim.task_id)
    if task is None or not _claim_still_held(task, claim):
        session.rollback()
        return False

    # Also enforced by the candidate query's SQL predicate. Kept here because
    # this function is directly invocable — a test (or a future caller) handing
    # it a claim it built itself must get the same threshold, not a repair the
    # candidate query would never have offered.
    running_for = now - claim.executed_at
    if running_for < timedelta(minutes=settings.STATUS_REPAIR_TASK_MAX_AGE_MINUTES):
        session.rollback()
        return False

    derived = InputTaskService.compute_status_from_sessions(session, claim.task_id)
    if derived is None:
        # No sessions are connected to the task at all. There is nothing to
        # derive from, and inventing a terminal status for a task whose
        # execution left no trace would be a guess — precisely what the sweep's
        # "verify, then repair" rule forbids. Left for a human.
        logger.debug(
            "Status repair: task %s has been %r for %s but has no connected "
            "sessions to derive from — skipping",
            claim.task_id,
            claim.status,
            running_for,
        )
        session.rollback()
        return False

    if derived == claim.status:
        # The sessions still say this task is running. The threshold made it
        # worth asking; the answer is that nothing is wrong.
        session.rollback()
        return False

    # ``update_task_status`` owns the write: history row, system comment,
    # transition validation, commit and event. Every status reachable from
    # ``compute_status_from_sessions`` (completed / blocked / error /
    # in_progress) is a legal successor of ``in_progress``, so the validation
    # cannot reject a derivation — but it is left in place as the guard it is,
    # rather than bypassed because today's inputs happen to satisfy it.
    InputTaskService.update_task_status(
        db_session=session,
        task_id=claim.task_id,
        new_status=derived,
        changed_by_system=True,
        reason=_REPAIR_REASON,
    )
    logger.info(
        "Status repair: task %s %s -> %s after %s at %r (re-derived from its "
        "sessions)",
        claim.task_id,
        claim.status,
        derived,
        running_for,
        claim.status,
    )
    return True


async def repair_input_tasks(ctx: RepairContext) -> int:
    """Re-derive input tasks stranded at ``in_progress``.

    Registered as Pass C of the status-repair sweep, **after** Pass B: it reads
    the session states that pass has just repaired and committed, and running it
    earlier would re-derive from the stale ones. This is the sweep's one real
    hand-off — the other cross-pass dependencies run through ``RepairContext``
    precisely because the database does not yet show them.

    ``async`` to match the pass signature the scheduler registers, though the
    work itself is entirely synchronous database access — there is no probe to
    await here, because the evidence this pass verifies against is already in
    the database.

    Returns the number of tasks whose status changed; it is also the pass's test
    surface, since the ``TESTING`` gate keeps the scheduler from running under
    pytest — a test builds ``RepairContext(session=db)`` and awaits this
    directly.
    """
    session = ctx.session
    now = datetime.now(UTC)

    # The age predicate is in SQL, and the candidate set is capped. Without both
    # of these, every legitimately long-running task in the deployment is
    # re-derived — sessions query, per-session unanswered-message query, subtask
    # read — on every tick forever, on the shared leader connection.
    #
    # ``executed_at`` is a naive column and ``cutoff`` is tz-aware; Postgres
    # resolves that by casting the parameter with the connection's TimeZone
    # (UTC here), the same shape Pass D and the other sweeps in the codebase
    # use. Everything read *back* still goes through ``as_utc``, because Python
    # comparison raises on the mix rather than resolving it.
    cutoff = now - timedelta(minutes=settings.STATUS_REPAIR_TASK_MAX_AGE_MINUTES)
    claims = [
        _Claim(task_id, status, as_utc(updated_at), as_utc(executed_at))
        for task_id, status, updated_at, executed_at in session.exec(
            select(
                InputTask.id,
                InputTask.status,
                InputTask.updated_at,
                InputTask.executed_at,
            )
            .where(
                InputTask.status == InputTaskStatus.IN_PROGRESS,
                col(InputTask.executed_at).is_not(None),
                col(InputTask.executed_at) < cutoff,
            )
            .order_by(col(InputTask.executed_at))
            .limit(_MAX_CANDIDATES_PER_TICK)
        ).all()
        if executed_at is not None
    ]
    session.rollback()

    if not claims:
        return 0

    if len(claims) == _MAX_CANDIDATES_PER_TICK:
        # Debug rather than a warning: a deployment can legitimately sit at the
        # cap for hours, and a warning repeated every tick is a warning nobody
        # reads. The remainder is picked up by the following ticks.
        logger.debug(
            "Status repair: %d stranded task(s) is this tick's cap — the rest "
            "will be re-derived on the next tick",
            _MAX_CANDIDATES_PER_TICK,
        )

    repaired = 0
    for claim in claims:
        try:
            if _repair_task(session, claim, now):
                repaired += 1
        except Exception as exc:
            # Per-row isolation, and the rollback comes first: a Postgres error
            # aborts the whole transaction, which every later row and every
            # later pass shares.
            session.rollback()
            logger.error(
                "Status repair: failed to re-derive task %s: %s",
                claim.task_id,
                exc,
                exc_info=True,
            )
    return repaired
