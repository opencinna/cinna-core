"""Admin routing API — traces, simulate, replay, recommendation drafts.

The durable half of routing observability. The channel debug buffer answers
"what just happened" for a few minutes in one worker; these routes answer "why
did it pick that" across restarts and workers, for the retention window.

Everything here requires ``get_current_active_superuser``. There is no
role-based partial access: a trace names the agents another account has
installed, the trigger prompts of agents the reader does not own, and — when
``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is on — an external sender's message text.
That is superuser-or-nothing, the same rule the channel administration surface
applies.

Read-only with respect to agents. Nothing on this router edits an agent, a
trigger prompt, or a bundle. The only writes are the traces themselves (a
simulate stores its own decision, which is what it returns), their deletion, and
the audit rows that record who ran what against whom.

Three of these routes spend a real LLM call per request and decide against
another account's live routing state. They share one set of conditions —
superuser-only, rate-limited on one per-admin bucket, and (for simulate and
replay) audited naming both the acting admin and the target. ``simulate``'s
docstring states plainly what that reach is and why it was not narrowed.

See ``docs/plans/auto_routing_tuning_plan.md`` §6 and §12.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session as DBSession

from app.api.deps import SessionDep, get_current_active_superuser
from app.core.config import settings
from app.models import (
    Message,
    RoutingDecisionPublic,
    RoutingDecisionsPublic,
    RoutingRecommendationPublic,
    RoutingRecommendationRequest,
    RoutingReplayRequest,
    RoutingReplayResult,
    RoutingSimulateRequest,
    SecurityEventCreate,
    ServerChannel,
    User,
)
from app.models.events import security_event as security_event_constants
from app.models.routing.routing_decision import TRACING_DISABLED_NOTICE
from app.services.common.rate_limiter import RateLimiter
from app.services.events.security_event_service import SecurityEventService
from app.services.routing.routing_trace_service import RoutingTraceService
from app.services.routing.routing_tuning_service import (
    RoutingSimulationError,
    RoutingTuningService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/routing", tags=["admin-routing"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


async def _audit(
    session: DBSession,
    user: User,
    event_type: str,
    details: dict[str, Any],
) -> None:
    """Record an admin action. Never blocks the response on audit failure.

    Same shape as ``server_channels.py``'s ``_audit``, deliberately: these two
    routers administer the same feature and an operator reading the security
    feed should not have to learn two conventions. As there, the payload never
    carries message text.
    """
    try:
        await SecurityEventService.create_event(
            session=session,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=event_type, severity="low", details=details
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write security event %s", event_type)


@router.get("/traces", response_model=RoutingDecisionsPublic)
def list_routing_traces(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID | None = Query(default=None),
    origin: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    """Recent routing decisions, newest first.

    ``outcome=no_match`` is the interesting filter: those are the decisions an
    admin is being asked about. Rows carry the verdict and cheap candidate
    counts but not ``stages`` — fetch one trace for the full detail.

    The filters are free-form strings rather than enums on purpose. The origin
    and outcome vocabularies grow without a migration, and a client asking for
    a value this build has never heard of should get an empty page, not a 422.

    With ``ROUTING_TRACE_ENABLED`` off this returns an empty page **plus a
    ``notice``** rather than a bare empty page. An empty table that means "the
    admin turned tracing off" must not look identical to one that means "this
    server has never routed anything" — §11a Rule 1.
    """
    data, count = RoutingTraceService.list(
        session,
        channel_id=channel_id,
        origin=origin,
        outcome=outcome,
        user_id=user_id,
        skip=skip,
        limit=limit,
    )
    return RoutingDecisionsPublic(
        data=data, count=count, notice=RoutingTraceService.disabled_notice()
    )


@router.get("/traces/{trace_id}", response_model=RoutingDecisionPublic)
def get_routing_trace(
    *,
    session: SessionDep,
    current_user: SuperUser,
    trace_id: uuid.UUID,
    expected_agent_id: str | None = Query(
        default=None,
        description=(
            "Narrow the reachability verdict to one candidate: 'why was THIS "
            "one not a candidate'. A candidate ref, not only an agent id — an "
            "agent's bare UUID, or 'identity:{owner_id}' to ask why a *person* "
            "was not reachable. Optional — without it the verdict describes "
            "the decision as a whole."
        ),
    ),
) -> Any:
    """One decision with its full stage trace, and a plain-language verdict.

    ``stages`` carries every candidate the router considered — *including* the
    rejected ones and why each was dropped — plus one entry per provider tried.
    The rendered classifier prompt and the raw model reply come with it only
    while ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is on: both carry the sender's
    words, so the same gate that withholds ``message_text`` withholds them, and
    ``message_text_notice`` says so.

    ``diagnosis`` is the tuning card's headline: one sentence naming why the
    decision went this way and what to change, plus the near-miss ranking. It
    is **computed on the server** (plan §10, Phase 4) so the wording is testable
    and lives beside the routing rules it paraphrases.

    **A query parameter rather than a second endpoint.** The diagnosis is a
    property of this trace, read at the same moment and rendered in the same
    panel; splitting it off would mean two round trips for one view, a second
    place where ``ROUTING_TRACE_ENABLED`` and the message-text gate have to be
    applied consistently, and — because a simulate returns
    ``RoutingTraceService.get``'s output verbatim — a simulate response that
    silently lacked the diagnosis a stored trace has.

    404 also while ``ROUTING_TRACE_ENABLED`` is off — the same status a missing
    id gets, so the gate leaks no existence information, with the notice as the
    detail so the reader can tell the two apart.
    """
    trace = RoutingTraceService.get(
        session, trace_id, expected_agent_id=expected_agent_id
    )
    if trace is None:
        raise HTTPException(
            status_code=404,
            detail=RoutingTraceService.disabled_notice() or "Routing trace not found",
        )
    return trace


@router.delete("/traces")
async def clear_routing_traces(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID | None = Query(default=None),
    all_channels: bool = Query(
        default=False,
        alias="all",
        description=(
            "Required to clear every channel's traces. Without it, and without "
            "channel_id, the request is rejected rather than run unscoped."
        ),
    ),
) -> Message:
    """Drop stored decisions — one channel's, or every channel's on request.

    Mirrors the channel debug-events clear: reproducing a routing problem starts
    from a known-empty feed.

    **The unscoped form has to be asked for by name.** ``channel_id`` is
    optional, so a bare ``DELETE /api/v1/admin/routing/traces`` — the shape a
    client sends when it forgets a parameter, or an operator types by hand —
    used to delete every trace on every channel for the whole retention window.
    A destructive default reachable by omission is §11a Rule 1: the dangerous
    call looked exactly like the ordinary one. Now the unscoped form needs
    ``?all=true``, and neither parameter present is a 400 rather than a wipe.

    **Audited.** This is not only a destructive route, it is one of the two
    erasure paths ``MESSAGE_TEXT_HIDDEN_NOTICE`` and ``TRACING_DISABLED_NOTICE``
    name to operators as the way to actually remove stored message text. An
    unlogged privacy control cannot be shown to have been used, which is most of
    what makes it a control. The event records who cleared what and how many
    rows went — never any message body, following the admin test-send precedent.

    Deliberately still available while ``ROUTING_TRACE_ENABLED`` is off; see
    ``RoutingTraceService.clear``.
    """
    if channel_id is None and not all_channels:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to clear every routing trace on every channel by "
                "default. Pass channel_id to clear one channel's traces, or "
                "all=true to clear them all."
            ),
        )
    deleted = RoutingTraceService.clear(session, channel_id=channel_id)
    await _audit(
        session,
        current_user,
        security_event_constants.ROUTING_TRACES_CLEARED,
        {
            "channel_id": str(channel_id) if channel_id else None,
            "deleted_count": deleted,
        },
    )
    return Message(message=f"Cleared {deleted} routing trace(s)")


# ── Simulate / replay / recommendation (plan §6) ─────────────────────
#
# Process-local, like every other consumer of this limiter. It is a backstop
# against a stuck UI or an enthusiastic admin, not a billing control: with N
# workers the effective ceiling is N × the setting, and that is accepted for
# the same reason the channel webhook accepts it.
_simulate_rate_limiter = RateLimiter()


def _rate_limit(current_user: User, what: str) -> None:
    """Throttle one admin's LLM-spending calls. 429 with ``Retry-After``.

    Keyed by the acting admin, not by target or by message: the resource being
    protected is LLM budget and it is spent per click, whoever it is spent on.

    Shared by simulate, replay and the recommendation draft on purpose. All
    three run a provider cascade per call, and a per-route bucket would let one
    admin spend three times the configured budget by rotating between them.
    """
    retry_after = _simulate_rate_limiter.check(
        f"routing-simulate:{current_user.id}",
        settings.ROUTING_SIMULATE_RATE_LIMIT_PER_MIN,
    )
    if retry_after is None:
        return
    raise HTTPException(
        status_code=429,
        detail=(
            f"Too many routing {what} runs — each one costs a real LLM call. "
            f"The limit is {settings.ROUTING_SIMULATE_RATE_LIMIT_PER_MIN} per "
            f"minute per admin (ROUTING_SIMULATE_RATE_LIMIT_PER_MIN)."
        ),
        headers={"Retry-After": str(int(retry_after))},
    )


def _require_tracing_enabled(what: str) -> None:
    """Refuse before spending anything when traces are not being written.

    ``ROUTING_TRACE_ENABLED`` gates persistence *and* reads, and a simulate's
    whole result is the trace it produces — so with the flag off there would be
    nothing to return. Checked first, before the LLM cascade, so the refusal
    costs nothing; and it carries ``TRACING_DISABLED_NOTICE`` rather than a
    bare "unavailable", because an operator who has forgotten they set that
    flag needs to be told which flag, not that something went wrong.
    """
    if settings.ROUTING_TRACE_ENABLED:
        return
    raise HTTPException(
        status_code=503,
        detail=(
            f"Routing {what} returns its result as a stored routing trace, and "
            f"{TRACING_DISABLED_NOTICE}"
        ),
    )


@router.post("/simulate", response_model=RoutingDecisionPublic)
async def simulate_routing(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: RoutingSimulateRequest,
) -> Any:
    """Route a message as another user would have it routed. **No effects.**

    Returns the same thing ``GET /admin/routing/traces/{id}`` returns — the
    same type, built by the same function — for a decision that was made and
    then not acted on. No thread binding, no session, no install, no outbound
    reply: ``ChannelRoutingService.decide`` cannot do any of those, which is
    why this is not a ``simulate=True`` flag threaded through the pipeline.

    **What this reveals, stated plainly so nobody discovers it later.** The
    response names the agents and bundles ``as_user_id`` has installed, their
    display names, their owners, and those owners' trigger prompts. Almost all
    of that is already in a stored ``routing_decision`` row the moment that user
    sends one message to a channel — what simulate removes is the *waiting*. It
    lets an admin enumerate the routing state of a user who has **never messaged
    the channel at all**, and that is genuinely new reach.

    It is deliberate reach. The narrowing that would remove it — restricting
    simulate to users who already have a channel binding — was considered and
    not adopted, because it would blunt the tool's main use: diagnosing the
    user whose *first* message failed to route, who by definition has no
    binding and no trace. So instead of narrowing, the reach is made
    accountable and non-bulk by the four conditions this route enforces:
    superuser-only; audited per call, naming the acting admin *and* the target;
    rate-limited per admin; and exposing exactly what a stored trace exposes and
    nothing more.

    That last one is not a matter of care. This route does not project anything
    itself — it hands back ``RoutingTraceService.get``'s output, so the
    message-text gate, the ``SAFE_STAGE_FIELDS`` allowlist and the name
    resolution apply here because they are the same code, not because they were
    reimplemented to match.

    **Naming a ``channel_id`` decides under that channel's real policy** rather
    than under ``ResolvedChannelPolicy.for_no_channel()``. That cuts both ways,
    and both ways matter to whoever reads the result.

    *It widens.* The no-channel policy holds ``allow_identity_routing`` False
    deliberately, so naming a channel is the only way a simulate can reproduce
    a ballot containing an identity candidate — a person who opted in to being
    routed to on that channel. Such a row carries that person's display name
    (their full name, falling back to their email), their ``owner_email``, a
    server-composed trigger prompt reading "Contact {name} ({email}). Routes to
    their available agents.", and — as ``prompt_examples`` — one line per
    example on each identity binding the target can reach, re-voiced as "ask
    {name} ({email}) to …". Those example lines are the third party's own
    binding wording, shown to an admin who is neither party.

    *It also narrows*, which is the half a reader will not expect. The resolved
    policy brings that channel's ``agent_scope`` and ``pinned_agent_id`` to
    bear and can switch ``allow_auto_install`` off, so a channel-named run can
    return **fewer** candidates than the same message run with no channel — down
    to a single pinned agent, or to none — and can bar Pass 2 entirely. A
    no-match under a channel is therefore not evidence of a no-match without
    one.

    Either way the reach is the reach a stored trace for that channel already
    has, and it stays inside the four conditions above; the audit row records
    the channel so a run can be told apart from one made without it.
    """
    _require_tracing_enabled("simulate")
    message = (data.message or "").strip()
    if not message:
        raise HTTPException(
            status_code=400,
            detail="Nothing to route: message is empty.",
        )
    # Throttled BEFORE the user lookup, not after. Ordered the other way the
    # 404/200 split answers "does this account exist" at an unbounded rate, and
    # the one route on this router that takes an arbitrary user id would be the
    # cheapest existence oracle in the API. Superuser-only either way, so this
    # is tidiness rather than a hole — but the cheap ordering is the safe one.
    _rate_limit(current_user, "simulate")
    target = session.get(User, data.as_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Beside the target-user 404, and after the limiter, for the same reason it
    # is: this is the second existence oracle on the route, and both belong
    # behind the same bucket. 404 rather than a degrade because a channel id
    # that resolves to nothing *now* is a bad request, and answering it with a
    # classification would bill the admin for an LLM call whose trace then
    # cannot be stored — see the comment on ``channel_id`` below.
    if (
        data.channel_id is not None
        and session.get(ServerChannel, data.channel_id) is None
    ):
        raise HTTPException(status_code=404, detail="Channel not found")

    # Read before the audit's commit expires the instance — the same Rule 2
    # hoist the routing pipeline makes, for the same reason: an argument
    # expression that turns into a lazy reload can raise where it reads like a
    # field access.
    target_id = target.id
    target_email = target.email

    # Audited BEFORE the run, unlike the admin test-send which audits after.
    # The difference is what the two are recording. Test-send audits an effect,
    # and a failed send produced none. This audits an *access* to another
    # account's routing state, and that access — plus the LLM spend — has
    # happened by the time the response is being built, whether or not the
    # response ever arrives.
    await _audit(
        session,
        current_user,
        security_event_constants.ROUTING_SIMULATE_RUN,
        {
            "mode": "simulate",
            "target_user_id": str(target_id),
            "target_user_email": target_email,
            "include_catalog": data.include_catalog,
            # Recorded because it changes what was decided, not merely how it
            # was displayed: a named channel resolves that channel's real
            # policy for the target, so the same message can produce a
            # different ballot — identity candidates included — depending on
            # this one value. An audit row that omitted it would timestamp a
            # run it could not describe.
            "channel_id": str(data.channel_id) if data.channel_id else None,
            # Deliberately NOT the message body: SecurityEvent rows are broadly
            # readable, and the message lives on the routing_decision row behind
            # the superuser-only trace API and its text gate. The length is
            # enough to correlate an audit row with a run.
            "message_chars": len(message),
        },
    )
    try:
        return await RoutingTuningService.simulate(
            session,
            user_id=target_id,
            actor_user_id=current_user.id,
            message=message,
            include_catalog=data.include_catalog,
            # Already checked to exist above, and that check is not
            # redundant with ``_policy_for``'s degrade. ``_policy_for`` was
            # written for replay, whose channel id comes off a stored trace
            # row; for a hand-typed simulate id the degrade is not enough,
            # because the run would classify (real LLM spend) and only then
            # fail the trace INSERT on ``RoutingDecision.channel_id``'s
            # foreign key — which ``persist`` swallows into ``None``, leaving
            # the admin a paid-for 500 pointing at the server log.
            #
            # What remains, stated rather than implied: the check closes the
            # hand-typed case, NOT the race. A channel deleted between that
            # check and the INSERT still fails the foreign key and still 500s
            # here. ``_policy_for``'s degrade keeps the *decision* sane across
            # that window but cannot save the trace, and the trace is what this
            # route returns. Narrow, unfixed, and known — see ``_policy_for``.
            channel_id=data.channel_id,
        )
    except RoutingSimulationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/traces/{trace_id}/replay", response_model=RoutingReplayResult)
async def replay_routing_trace(
    *,
    session: SessionDep,
    current_user: SuperUser,
    trace_id: uuid.UUID,
    data: RoutingReplayRequest | None = None,
) -> Any:
    """Re-run a stored decision's message against **current** state, and diff it.

    The tuning loop's second half: change a trigger prompt, replay the decision
    that went wrong, and see whether it goes right now. The response carries
    both traces plus a field-by-field diff, and the diff says "nothing changed"
    out loud when nothing did — an unchanged replay is the answer that the
    change did not work, and an empty panel would read as though the replay had
    not run.

    Same four conditions as simulate, and for the same reasons: it decides
    against another account's live routing state and spends an LLM call doing
    it. The replay is stored as its own ``origin="simulate"`` trace with the
    acting admin as ``actor_user_id``; the original is left untouched.

    Refused (409) when the original's message text is not available to re-run —
    either because ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` is off now, or because
    it was off when the trace was captured. ``POST /simulate`` with the message
    typed in is the way through that; the refusal says so.
    """
    _require_tracing_enabled("replay")
    original = RoutingTraceService.get(session, trace_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Routing trace not found")
    try:
        # Plain values, detached by the service while the row was freshly
        # loaded. The route never holds the ORM instance, so the audit's commit
        # below cannot expire anything out from under it and the bypassed row's
        # ``message_text`` cannot reach a response builder by accident.
        source = RoutingTuningService.replay_source(session, trace_id)
    except RoutingSimulationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    _rate_limit(current_user, "replay")
    target = session.get(User, source.user_id)
    target_email = target.email if target is not None else None
    await _audit(
        session,
        current_user,
        security_event_constants.ROUTING_SIMULATE_RUN,
        {
            "mode": "replay",
            "source_trace_id": str(trace_id),
            "target_user_id": str(source.user_id),
            "target_user_email": target_email,
            "include_catalog": data.include_catalog if data else True,
            "message_chars": len(source.message),
        },
    )
    try:
        replay = await RoutingTuningService.simulate(
            session,
            user_id=source.user_id,
            actor_user_id=current_user.id,
            message=source.message,
            include_catalog=data.include_catalog if data else True,
            # Carried from the original so the replay lands on the same
            # channel's filtered view. ``origin="simulate"`` is what keeps it
            # distinguishable from a real decision, not the absence of a
            # channel.
            channel_id=source.channel_id,
            thread_key=source.thread_key,
        )
    except RoutingSimulationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return RoutingReplayResult(
        original=original,
        replay=replay,
        diff=RoutingTuningService.diff(original, replay),
    )


@router.post(
    "/traces/{trace_id}/recommendation", response_model=RoutingRecommendationPublic
)
def draft_routing_recommendation(
    *,
    session: SessionDep,
    current_user: SuperUser,
    trace_id: uuid.UUID,
    data: RoutingRecommendationRequest | None = None,
) -> Any:
    """Draft a trigger prompt that would have matched this message. **Writes nothing.**

    The advisory end of the feature, and the reason the whole admin surface can
    be read-only with respect to agents. When a foreign agent routes badly the
    output is wording its owner can apply — never an edit made on their behalf.
    Nothing on this route writes: not the agent, not the bundle, not the trace.

    ``ref_id`` names which candidate of *this trace* to draft for; with an
    obvious subject (the decision's own selection, or a sole candidate) it can
    be omitted. Restricted to this trace's candidates deliberately — an
    unrestricted form would be a "draft a trigger prompt for any agent" oracle
    hanging off a diagnostics endpoint.

    Rate-limited on the same bucket as simulate and replay: it runs a provider
    cascade per call like they do. Not audited, unlike them — it exposes nothing
    the caller did not already have from ``GET /traces/{id}``, and "writes
    nothing" is easier to keep true when it is literally true.
    """
    _require_tracing_enabled("recommendation drafting")
    trace = RoutingTraceService.get(session, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Routing trace not found")
    if not trace.message_text:
        raise HTTPException(
            status_code=409,
            detail=(
                "Drafting a recommendation needs the message that failed to "
                "route, and this trace's text is not available — "
                "ROUTING_TRACE_STORE_MESSAGE_TEXT is off now, or was off when "
                "it was captured."
            ),
        )
    _rate_limit(current_user, "recommendation")
    try:
        return RoutingTuningService.recommend(
            session,
            trace=trace,
            message=trace.message_text,
            ref_id=data.ref_id if data else None,
            actor=current_user,
        )
    except RoutingSimulationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
