"""Pass A of the status-repair sweep — environments abandoned mid-lifecycle.

**The rows this looks at.** An ``AgentEnvironment`` whose ``status`` is one of
the transitional values (``creating``, ``building``, ``rebuilding``,
``starting``, ``activating``) is a row that some background task claims to own.
When that task dies with the process — ``CancelledError`` is a
``BaseException``, so every ``except Exception`` fallback in the lifecycle is
skipped — nothing ever writes the terminal status, and the row keeps the
transitional one forever. It is then invisible to the other schedulers (they all
filter ``status == "running"``) *and* it blocks the user's own way out, because
rebuild is refused while an environment is transitional.

**What decides the repair.** Never the age. The threshold only says when a row
is worth *looking at*; the write is decided by two independent pieces of live
evidence:

1. **A liveness heartbeat** — ``status_changed_at``, which
   ``environment_lifecycle._set_status`` *and* ``_touch_progress`` stamp. Every
   step of a long operation reports itself through ``status_message``
   ("Building template image...", "Installing custom packages...", "Syncing
   credentials..."), so the column tracks *last progress*, not *start time*.
   The build threshold therefore reads "no lifecycle progress for 60 minutes",
   not "this build started 60 minutes ago" — a slow-but-alive build keeps
   pushing the clock forward and is never reaped, while a dead one goes silent
   because the write and the work are the same thread of execution. A dead
   operation cannot fake a heartbeat.

   Rejected alternatives: ``AgentEnvActionLog`` rows are written only at the
   *terminal* outcome of a sub-operation (and at cron-skips), so they are not a
   progress signal at all; ``updated_at`` has no ``onupdate``;
   ``last_activity_at`` is bumped by usage-intent and ``last_health_check``
   only on success, so both lie about a stuck row.

2. **The container itself** — asked through the adapter. Only a container that
   is up *and* healthy licenses a repair to ``running``; only a container that
   is positively absent or exited licenses a repair to ``error``. Anything
   ambiguous (still booting, unhealthy-but-up, Docker unreachable) is left for
   the next tick, because an inconclusive probe is not evidence of death.

The one blind spot in (1) is ``docker build``: a cold image build can run for a
long time while writing nothing to the row. ``template_image_service`` closes it
with a veto-only, process-local check (see ``is_build_in_flight``).

Every repair is its own read → verify → write transaction, and what it
re-checks under the write is the row's whole *claim* — its status together with
``status_changed_at``, not the status alone. A second run over an already
repaired row is a no-op because the row is no longer transitional; the
timestamp is what additionally makes the write safe against a row that was
re-claimed while we were probing it, since a fresh operation can legitimately
re-assert the very same transitional status (see ``_Claim``).
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import create_session
from app.models.agents.agent import Agent
from app.models.environments.environment import AgentEnvironment
from app.models.events.event import EventType

# _TRANSITIONAL_STATUSES is imported rather than re-derived: it is already the
# platform's answer to "which statuses mean a background operation owns this
# row" (it is what blocks a rebuild), and a private copy here would be a fourth
# definition to keep in sync. ``as_utc`` is the shared "this DB column may come
# back naive" reader — the timestamps in this domain are a mix of tz-aware
# (status_changed_at) and naive (created_at/updated_at), and comparing a naive
# one to an aware one raises rather than misbehaving quietly.
from app.services.environments.admin_environment_service import (
    _TRANSITIONAL_STATUSES,
)
from app.services.environments.adapters.base import EnvironmentAdapter
from app.services.environments.environment_lifecycle import _set_status
from app.services.environments.environment_service import EnvironmentService
from app.services.environments.template_image_service import template_image_service
from app.services.events.event_service import event_service
from app.services.system.status_repair_context import RepairContext
from app.utils import as_utc

logger = logging.getLogger(__name__)

# The transitional statuses split by how long the operation behind them may
# legitimately stay quiet. Activation/start is a container start — seconds to
# minutes. Everything else in the transitional set is create/build/rebuild
# shaped, which pulls and builds images and installs packages, so it gets the
# much wider window.
#
# Defined as a SUBTRACTION from the imported set rather than as a second literal
# so a transitional status added later (and forgotten here) lands in the
# conservative bucket instead of silently escaping the pass. It is also how the
# dead ``initializing`` value — no writer anywhere — stays harmlessly covered.
_ACTIVATION_STATUSES = frozenset({"activating", "starting"})
_BUILD_STATUSES = _TRANSITIONAL_STATUSES - _ACTIVATION_STATUSES

# How many multiples of its own threshold a row may stay quiet while its
# container keeps giving no readable answer, before the operation is declared
# lost anyway. A CONSTANT rather than a setting: it is not a tuning knob but a
# statement about the lifecycle — no live path can stay silent this long, since
# every long step heartbeats and the one that does not (``docker build``) is
# vetoed separately. See the escalation branch for why "leave it alone forever"
# is not the conservative option it looks like.
_INCONCLUSIVE_ESCALATION_FACTOR = 6


class _Claim(NamedTuple):
    """One row's transitional claim, as observed by the candidate query.

    ``(status, changed_at)`` together are a claim *token*, and the pair is what
    the write re-checks. Status alone is not enough: because ``_set_status``
    stamps unconditionally, a user clicking Activate on a row stuck at
    ``starting`` re-claims it with the *same* status, and a status-only check
    would happily write "error" over that live activation — or, worse, run a
    second ``_sync_dynamic_data`` into the container alongside the first. The
    timestamp is what distinguishes "still the abandoned claim I verified" from
    "a new operation that happens to use the same word".
    """

    env_id: UUID
    status: str
    changed_at: datetime


# Probe outcomes. Only the first two are actionable; "inconclusive" is the
# golden rule made explicit — a probe that cannot answer is not a licence to
# write a terminal status.
_PROBE_RUNNING = "running"
_PROBE_GONE = "gone"
_PROBE_INCONCLUSIVE = "inconclusive"


def _max_quiet_time(status: str) -> timedelta:
    """How long a row in ``status`` may go without progress before we look."""
    minutes = (
        settings.STATUS_REPAIR_ENV_ACTIVATING_MAX_AGE_MINUTES
        if status in _ACTIVATION_STATUSES
        else settings.STATUS_REPAIR_ENV_BUILDING_MAX_AGE_MINUTES
    )
    return timedelta(minutes=minutes)


def _escalation_time(status: str) -> timedelta:
    """Quiet time past which an unreadable container stops buying more time."""
    return _max_quiet_time(status) * _INCONCLUSIVE_ESCALATION_FACTOR


def _claim_still_held(environment: AgentEnvironment, claim: _Claim) -> bool:
    """True if the row is still sitting on the exact claim we verified."""
    if environment.status != claim.status:
        return False
    if as_utc(environment.status_changed_at) != claim.changed_at:
        # Same status, new clock: something wrote this row since we looked, so
        # the operation behind it is alive and this is not our row to repair.
        logger.debug(
            "Status repair: environment %s was re-claimed at %r while we "
            "probed — leaving it alone",
            environment.id,
            claim.status,
        )
        return False
    return True


def _read_and_prepare(
    session: Session, claim: _Claim, now: datetime
) -> tuple[timedelta, EnvironmentAdapter | None, bool] | None:
    """Verify the claim and build everything the probe needs, or return None.

    Returns only plain values, no ORM objects: the caller closes this read
    transaction immediately afterwards, and the write phase re-reads the row
    under its own transaction.
    """
    environment = session.get(AgentEnvironment, claim.env_id)
    if environment is None or not _claim_still_held(environment, claim):
        return None

    quiet_for = now - claim.changed_at
    if quiet_for < _max_quiet_time(claim.status):
        return None

    if session.get(Agent, environment.agent_id) is None:
        # No owner to notify and no one whose UI would update. Leave the row:
        # an environment without its agent is a different kind of broken.
        logger.warning(
            "Status repair: environment %s is stuck at %r but its agent %s is "
            "gone — skipping",
            claim.env_id,
            claim.status,
            environment.agent_id,
        )
        return None

    # Veto-only liveness check for the one window the row's own heartbeat cannot
    # cover: a cold ``docker build`` writes nothing for its whole duration.
    if claim.status in _BUILD_STATUSES and template_image_service.is_build_in_flight(
        environment.env_name
    ):
        logger.info(
            "Status repair: environment %s quiet for %s at %r, but a template "
            "image build for %r is in flight — leaving it alone",
            claim.env_id,
            quiet_for,
            claim.status,
            environment.env_name,
        )
        return None

    adapter, never_provisioned = _prepare_probe(environment)
    return quiet_for, adapter, never_provisioned


def _prepare_probe(
    environment: AgentEnvironment,
) -> tuple[EnvironmentAdapter | None, bool]:
    """Return ``(adapter, never_provisioned)`` for this environment.

    Synchronous on purpose: the adapter copies the plain values it needs (id,
    directory, port, container name, auth token) and holds no ORM reference, so
    the caller can close its read transaction before awaiting the probe instead
    of holding one open across the network round-trip.

    ``never_provisioned`` — no port allocated — is itself hard evidence, not a
    convenience skip: the port is allocated *before* ``_update_environment_config``
    generates the compose file, so an environment without one has provably never
    had a container. It also has to short-circuit before ``get_adapter``, which
    would otherwise allocate a throwaway port on every tick and leak one from
    the in-memory pool each time.
    """
    if (environment.config or {}).get("port") is None:
        return None, True
    try:
        return EnvironmentService.get_lifecycle_manager().get_adapter(environment), False
    except Exception as exc:
        # Non-docker types raise NotImplementedError here; nothing to probe.
        logger.debug(
            "Status repair: no adapter for environment %s: %s", environment.id, exc
        )
        return None, False


async def _probe(
    adapter: EnvironmentAdapter | None, *, never_provisioned: bool
) -> str:
    """Ask the container what is actually true.

    ``adapter.get_status()`` already folds the health check in — it returns
    "running" only when the container is up *and* answering healthy, and
    "starting" for a container that is up but not yet healthy — which is exactly
    the "running + healthy" evidence the repair needs, so this does not
    re-implement it.

    Deliberately narrow in both directions:
    - "running"        → the environment really is up  → repair to ``running``
    - "stopped"        → container exited or absent    → repair to ``error``
    - anything else    → still booting, wedged-but-up, or Docker unreachable
                          (``get_status`` collapses a daemon error into "error")
                          → inconclusive, leave the row for the next tick.

    The cost of the last case is that a container which is permanently up but
    never healthy keeps its transitional status. That is the intended trade:
    re-probing it every tick is free, and writing "error" over a container that
    is still there would be a guess.
    """
    if adapter is None:
        # Provably never provisioned → gone. Otherwise there is simply nothing
        # here that can answer (a non-docker environment type), which is not the
        # same thing as the container being absent.
        return _PROBE_GONE if never_provisioned else _PROBE_INCONCLUSIVE
    try:
        status = await adapter.get_status()
    except Exception as exc:
        logger.debug("Status repair: container probe failed: %s", exc)
        return _PROBE_INCONCLUSIVE
    if status == "running":
        return _PROBE_RUNNING
    if status == "stopped":
        return _PROBE_GONE
    return _PROBE_INCONCLUSIVE


async def _resync_on_own_session(env_id: UUID, agent_id: UUID) -> None:
    """Re-push prompts, credentials, plugins and handover config. Best-effort.

    The crash skipped this step, and it is the gap that bit us on 2026-09-01:
    the container was up and healthy while its prompts and credentials were
    whatever the previous run had left behind. Failing to sync must not stop the
    repair — a running-but-unsynced environment is still far better than one
    wedged at "activating" forever.

    **On its own session, deliberately.** The sync is minutes of container
    round-trips with progress commits in between, and the sweep's session is
    bound to the *pinned leader connection* every pass shares. Handing it the
    leader session would hold that one connection's transaction open across the
    whole bring-up, which is exactly the invariant the rest of this module
    states ("no ORM objects across the network round-trip"). Its own session
    also means a sync that poisons its transaction cannot abort the repair write
    that follows.

    Private-by-name but the lifecycle's own bring-up entry points call it the
    same way; there is no public "resync this environment" seam yet, and
    inventing one is Pass A scope creep.
    """
    lifecycle = EnvironmentService.get_lifecycle_manager()
    try:
        with create_session() as sync_session:
            environment = sync_session.get(AgentEnvironment, env_id)
            agent = sync_session.get(Agent, agent_id)
            if environment is None or agent is None:
                return
            await lifecycle._sync_dynamic_data(sync_session, environment, agent)
    except Exception as exc:
        logger.warning(
            "Status repair: dynamic-data sync failed for environment %s "
            "(continuing with the repair): %s",
            env_id,
            exc,
        )


async def _repair_to_running(
    session: Session,
    environment: AgentEnvironment,
    agent: Agent,
    claim: _Claim,
) -> bool:
    """Finish the interrupted bring-up: sync dynamic data, then mark running."""
    # Plain values before the rollback below expires the ORM objects.
    env_id = environment.id
    agent_id = agent.id
    owner_id = agent.owner_id

    # Close the leader session's read transaction before the sync: it is the
    # sweep's shared pinned connection and the sync is a long round-trip.
    session.rollback()
    await _resync_on_own_session(env_id, agent_id)

    # Re-read: the rollback above expired the instance the caller handed us, and
    # the row may have moved (or gone) during the sync.
    refreshed = session.get(AgentEnvironment, env_id)
    if refreshed is None:
        logger.debug(
            "Status repair: environment %s vanished during the dynamic-data "
            "sync — skipping",
            env_id,
        )
        return False
    environment = refreshed

    # Status only, not the full claim token: the sync above commits progress
    # messages of its own, so ``status_changed_at`` has legitimately moved and
    # comparing it here would reject every repair. The claim token was checked
    # immediately before this call, which is what closes the window that
    # matters — the multi-second probe. The residual window is a user clicking
    # Activate *during* the sync, and it is not merely a redundant status write:
    # that activation is a container restart, and the drain this function is
    # about to trigger would then be racing it — messages replayed into an
    # environment whose container is going down and coming back. Bounded rather
    # than eliminated, because the activation ends in the same terminal state
    # (status "running", ENVIRONMENT_ACTIVATED emitted) and re-drains the same
    # sessions itself, so the outcome converges even when the timing is ugly.
    if environment.status != claim.status:
        logger.debug(
            "Status repair: environment %s moved to %r during repair — skipping",
            environment.id,
            environment.status,
        )
        return False

    # The marker stays in the UI message on purpose, exactly as it does on the
    # error path: an environment that comes unstuck on its own is a surprising
    # event, and the row should say who moved it.
    _set_status(environment, "running", "Environment is running (reconciled by status repair)")
    environment.last_health_check = datetime.now(UTC)
    # Both halves of the pair the lifecycle writes here, and the second one is
    # load-bearing: environment_suspension_scheduler selects status=="running"
    # and suspends anything whose last_activity_at is older than the inactivity
    # limit (10 min), which a rescued environment's hours-old value always is.
    # Without this it can stop the container out from under the very drain the
    # ENVIRONMENT_ACTIVATED below is about to start. Granting the ordinary
    # post-activation window means an unused environment is suspended one
    # window later, which is the correct outcome rather than an exemption.
    environment.last_activity_at = datetime.now(UTC)
    session.add(environment)
    session.commit()

    # ENVIRONMENT_ACTIVATED, not just a status-changed ping: this is the event
    # that drains sessions parked at ``pending_stream``
    # (SessionService.handle_environment_activated), so repairing the
    # environment un-wedges its sessions through the ordinary path instead of a
    # second, parallel recovery mechanism.
    await event_service.emit_event(
        event_type=EventType.ENVIRONMENT_ACTIVATED,
        model_id=env_id,
        user_id=owner_id,
        meta={
            "environment_id": str(env_id),
            "agent_id": str(agent_id),
            "instance_name": environment.instance_name,
            "reason": "status_repair",
        },
    )
    return True


async def _repair_to_error(
    session: Session,
    environment: AgentEnvironment,
    agent: Agent,
    claimed_status: str,
) -> bool:
    """Record the lost operation as an error so the user can retry it."""
    if claimed_status in _ACTIVATION_STATUSES:
        message = "activation lost (reconciled by status repair)"
    else:
        # A half-finished build is not resumable — the user re-triggers the
        # rebuild, which this very status was blocking.
        message = f"{claimed_status} interrupted (reconciled by status repair)"

    _set_status(environment, "error", message)
    # Every lifecycle error path pairs the status with this; leaving it behind
    # would show whatever unrelated failure last wrote it as the cause of a
    # repair that has a quite different one.
    environment.config = environment.config or {}
    environment.config["last_error"] = message
    flag_modified(environment, "config")
    session.add(environment)
    session.commit()

    await event_service.emit_event(
        event_type=EventType.ENVIRONMENT_STATUS_CHANGED,
        model_id=environment.id,
        user_id=agent.owner_id,
        meta={
            "environment_id": str(environment.id),
            "agent_id": str(agent.id),
            "instance_name": environment.instance_name,
            "status": "error",
            "reason": "status_repair",
            "message": message,
        },
    )
    return True


async def _repair_environment(
    ctx: RepairContext, claim: _Claim, now: datetime
) -> bool:
    """Read → verify → probe → re-verify → write one environment.

    Returns True if the row was repaired.
    """
    session = ctx.session
    try:
        prepared = _read_and_prepare(session, claim, now)
    finally:
        # Every exit from the read section closes its transaction, early
        # returns included — otherwise a skipped candidate leaves one open
        # across the probe's network round-trip and on into the next candidate.
        session.rollback()

    if prepared is None:
        return False
    quiet_for, adapter, never_provisioned = prepared

    outcome = await _probe(adapter, never_provisioned=never_provisioned)
    probe_note = outcome
    if outcome == _PROBE_INCONCLUSIVE:
        if quiet_for < _escalation_time(claim.status):
            logger.debug(
                "Status repair: environment %s quiet for %s at %r but its "
                "container gave no clear answer — leaving it for the next tick",
                claim.env_id,
                quiet_for,
                claim.status,
            )
            return False
        # Escalation. The container is still ambiguous, but the *operation* is
        # not: nothing has written a heartbeat for many multiples of its own
        # threshold, and no live path in the lifecycle is capable of that (the
        # one long silent step, ``docker build``, is vetoed above). Leaving the
        # row transitional forever is not the safe choice it looks like — a
        # transitional status is exactly what blocks the user's rebuild, which
        # is the whole reason this pass exists. The reachable case is a
        # container that is up but permanently unhealthy (e.g. the crash landed
        # after the auth token was rotated in the DB but before the container
        # was recreated, so every health check gets a 401): forever
        # inconclusive, forever unrepairable, and invisible at debug level.
        #
        # **The blast radius is the whole transitional set at once.** The probe
        # collapses a Docker daemon that cannot be reached into "inconclusive"
        # (``get_status`` turns a daemon error into "error"), so an outage
        # lasting 6x the threshold — 6 hours for a build, 1 hour for an
        # activation — escalates *every* transitional row in the deployment on
        # the same tick, marking healthy-but-unreachable environments as errors.
        # Accepted rather than overlooked: those rows need a rebuild after an
        # outage of that length anyway, "error" is a status the user can act on
        # while the transitional one is not, and the alternative (a daemon-wide
        # health gate) is a second liveness signal to keep honest. The warning
        # below is per row on purpose — an operator seeing dozens at once is
        # reading the shape of exactly this event.
        logger.warning(
            "Status repair: environment %s has been quiet for %s at %r — %dx "
            "its threshold — while its container stayed unreadable; treating "
            "the operation as lost",
            claim.env_id,
            quiet_for,
            claim.status,
            _INCONCLUSIVE_ESCALATION_FACTOR,
        )
        outcome = _PROBE_GONE
        probe_note = f"{_PROBE_INCONCLUSIVE} (escalated)"

    try:
        # Re-read under the write transaction. Fresh objects rather than a
        # refresh of the read phase's: this IS the write's own read, and the
        # claim check below is what makes the write safe.
        environment = session.get(AgentEnvironment, claim.env_id)
        if environment is None:
            # Deleted while we probed — a stuck environment is exactly the kind
            # a user reaches for the delete button on.
            logger.debug(
                "Status repair: environment %s vanished during probe", claim.env_id
            )
            return False
        if not _claim_still_held(environment, claim):
            return False
        agent = session.get(Agent, environment.agent_id)
        if agent is None:
            return False
        agent_id = agent.id

        if outcome == _PROBE_RUNNING:
            repaired = await _repair_to_running(session, environment, agent, claim)
            new_status = "running"
            if repaired:
                # Pass B reads this: the ENVIRONMENT_ACTIVATED just emitted
                # drains this environment's ``pending_stream`` sessions, but the
                # handler runs detached and writes nothing to the session rows
                # for several hops, so B would otherwise see the parked claims
                # and act on them a second time.
                ctx.note_drained_environment(claim.env_id)
        else:
            repaired = await _repair_to_error(session, environment, agent, claim.status)
            new_status = "error"

        if repaired:
            logger.info(
                "Status repair: environment %s (agent %s) %s -> %s after %s "
                "without lifecycle progress (container probe: %s)",
                claim.env_id,
                agent_id,
                claim.status,
                new_status,
                quiet_for,
                probe_note,
            )
        return repaired
    finally:
        # Same discipline as the read phase, and for the same reason: every exit
        # from the write section closes its transaction — the early returns
        # above, and the read the ORM reopens when the post-commit event fanout
        # touches an expired attribute. The next candidate (and the next pass)
        # shares this connection.
        session.rollback()


async def repair_environments(ctx: RepairContext) -> int:
    """Repair environments abandoned in a transitional status.

    Registered as Pass A of the status-repair sweep. Returns the number of rows
    repaired; it is also the pass's test surface, since the ``TESTING`` gate
    keeps the scheduler itself from ever running under pytest — a test builds
    ``RepairContext(session=db)`` and awaits this directly.

    Every environment repaired to ``running`` is recorded in ``ctx`` for Pass B,
    because the drain that repair kicks off is asynchronous and leaves no mark
    on the session rows B is about to read.
    """
    session = ctx.session
    now = datetime.now(UTC)

    # Claims, not ORM objects: each row is loaded fresh inside its own
    # transaction below, and one broken row cannot leave a half-populated
    # object in the identity map for the next. No age predicate in SQL — the
    # two thresholds differ per status and the transitional set is a handful of
    # rows on any real deployment.
    claims = [
        _Claim(env_id, status, as_utc(changed_at))
        for env_id, status, changed_at in session.exec(
            select(
                AgentEnvironment.id,
                AgentEnvironment.status,
                AgentEnvironment.status_changed_at,
            ).where(col(AgentEnvironment.status).in_(_TRANSITIONAL_STATUSES))
        ).all()
    ]
    session.rollback()

    if not claims:
        return 0

    repaired = 0
    for claim in claims:
        try:
            if await _repair_environment(ctx, claim, now):
                repaired += 1
        except Exception as exc:
            # Per-row isolation, for the same reason the sweep isolates passes:
            # one environment whose adapter or event fanout blows up must not
            # leave every later candidate unreconciled. Roll back first — a
            # Postgres error aborts the whole transaction, and the next row's
            # read would fail with "current transaction is aborted".
            session.rollback()
            logger.error(
                "Status repair: failed to repair environment %s (%s): %s",
                claim.env_id,
                claim.status,
                exc,
                exc_info=True,
            )
    return repaired
