"""Pass B of the status-repair sweep — sessions wedged mid-interaction.

``Session.interaction_status`` is the column the chat UI reads to decide
whether it is showing a live turn. Two of its three values are transitional and
are cleared by something that can die:

* ``running`` — a stream is in flight. It is cleared by the terminal
  ``STREAM_COMPLETED`` / ``STREAM_INTERRUPTED`` / ``STREAM_ERROR`` handling. A
  process that goes away mid-stream emits none of them, and the session shows
  "streaming" forever.
* ``pending_stream`` — messages are parked, waiting for the environment to come
  up. It is cleared by ``SessionService.handle_environment_activated``, which
  fires off an ``ENVIRONMENT_ACTIVATED`` event and **nothing else**: there is no
  poller behind it. If that event never arrives — the activation task died, or
  the environment was reconciled to ``error`` — the session waits forever with
  the user's message undelivered.

**What Pass A leaves behind, and why the claim check does not catch it.** Pass A
emits ``ENVIRONMENT_ACTIVATED`` for every environment it repairs to ``running``,
which drains that environment's ``pending_stream`` sessions through the ordinary
path. It is tempting to conclude that a drained session has therefore already
moved by the time this pass looks. **It has not.** The drain is asynchronous on
two independent counts: ``event_service._call_backend_handlers`` dispatches
handlers through ``create_task_with_error_logging``, so ``emit_event`` returns
before ``handle_environment_activated`` has run at all; and when it does run,
neither it nor ``initiate_stream``'s env-running branch writes the session row —
that only happens when the ``STREAM_STARTED`` handler lands, several task hops
and a credential-refresh round-trip later. Milliseconds after Pass A's emit the
claim token is still *exactly* the one this pass would have decided against, so
the claim check passes and the row looks abandoned.

The fix is not a bigger claim check but a smaller one: Pass A records the
environments it drained in the tick's ``RepairContext``, and this pass skips
their sessions outright. See ``status_repair_context``.

**The claim token.** Status alone is not a claim, for the same reason it was not
one in Pass A: a session legitimately re-enters ``running`` on the very next
turn, and a status-only check would let a repair decided against turn N be
written over turn N+1. Each transitional value is therefore paired with the
timestamp that moves when the claim is re-asserted:

* ``running``        → ``streaming_started_at``. Purpose-built for this: set at
  the top of every stream and nulled at every clear site, so it is *this*
  stream's start, never a previous one's.
* ``pending_stream`` → ``updated_at``, which every write through
  ``update_interaction_status`` bumps.

**``updated_at`` is a claim token here, not a modification time.** Two writes in
this module treat it from opposite directions and both are deliberate:
``_stamp_resend_claim`` bumps it *without* a semantic change, to suppress
re-entry, and ``_recount_pending`` corrects a derived column *without* bumping
it, so a repaired row stays visible to the next tick. Read every write to this
column as "am I claiming, or releasing, this row" — never as "did the session
change".

**Auto-resend is bounded on purpose, and the bound is on the message.**
Re-entering the drain for a session whose environment is up is the same delivery
``ENVIRONMENT_ACTIVATED`` would have made, a few minutes late — that is the
settled decision (plan §5.1). Doing it for an hours-old message is a different
act: surprise-executing a stale instruction against an agent is worse than a
visibly stuck message the user can see and re-send. The window is therefore
measured from the **oldest pending message's own timestamp**, which is the
"how stale is this instruction" clock §5.1 is actually about — not from the
claim stamp, which this pass moves itself and could keep inside its own window
forever. Past the window, and after one resend that did not take, the session is
cleared to idle with its messages left ``pending`` — visible in the UI and
recoverable through ``/session-recover``.
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from sqlmodel import Session, col, func, select

from app.core.config import settings
from app.core.db import create_session
from app.models.environments.environment import AgentEnvironment
from app.models.sessions.session import Session as ChatSession, SessionMessage
from app.services.sessions.active_streaming_manager import active_streaming_manager
from app.services.sessions.message_service import MessageService
from app.services.sessions.session_service import SessionService
from app.services.sessions.stream_heartbeat import (
    ORPHAN_CLEARED_KEY,
    STREAM_HEARTBEAT_KEY,
    build_orphan_cleared_marker,
    heartbeat_now,
    parse_heartbeat,
    set_turn_aborted_marker,
)
from app.services.sessions.stream_processor import is_session_lock_held
from app.services.system.status_repair_context import RepairContext
from app.utils import as_utc, create_task_with_error_logging

logger = logging.getLogger(__name__)

# The two transitional values of ``interaction_status``. ``""`` is the resting
# state and is never repaired.
_RUNNING = "running"
_PENDING_STREAM = "pending_stream"
_TRANSITIONAL_INTERACTION_STATUSES = (_RUNNING, _PENDING_STREAM)

# How far past its own threshold the OLDEST PENDING MESSAGE may be before the
# drain is no longer re-entered and the row is cleared to idle instead.
#
# A CONSTANT rather than a setting, because it is not a tuning knob: it encodes
# the approved rule ("a message may be delivered a few minutes late, never
# hours late", plan §5.1) as a bound relative to whatever the threshold is set
# to, so raising the threshold cannot silently widen the resend window into
# hours. At the default 15 minutes this admits resends up to 30 minutes late,
# which is the range a leader that was itself down for a tick or two lands in;
# anything past it is a backend that was gone long enough for the user to have
# noticed and moved on.
#
# **Which clock it measures is the whole point.** Applied to the claim stamp it
# would not converge at all: ``_stamp_resend_claim`` bumps ``updated_at``, which
# would also be the basis of the window, so a stamped row is perpetually "15
# minutes old" — always inside ``[threshold, 2x threshold)``, never falling out
# to the clear — and the resend repeats every tick forever. That is reachable
# whenever ``initiate_stream`` returns without writing the status: no pending
# messages, an unresolvable session/agent/environment, or the install-readiness
# gate short-circuit. Measured against the message it is a real deadline.
_RESEND_MAX_AGE_FACTOR = 2

# ``session_metadata`` key recording which stuck episode this pass has already
# re-entered the drain for, as the ISO timestamp of the oldest pending message
# at the time. It is what caps re-entry at ONE resend per row per episode
# instead of leaving that to the window alone: the window says "these
# instructions are not too stale to deliver", this says "and we have not already
# tried". Keyed by the message timestamp rather than a bare flag so it scopes
# itself — a genuinely new episode has a newer oldest-pending message, does not
# match, and gets its own single resend without anyone having to clear the mark.
#
# ``session_metadata`` rather than a column: it is already the platform's
# session-scoped flag bag (``recovery_pending``, ``webapp_actions_context_sent``,
# ``hidden_for_callers``), and a repair-bookkeeping column is not worth a
# migration.
_RESENT_EPISODE_KEY = "status_repair_resent_for"

_REPAIR_REASON = "reconciled by status repair"


class _Claim(NamedTuple):
    """One session's interaction claim, as observed by the candidate query.

    ``(interaction_status, stamped_at)`` together are the claim *token*, and the
    pair is what every write re-checks. See the module docstring for which
    column ``stamped_at`` comes from and why it differs per status.
    """

    session_id: UUID
    interaction_status: str
    stamped_at: datetime


def _max_quiet_time(interaction_status: str) -> timedelta:
    """How long a session may sit in ``interaction_status`` before we look."""
    minutes = (
        settings.STATUS_REPAIR_STREAM_MAX_AGE_MINUTES
        if interaction_status == _RUNNING
        else settings.STATUS_REPAIR_PENDING_STREAM_MAX_AGE_MINUTES
    )
    return timedelta(minutes=minutes)


def _stamp_of(chat_session: ChatSession, interaction_status: str) -> datetime | None:
    """The claim timestamp for ``interaction_status`` on this row.

    ``None`` for a ``running`` session with no ``streaming_started_at``: the
    column is nulled at every clear site, so a missing value means the stream
    never recorded a start and there is no honest age to measure. Such a row is
    left alone rather than reaped on a guess.
    """
    if interaction_status == _RUNNING:
        started = chat_session.streaming_started_at
        return as_utc(started) if started is not None else None
    return as_utc(chat_session.updated_at)


def _claim_still_held(chat_session: ChatSession, claim: _Claim) -> bool:
    """True if the row is still sitting on the exact claim we verified."""
    if chat_session.interaction_status != claim.interaction_status:
        return False
    if _stamp_of(chat_session, claim.interaction_status) != claim.stamped_at:
        # Same status, new clock: the session re-entered this state since we
        # looked — a fresh turn, or a drain that has already started — so the
        # claim we decided against is not the one in front of us.
        logger.debug(
            "Status repair: session %s was re-claimed at %r while we looked — "
            "leaving it alone",
            chat_session.id,
            claim.interaction_status,
        )
        return False
    return True


def _count_pending(session: Session, session_id: UUID) -> int:
    """Count the session's undelivered user messages.

    The same predicate ``MessageService.collect_pending_messages`` selects on —
    ``role == "user"`` *and* ``sent_to_agent_status == "pending"`` — because
    ``pending_messages_count`` is meant to be exactly the length of that list,
    and a count that used a wider predicate would be a second, disagreeing
    definition of "pending".
    """
    return (
        session.exec(
            select(func.count())
            .select_from(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "user",
                SessionMessage.sent_to_agent_status == "pending",
            )
        ).one()
        or 0
    )


def _oldest_pending_at(session: Session, session_id: UUID) -> datetime | None:
    """When the session's oldest undelivered user message was written.

    The honest answer to "how stale is the instruction we would be delivering",
    and therefore the clock the resend window is measured against. ``None`` when
    there is nothing pending — which is itself decisive: a ``pending_stream``
    session with no pending messages has nothing to re-enter the drain *for*
    (the drain would return ``no_pending_messages`` without writing the status,
    which is one of the ways this row got stuck in the first place).

    Same predicate as ``_count_pending``, for the same reason: one definition of
    "pending" per module.
    """
    stamp = session.exec(
        select(func.min(SessionMessage.timestamp)).where(
            SessionMessage.session_id == session_id,
            SessionMessage.role == "user",
            SessionMessage.sent_to_agent_status == "pending",
        )
    ).one()
    return as_utc(stamp) if stamp is not None else None


def _resent_episode(chat_session: ChatSession) -> datetime | None:
    """The episode this pass has already re-entered the drain for, if any.

    Tolerant on purpose: the value is read back out of a JSON column, and a
    marker nobody can parse must degrade to "no resend recorded" (one extra
    delivery attempt, bounded by the window) rather than to an exception that
    strands the row.
    """
    raw = (chat_session.session_metadata or {}).get(_RESENT_EPISODE_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(raw))
    except ValueError:
        logger.debug(
            "Status repair: session %s carries an unparseable %s marker (%r)",
            chat_session.id,
            _RESENT_EPISODE_KEY,
            raw,
        )
        return None


def _recount_pending(session: Session, session_id: UUID) -> None:
    """Re-derive ``pending_messages_count`` from the actual rows (step B.3).

    Called whenever B.1 or B.2 touches a session, because the counter is
    maintained by the very stream paths that died: it is written alongside the
    interaction status, so a crash between the two leaves it describing a state
    the ``message`` rows disagree with, and the UI shows a badge for messages
    that are not there (or none for messages that are).

    Deliberately does **not** bump ``updated_at``. This is a correction of a
    derived column, not an interaction, and ``updated_at`` is the claim token
    for ``pending_stream`` — forging a heartbeat on it here would hide a row
    from the next tick's candidate query for no reason.
    """
    chat_session = session.get(ChatSession, session_id)
    if chat_session is None:
        session.rollback()
        return
    actual = _count_pending(session, session_id)
    if chat_session.pending_messages_count == actual:
        session.rollback()
        return
    logger.info(
        "Status repair: session %s pending_messages_count %d -> %d (recomputed)",
        session_id,
        chat_session.pending_messages_count,
        actual,
    )
    chat_session.pending_messages_count = actual
    session.add(chat_session)
    session.commit()


def _spawn_drain(session_id: UUID) -> None:
    """Re-enter the drain for one session, exactly as activation would.

    A module-level function rather than an inline call so the one place this
    pass reaches outside the database is nameable — by a reader, and by a test
    that wants the decision without the delivery.

    Fire-and-forget on the application's main loop, and it has to be: the sweep
    runs there (see ``status_repair_scheduler.run_status_repair``), the stream
    it starts outlives this tick, and awaiting it here would hold the sweep open
    for the length of an agent turn. Same call shape, same reason, as
    ``SessionService.handle_environment_activated``.
    """
    create_task_with_error_logging(
        SessionService.initiate_stream(
            session_id=session_id,
            get_fresh_db_session=create_session,
        ),
        task_name=f"status_repair_drain_{session_id}",
    )


def _stamp_resend_claim(
    session: Session, claim: _Claim, oldest_pending_at: datetime
) -> bool:
    """Claim the row before spawning a drain. True if we still own it.

    **This is the one place in the sweep that stamps before it works**, and it
    is not optional bookkeeping. Nothing else on the resend path writes the
    session row: ``initiate_stream`` runs asynchronously and may take minutes to
    reach its first write, so a tick that spawned a drain and left the claim
    untouched would find the identical claim two minutes later and spawn a
    second one — and a third.

    What that repetition actually costs is worth stating precisely, because it
    is *not* a duplicate delivery of the user's message: ``process_pending_messages``
    holds a per-session lock and marks each message sent inside it (see
    ``message_service`` §"Same-session serialization"), so the second drain
    finds nothing pending. The real cost is everything ``initiate_stream`` does
    on the way there — an OAuth credential refresh and a credentials sync into
    the container, plus a title-generation LLM call on an untitled session —
    once per tick, forever, for a row that is going nowhere.

    Two writes, and they do different jobs:

    * ``updated_at`` is the claim token for ``pending_stream``, so bumping it
      buys one threshold's worth of quiet — re-entry suppression, nothing more.
      It is deliberately **not** what the resend window is measured against
      (see ``_RESEND_MAX_AGE_FACTOR``); a window anchored to a stamp this
      function moves could never expire.
    * the episode marker is the hard cap: one resend per row per stuck episode.
      The next tick that finds the same oldest pending message knows this pass
      has already tried, and clears the row to idle instead of trying again.

    The status is left at ``pending_stream``: the drain is about to decide what
    it should become, and clearing it here would only race that decision.
    """
    chat_session = session.get(ChatSession, claim.session_id)
    if chat_session is None or not _claim_still_held(chat_session, claim):
        session.rollback()
        return False
    chat_session.updated_at = datetime.now(UTC)
    # A new dict rather than an in-place mutation: the column is a plain
    # ``Column(JSON)`` with no mutation tracking, so an in-place edit needs
    # ``flag_modified`` to be persisted at all and assignment is the version
    # that cannot be silently dropped by a later refactor.
    chat_session.session_metadata = {
        **(chat_session.session_metadata or {}),
        _RESENT_EPISODE_KEY: oldest_pending_at.isoformat(),
    }
    session.add(chat_session)
    session.commit()
    return True


async def _clear_to_idle(session: Session, claim: _Claim) -> bool:
    """Clear the interaction status and re-derive the pending count.

    Reuses ``SessionService.clear_interaction_status`` rather than writing the
    column here: it is already the platform's idempotent "this stream is over"
    helper, it nulls ``streaming_started_at`` with the status (the pair the rest
    of the code treats as one value), and it emits both of the websocket events
    an open chat window needs to stop showing a spinner.

    Note that helper opens its **own** ``create_session()``, so this pass's
    transaction must be closed before the call — which is why the claim is
    re-verified here and the read rolled back immediately.

    The residual window — a new turn starting between that check and the
    helper's own read — is sub-millisecond, and it is **not** self-correcting:
    ``running`` is written once, by the ``STREAM_STARTED`` handler, so there is
    no "next write" to re-assert it and the new turn would stream with the UI
    showing it as idle until the 3-second session poll re-derives the state.
    Accepted because the window is that narrow and the failure is cosmetic and
    transient; named here so nobody widens it on the strength of a reassurance
    that was never true.

    What this does **not** do is stop the stream. On the ``running`` path it
    clears the column of a turn that may, in the rare legitimate case, still be
    writing — which is why Pass D is told (through ``RepairContext``) not to
    read this column as evidence for any session this pass has touched.
    """
    chat_session = session.get(ChatSession, claim.session_id)
    still_ours = chat_session is not None and _claim_still_held(chat_session, claim)
    session.rollback()
    if not still_ours:
        return False

    await SessionService.clear_interaction_status(
        claim.session_id, reason=_REPAIR_REASON
    )
    _recount_pending(session, claim.session_id)
    return True


class _Plan(NamedTuple):
    """What the read phase decided, in plain values only."""

    age: timedelta
    #: The environment the session is bound to, or ``None`` when it has none
    #: (detached, or deleted out from under it).
    env_id: UUID | None
    #: That environment's status, or ``None`` for the same reasons.
    env_status: str | None
    #: When the oldest undelivered user message was written, or ``None`` when
    #: nothing is pending. This is the clock the resend window is measured on.
    oldest_pending_at: datetime | None
    #: The episode this pass has already re-entered the drain for, if any.
    resent_episode: datetime | None


def _read_and_prepare(
    session: Session, claim: _Claim, now: datetime
) -> _Plan | None:
    """Verify the claim and read what the repair decision needs, or ``None``.

    Returns plain values only: the caller closes this read transaction
    immediately afterwards, and each write phase re-reads under its own.
    """
    chat_session = session.get(ChatSession, claim.session_id)
    if chat_session is None or not _claim_still_held(chat_session, claim):
        return None

    age = now - claim.stamped_at
    if age < _max_quiet_time(claim.interaction_status):
        return None

    env_status: str | None = None
    if chat_session.environment_id is not None:
        environment = session.get(AgentEnvironment, chat_session.environment_id)
        env_status = environment.status if environment is not None else None
    return _Plan(
        age=age,
        env_id=chat_session.environment_id,
        env_status=env_status,
        oldest_pending_at=_oldest_pending_at(session, claim.session_id),
        resent_episode=_resent_episode(chat_session),
    )


def _resend_is_due(plan: _Plan, claim: _Claim, now: datetime) -> bool:
    """Should the drain be re-entered for this parked session?

    Three conditions, each of which is a different question:

    1. the environment is actually up, so there is something to deliver *to*;
    2. the oldest pending message is recent enough that delivering it now is a
       late delivery and not a surprise (plan §5.1) — and there is a pending
       message at all;
    3. this pass has not already re-entered the drain for these same messages.

    Fail any of them and the caller clears the row to idle instead, which leaves
    the messages ``pending`` and recoverable and — unlike a resend — converges.
    """
    if plan.env_status != "running":
        return False
    if plan.oldest_pending_at is None:
        return False
    if (now - plan.oldest_pending_at) >= (
        _max_quiet_time(claim.interaction_status) * _RESEND_MAX_AGE_FACTOR
    ):
        return False
    return plan.resent_episode != plan.oldest_pending_at


async def _repair_session(
    ctx: RepairContext, claim: _Claim, now: datetime
) -> bool:
    """Read → verify → decide → re-verify → write one session."""
    session = ctx.session
    try:
        plan = _read_and_prepare(session, claim, now)
    finally:
        # Every exit from the read section closes its transaction, early
        # returns included — the writes below happen on other connections
        # (``clear_interaction_status`` opens its own) and must not queue
        # behind a read this pass forgot to close.
        session.rollback()

    if plan is None:
        return False

    if claim.interaction_status == _PENDING_STREAM and ctx.is_environment_draining(
        plan.env_id
    ):
        # Pass A repaired this environment to ``running`` moments ago and its
        # ENVIRONMENT_ACTIVATED is already draining exactly these sessions — the
        # row simply has not been written yet (see the module docstring). Acting
        # on it here would either spawn a second drain alongside the first or,
        # once past the resend window, clear the row to idle *while* the drain is
        # in flight, logging "messages stay pending and recoverable" about
        # messages being delivered right now.
        ctx.note_session_in_motion(claim.session_id)
        logger.debug(
            "Status repair: session %s is parked at %r but its environment %s "
            "was just repaired to running — the ordinary drain has it",
            claim.session_id,
            claim.interaction_status,
            plan.env_id,
        )
        return False

    if claim.interaction_status == _RUNNING:
        # B.1. No environment probe: a stream this old is over regardless of
        # whether its container is up, and the container being up is not
        # evidence that anything is still writing into this session. The
        # threshold itself is the safeguard — 120 minutes by default, wide
        # enough that no legitimate turn is reaped mid-flight. A fresh stream
        # heartbeat (plan D10) proves a turn is still live on some worker, so
        # a turn longer than the threshold is left alone — up to the hard cap,
        # the backstop for a stream hung on a still-connected environment
        # (which keeps beating while no chunk ever arrives).
        hard_cap = timedelta(hours=settings.STATUS_REPAIR_STREAM_HARD_MAX_AGE_HOURS)
        if now - claim.stamped_at < hard_cap and _has_fresh_heartbeat(
            session, claim.session_id, now
        ):
            return False
        if not await _clear_to_idle(session, claim):
            return False
        # Recorded for Pass D *after* the write, and only when it landed: this
        # clear is the evidence D would otherwise read as "the turn is over",
        # and the stream itself has not been cancelled.
        ctx.note_session_in_motion(claim.session_id)
        logger.info(
            "Status repair: session %s cleared from %r after %s of streaming "
            "with no terminal event",
            claim.session_id,
            claim.interaction_status,
            plan.age,
        )
        return True

    # B.2. The environment decides, and so does the age of the message itself.
    # Up + recent + not already retried → re-enter the drain the lost
    # ENVIRONMENT_ACTIVATED would have started. Anything else → clearing to idle
    # leaves the messages ``pending`` and recoverable.
    oldest_pending_at = plan.oldest_pending_at
    if oldest_pending_at is not None and _resend_is_due(plan, claim, now):
        if not _stamp_resend_claim(session, claim, oldest_pending_at):
            return False
        _recount_pending(session, claim.session_id)
        _spawn_drain(claim.session_id)
        ctx.note_session_in_motion(claim.session_id)
        logger.info(
            "Status repair: session %s parked at %r for %s with its "
            "environment running and its oldest pending message from %s — "
            "re-entering the drain (once for this episode)",
            claim.session_id,
            claim.interaction_status,
            plan.age,
            plan.oldest_pending_at,
        )
        return True

    if not await _clear_to_idle(session, claim):
        return False
    ctx.note_session_in_motion(claim.session_id)
    logger.info(
        "Status repair: session %s cleared from %r after %s (environment "
        "status: %s, oldest pending message: %s, already re-sent this "
        "episode: %s) — its messages stay pending and recoverable",
        claim.session_id,
        claim.interaction_status,
        plan.age,
        plan.env_status or "unbound",
        plan.oldest_pending_at or "none",
        plan.resent_episode is not None
        and plan.resent_episode == plan.oldest_pending_at,
    )
    return True


async def repair_sessions(ctx: RepairContext) -> int:
    """Repair sessions wedged in a transitional ``interaction_status``.

    Registered as Pass B of the status-repair sweep, after Pass A — whose
    drained environments this pass reads out of ``ctx`` and skips, because their
    sessions are already moving through the ordinary path even though nothing
    has written them yet. Every session this pass does touch is recorded back
    into ``ctx`` for Pass D.

    Returns the number of sessions repaired; it is also the pass's test surface,
    since the ``TESTING`` gate keeps the scheduler itself from running under
    pytest — a test builds ``RepairContext(session=db)`` and awaits this
    directly.
    """
    session = ctx.session
    now = datetime.now(UTC)

    # Claims, not ORM objects: each row is re-read inside its own transaction
    # below. The age predicate stays in Python because the two thresholds read
    # from two different columns, and the transitional set is small — a
    # deployment has at most a handful of sessions mid-interaction at any
    # instant, and only the ones that crossed a threshold do any work.
    claims: list[_Claim] = []
    rows = session.exec(
        select(
            ChatSession.id,
            ChatSession.interaction_status,
            ChatSession.streaming_started_at,
            ChatSession.updated_at,
        ).where(
            col(ChatSession.interaction_status).in_(
                _TRANSITIONAL_INTERACTION_STATUSES
            )
        )
    ).all()
    session.rollback()

    for session_id, interaction_status, streaming_started_at, updated_at in rows:
        stamp = (
            streaming_started_at
            if interaction_status == _RUNNING
            else updated_at
        )
        if stamp is None:
            # A ``running`` session that never recorded a start. See
            # ``_stamp_of``: no honest age, so no repair.
            logger.debug(
                "Status repair: session %s is %r with no streaming_started_at "
                "— no age to measure, skipping",
                session_id,
                interaction_status,
            )
            continue
        claims.append(_Claim(session_id, interaction_status, as_utc(stamp)))

    if not claims:
        return 0

    repaired = 0
    for claim in claims:
        try:
            if await _repair_session(ctx, claim, now):
                repaired += 1
        except Exception as exc:
            # Per-row isolation. Roll back first: a Postgres error aborts the
            # whole transaction, and every later row — and every later *pass*,
            # since they all share this session — would fail with "current
            # transaction is aborted".
            session.rollback()
            logger.error(
                "Status repair: failed to repair session %s (%s): %s",
                claim.session_id,
                claim.interaction_status,
                exc,
                exc_info=True,
            )
    return repaired


# ── Orphaned streams ───────────────────────────────────────────────────
#
# A turn whose process died (or whose task was torn down before it could
# finalize) leaves its agent row at ``streaming_in_progress`` and its session
# at ``running``. Every live turn beats ``stream_heartbeat_at`` into both rows
# about every 30 s (``services/sessions/stream_heartbeat.py``), whichever
# backend worker runs it. This pass acts on that DB evidence only (plan D10):
#
# * heartbeat older than ``STATUS_REPAIR_ORPHAN_STREAM_MIN_AGE_MINUTES``
#   (4 missed beats) → orphaned, sealed / cleared;
# * heartbeat fresher than that → alive (possibly on a sibling worker), kept;
# * no heartbeat (legacy rows written before D10) → agent rows keep the old
#   ``STATUS_REPAIR_STREAM_MAX_AGE_MINUTES`` rule; sessions are left to Pass B.
#
# In-process state (stream registry, session lock) is only an extra **skip**
# guard: it proves a turn is alive on this worker, and proves nothing about
# the others.


def _local_turn_present(session_id: UUID) -> bool:
    """True when this worker runs a turn for the session."""
    return (
        active_streaming_manager.is_streaming_nowait(session_id)
        or is_session_lock_held(str(session_id))
    )


def _heartbeat_of(metadata: dict | None) -> tuple[object, datetime | None]:
    raw = (metadata or {}).get(STREAM_HEARTBEAT_KEY)
    return raw, parse_heartbeat(raw)


def _orphan_message_is_repairable(
    ctx: RepairContext,
    heartbeat: datetime | None,
    session_id: UUID,
    age: timedelta,
    session_interaction_status: str | None,
    heartbeat_cutoff: datetime,
) -> bool:
    if _local_turn_present(session_id):
        return False
    if heartbeat is not None:
        return heartbeat < heartbeat_cutoff
    # Legacy row: no liveness evidence, so only the long bound applies.
    return (
        age >= timedelta(minutes=settings.STATUS_REPAIR_STREAM_MAX_AGE_MINUTES)
        and session_interaction_status != _RUNNING
        and not ctx.is_session_in_motion(session_id)
    )


# How far back (by ``Session.updated_at``) the orphan pass looks for sessions
# that may hold an unfinished agent row. ``updated_at`` moves at stream start
# and at every interaction-status clear, so a crashed turn's session stays in
# the window for the long (legacy) bound plus a day of backend downtime.
# Bounds the scan: ``message.message_metadata`` is JSON (not JSONB) and
# ``message.timestamp`` is not indexed, so the rows are looked up per
# candidate session through the ``(session_id, sequence_number)`` index.
_ORPHAN_SCAN_MARGIN = timedelta(days=1)
_ORPHAN_SCAN_CHUNK = 500


def _orphan_candidate_session_ids(session: Session, now: datetime) -> list[UUID]:
    window_start = now - (
        timedelta(minutes=settings.STATUS_REPAIR_STREAM_MAX_AGE_MINUTES)
        + _ORPHAN_SCAN_MARGIN
    )
    ids = session.exec(
        select(ChatSession.id).where(
            (ChatSession.interaction_status == _RUNNING)
            | (col(ChatSession.updated_at) >= window_start)
        )
    ).all()
    return list(ids)


def _orphan_cutoff(now: datetime) -> datetime:
    return now - timedelta(
        minutes=settings.STATUS_REPAIR_ORPHAN_STREAM_MIN_AGE_MINUTES
    )


def _seal_if_heartbeat_unchanged(
    session: Session, message_id: UUID, observed_heartbeat: object,
) -> bool:
    """Seal the row as aborted unless its heartbeat moved since the read."""
    row = session.get(
        SessionMessage, message_id, with_for_update=True, populate_existing=True,
    )
    if row is None or (row.message_metadata or {}).get(
        STREAM_HEARTBEAT_KEY
    ) != observed_heartbeat:
        session.rollback()
        return False
    # ``_apply_aborted`` re-locks the same row (already ours) and re-checks
    # ``streaming_in_progress``.
    if MessageService._apply_aborted(session, row, None, None):
        return True
    session.rollback()
    return False


async def _repair_orphaned_messages(ctx: RepairContext, now: datetime) -> int:
    session = ctx.session
    cutoff = _orphan_cutoff(now)
    candidate_ids = _orphan_candidate_session_ids(session, now)
    rows = []
    for start in range(0, len(candidate_ids), _ORPHAN_SCAN_CHUNK):
        chunk = candidate_ids[start:start + _ORPHAN_SCAN_CHUNK]
        rows.extend(session.exec(
            select(
                SessionMessage.id,
                SessionMessage.session_id,
                SessionMessage.timestamp,
                SessionMessage.message_metadata,
                ChatSession.interaction_status,
            )
            .join(ChatSession, ChatSession.id == SessionMessage.session_id)
            .where(
                col(SessionMessage.session_id).in_(chunk),
                SessionMessage.role == "agent",
                SessionMessage.message_metadata["streaming_in_progress"].as_string()
                == "true",
                # A row younger than the minimum age cannot carry a stale
                # heartbeat: it was stamped at creation.
                SessionMessage.timestamp < cutoff,
            )
        ).all())
    session.rollback()

    repaired = 0
    for message_id, session_id, timestamp, metadata, interaction_status in rows:
        raw_heartbeat, heartbeat = _heartbeat_of(metadata)
        age = now - as_utc(timestamp)
        try:
            if not _orphan_message_is_repairable(
                ctx, heartbeat, session_id, age, interaction_status, cutoff
            ):
                continue
            if _seal_if_heartbeat_unchanged(session, message_id, raw_heartbeat):
                repaired += 1
                logger.info(
                    "Status repair: agent message %s of session %s sealed as "
                    "aborted (last heartbeat: %s, age: %s)",
                    message_id, session_id, raw_heartbeat or "none (legacy)", age,
                )
        except Exception as exc:
            session.rollback()
            logger.error(
                "Status repair: failed to seal orphaned message %s: %s",
                message_id, exc, exc_info=True,
            )
    return repaired


def _has_fresh_heartbeat(session: Session, session_id: UUID, now: datetime) -> bool:
    row = session.get(ChatSession, session_id, populate_existing=True)
    _, heartbeat = _heartbeat_of(row.session_metadata if row else None)
    session.rollback()
    return heartbeat is not None and heartbeat >= _orphan_cutoff(now)


def _clear_orphaned_session(
    session: Session, claim: _Claim, observed_heartbeat: object,
) -> UUID | None:
    """Clear a ``running`` claim whose heartbeat went stale, under the row lock.

    The claim and the heartbeat are re-checked and the clear committed in one
    locked transaction, so a beat that lands meanwhile either blocks us (and we
    then see its new heartbeat) or finds the row already cleared. The same
    write leaves ``ORPHAN_CLEARED_KEY`` (the heartbeat judged and the original
    ``streaming_started_at``, so a restore keeps the hard-cap clock), which lets a
    live turn restore ``running`` (``StreamHeartbeat``).

    Does the clear itself rather than through
    ``SessionService.clear_interaction_status``: that helper opens its own
    connection, which would block on the lock held here. Returns the owner's
    user id when the row was cleared, else ``None``.
    """
    row = session.get(
        ChatSession, claim.session_id, with_for_update=True, populate_existing=True,
    )
    if (
        row is None
        or not _claim_still_held(row, claim)
        or (row.session_metadata or {}).get(STREAM_HEARTBEAT_KEY) != observed_heartbeat
    ):
        session.rollback()
        return None
    row.interaction_status = ""
    original_start = row.streaming_started_at
    row.streaming_started_at = None
    row.updated_at = datetime.now(UTC)
    row.session_metadata = {
        **(row.session_metadata or {}),
        ORPHAN_CLEARED_KEY: build_orphan_cleared_marker(
            observed_heartbeat, original_start
        ),
    }
    # A turn that crashed before writing any agent row leaves nothing for
    # Pass A to seal; mark its user message so it reads as cut off.
    set_turn_aborted_marker(session, claim.session_id, heartbeat_now())
    user_id = row.user_id
    session.add(row)
    session.commit()
    return user_id


async def _repair_orphaned_sessions(ctx: RepairContext, now: datetime) -> int:
    session = ctx.session
    cutoff = _orphan_cutoff(now)
    rows = session.exec(
        select(
            ChatSession.id,
            ChatSession.streaming_started_at,
            ChatSession.session_metadata,
        ).where(
            ChatSession.interaction_status == _RUNNING,
            col(ChatSession.streaming_started_at).is_not(None),
            col(ChatSession.streaming_started_at) < cutoff,
        )
    ).all()
    session.rollback()

    repaired = 0
    for session_id, streaming_started_at, metadata in rows:
        if ctx.is_session_in_motion(session_id) or _local_turn_present(session_id):
            continue
        raw_heartbeat, heartbeat = _heartbeat_of(metadata)
        if heartbeat is None or heartbeat >= cutoff:
            # Alive, or legacy (Pass B's long bound covers it).
            continue
        claim = _Claim(session_id, _RUNNING, as_utc(streaming_started_at))
        try:
            user_id = _clear_orphaned_session(session, claim, raw_heartbeat)
            if user_id is None:
                continue
            ctx.note_session_in_motion(session_id)
            _recount_pending(session, session_id)
            await SessionService.emit_interaction_status_cleared(session_id, user_id)
            repaired += 1
            logger.info(
                "Status repair: session %s cleared from 'running' — its stream "
                "heartbeat is stale (last: %s)",
                session_id, raw_heartbeat,
            )
        except Exception as exc:
            session.rollback()
            logger.error(
                "Status repair: failed to clear orphaned session %s: %s",
                session_id, exc, exc_info=True,
            )
    return repaired


async def repair_orphaned_streams(ctx: RepairContext) -> int:
    """Seal agent messages and sessions whose stream heartbeat went stale.

    Registered after Pass B (``repair_sessions``) and before the input-task
    pass, which re-derives task state from the sessions cleared here. Every
    session cleared is recorded in ``ctx`` for Pass D. Returns the number of
    messages plus sessions repaired.
    """
    now = datetime.now(UTC)
    repaired = await _repair_orphaned_messages(ctx, now)
    repaired += await _repair_orphaned_sessions(ctx, now)
    return repaired
