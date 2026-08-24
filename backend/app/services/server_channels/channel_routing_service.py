"""Channel routing, split from its effects.

``ChannelRoutingService.decide`` answers one question — *which agent or bundle
should this message go to* — and does nothing else. It creates no thread
binding, no session, no install, and sends no reply. ``ChannelInboundService``
composes it as ``decide()`` → bind → ingest; ``POST /admin/routing/simulate``
calls ``decide()`` and stops.

**Why this is a module and not a ``simulate=True`` flag.** A flag threaded
through the pipeline makes "simulate has no side effects" a property somebody
has to keep true at every branch, forever, including branches added later by
someone who never read this docstring. The split makes it a property of the
*call graph*: simulate cannot bind, because nothing reachable from ``decide``
binds. Four structural facts hold that up, and each is worth preserving
deliberately —

1. **No caller session crosses the boundary.** ``decide`` takes ids and text,
   never a ``Session``. It therefore cannot add to, commit, or roll back a
   transaction the caller is holding. (This is also the rule ``run_in_thread``
   already enforced for the worker targets, applied one level up.)
2. **The sessions it opens are read-only in practice.** Both thread targets
   open a short-lived session, issue ``SELECT``s through
   ``AppMCPRoutingService`` / ``CatalogService``, and close it. Neither calls
   ``add``/``commit``/``delete``, and ``create_session`` does not commit on
   exit.
3. **Nothing effectful is imported here.** No ``ChannelThreadBinding``, no
   ``ChannelOutboundService``, no ingestion or install service. Those names are
   not in this module's namespace, so no branch added later can reach them by
   accident — it would have to add the import, which is visible in a diff.
   ``tests/architecture/channel_routing_purity_test.py`` fails if one appears.
4. **It returns plain data.** Ids and recorders, never ORM instances attached
   to somebody's session — the passes' sessions are closed by the time the
   caller sees the result, so a row handed out here turns the caller's next
   attribute read into a lazy reload against a dead session. The same test
   pins ``decide``'s return type and every field on ``RoutingDecisionResult``
   against an allowlist of plain-data types.

Each of the four is executed by that test, and that is the point of stating
them: a structural fact asserted only in prose is a claim, not a guarantee.
This list is not to be extended without extending the test in the same change
— it was written claiming four while the test enforced three, which put the
fact a reader most needed in the one place nothing would catch it.

The only write the routing pass makes is the routing trace itself, and on the
**happy path** it is deliberately outside ``decide``: the caller persists,
because only the caller knows whether Pass 1 was the whole decision or its first
half (``RoutingDecisionResult.persist_args`` encodes that rule so both callers
apply it identically).

On the **error path** it is inside. Each thread target writes its own
``outcome=error`` trace before re-raising, because ``capture`` re-raises
unchanged and the caller therefore never receives that recorder — an error
decision, the single most useful row this table can hold, would otherwise be the
one outcome no code path can produce. **Callers must account for that:** an
exception out of ``decide`` means a trace has *already* been persisted, and
``RoutingTuningService.simulate`` points the admin at that row rather than
reporting a bare failure over the top of it. ``RoutingTraceService.persist``
opens its own session for the reasons its docstring gives, so none of this can
reach a caller's transaction.

Both passes are offloaded to a worker thread. Routing ends in a blocking LLM
HTTP call and the callers run on the event loop, so inline execution would
stall every other request and stream in the process for the duration of the
provider cascade — externally triggerable, since any whitelisted sender can
open a thread.

See ``docs/plans/auto_routing_tuning_plan.md`` §6.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session as DBSession, select

from app.models import (
    Agent,
    AgentBundle,
    AgentBundleRevision,
    ServerAutoInstallBundle,
    User,
)
from app.services.routing import routing_trace
from app.services.routing.routing_trace import RoutingTrace
from app.services.routing.routing_trace_service import RoutingTraceService

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelRouting]"


@dataclass(frozen=True)
class RoutingDecisionResult:
    """What routing decided, plus the recorders that explain it.

    Deliberately ids rather than ORM rows: the instances the passes loaded
    belong to sessions that are already closed by the time this is returned,
    and handing them out would make the caller's next attribute read a lazy
    reload against a dead session.

    ``catalog_ran`` is a separate flag rather than ``pass2_trace is not None``
    because a pass can run and hand back no recorder (the thread target returns
    ``(None, None)`` when the user row has vanished), and the persist rule below
    turns on *whether Pass 2 ran*, not on whether it produced a trace.
    """

    agent_id: uuid.UUID | None = None
    bundle_uuid: uuid.UUID | None = None
    pass1_trace: RoutingTrace | None = None
    pass2_trace: RoutingTrace | None = None
    catalog_ran: bool = False

    @property
    def persist_args(self) -> tuple[RoutingTrace | None, RoutingTrace | None]:
        """``(trace, preceded_by)`` for :meth:`RoutingTraceService.persist`.

        One inbound message is one decision and gets one row, so when Pass 2
        ran its recorder is the terminal one and Pass 1 folds in behind it.
        When Pass 1 was terminal (it routed, or the caller asked for no catalog
        pass) it is the whole decision and stands alone.

        A property rather than a rule each caller reapplies: the real path and
        simulate must not be able to write differently shaped rows for the same
        decision, or the admin list is comparing two things.
        """
        if self.catalog_ran:
            return self.pass2_trace, self.pass1_trace
        return self.pass1_trace, None

    def persist_call(self) -> Any:
        """The trace write this decision wants, bound and ready for a thread.

        ``run_in_thread`` forwards **positional** arguments only, so ``persist``'s
        ``preceded_by`` keyword has to be bound by a partial before it gets
        there. Binding it here rather than at each call site means the merge
        rule above is applied once: the real path persists from four branches
        and simulate from a fifth, and five hand-written
        ``persist(a, preceded_by=b)`` calls are five chances to disagree about
        which trace is terminal.

        Build it at the call site, immediately before the await — two of the
        real path's branches call ``record_error`` on the trace first, and the
        partial captures the recorder object, so the mutation has to have
        already happened.

        Total by construction, as §11a Rule 2 requires of anything evaluated as
        a bare argument expression: two plain attribute reads off a frozen
        dataclass and a ``functools.partial``. Nothing here can reach the
        database or invoke a ``__str__``.
        """
        import functools

        trace, preceded_by = self.persist_args
        return functools.partial(
            RoutingTraceService.persist, trace, preceded_by=preceded_by
        )


class ChannelRoutingService:
    """Pure routing: Pass 1 (installed agents) then Pass 2 (auto-install catalog)."""

    @staticmethod
    async def decide(
        *,
        user_id: uuid.UUID,
        text: str,
        include_catalog: bool = True,
        origin: str = routing_trace.ORIGIN_SERVER_CHANNEL,
        channel_id: uuid.UUID | None = None,
        thread_key: str | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> RoutingDecisionResult:
        """Route ``text`` for ``user_id``. No binding, session, install or reply.

        Pass 2 runs only when Pass 1 found nothing *and* ``include_catalog`` is
        set — the simulate form exposes that toggle so an admin can ask "would
        this have matched something they already have?" without the catalog
        answering for it.

        ``origin`` / ``actor_user_id`` travel onto the trace unchanged. The real
        path leaves both at their defaults; simulate and replay pass
        ``ORIGIN_SIMULATE`` and the acting admin, which is what makes a
        simulated decision distinguishable from a real one in the admin list
        rather than something a reader has to infer.

        **Deviation from the pre-split behaviour, deliberate and narrow.**
        ``_route_new_thread`` used to gate Pass 2 on re-resolving the Pass-1
        agent in the *caller's* session: if that ``db.get`` came back empty it
        fell through to the catalog pass. That could only happen if the agent
        row were deleted in the microseconds between the worker thread loading
        it and the caller re-reading it, and the fall-through was incidental
        rather than designed — it would auto-install a catalog bundle for a
        sender whose agent had just been deleted. Pass 2 is now gated on Pass 1
        returning no agent at all; the vanished-agent race ends as a no-match
        instead. Nothing else about the ordering changes.
        """
        agent_id, pass1_trace = await ChannelRoutingService.run_in_thread(
            ChannelRoutingService._route_installed_in_thread,
            user_id,
            text,
            channel_id,
            thread_key,
            origin,
            actor_user_id,
        )
        if agent_id is not None or not include_catalog:
            return RoutingDecisionResult(agent_id=agent_id, pass1_trace=pass1_trace)

        bundle_uuid, pass2_trace = await ChannelRoutingService.run_in_thread(
            ChannelRoutingService._route_catalog_in_thread,
            user_id,
            text,
            channel_id,
            thread_key,
            pass1_trace,
            origin,
            actor_user_id,
        )
        return RoutingDecisionResult(
            bundle_uuid=bundle_uuid,
            pass1_trace=pass1_trace,
            pass2_trace=pass2_trace,
            catalog_ran=True,
        )

    @staticmethod
    async def run_in_thread(fn: Any, *args: Any) -> Any:
        """Run a blocking callable off the event loop.

        **Public on purpose**, despite living on this class. Two other modules
        call it — ``channel_inbound_service`` for its four trace writes and
        ``routing_tuning_service`` for simulate's — so an underscore name here
        would be a private symbol that three modules depend on, and the next
        person to change its signature would grep only this file. That is §11a
        Rule 2 instance 4 exactly: a signature change landing without its call
        sites, raising ``TypeError`` at the call site *before* the guarded
        callee is entered, and breaking channel routing outright.

        Both routing passes end in a synchronous LLM HTTP call (and the
        provider manager cascades through providers sequentially). This
        coroutine runs on the main loop, so calling them inline would block
        the whole process — externally triggerable, since any whitelisted
        sender can open a new thread.

        ``fn`` must NOT close over the caller's DB session: ``run_sync`` cannot
        interrupt a running thread, so a cancelled task would close the session
        out from under a mid-query worker. The thread targets below each open
        their own session and return plain ids (the pattern at
        ``message_service.py`` ``_run_in_thread``).

        Capacity note: anyio's default thread limiter is 40. With a webhook
        rate limit of 120/min per token, a burst of new threads queues here
        rather than growing unbounded — queueing is the intended degradation,
        and it is bounded by the limiter rather than by memory.

        **Also the offload for ``RoutingTraceService.persist``**, which the
        callers use it for. ``persist`` is synchronous and does an
        INSERT+COMMIT through a pooled connection of its own; called inline from
        a coroutine it performs that round trip **on the event loop**, so every
        other request and stream in the process waits on it — and it happens up
        to four times per inbound message, on a path any whitelisted sender can
        trigger. That is §11a Rule 2 arriving as latency rather than as an
        exception: the diagnostic degrading the pipeline it exists to explain.

        Note the second pooled connection is not what the offload avoids — a
        worker thread takes one too. It is the *loop* that stops waiting.

        Note ``*args`` is **positional only** — there is no ``**kwargs``,
        deliberately, because ``anyio.to_thread.run_sync``'s own keywords
        (``limiter``, ``abandon_on_cancel``) would be ambiguous with the
        callee's. A call that needs keywords wraps them itself:
        ``await run_in_thread(functools.partial(fn, a, kw=b))``.
        """
        import functools

        import anyio.to_thread

        return await anyio.to_thread.run_sync(functools.partial(fn, *args))

    @staticmethod
    def _route_installed_in_thread(
        user_id: uuid.UUID,
        text: str,
        channel_id: uuid.UUID | None = None,
        thread_key: str | None = None,
        origin: str = routing_trace.ORIGIN_SERVER_CHANNEL,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID | None, RoutingTrace | None]:
        """Thread target for Pass 1. Owns its session; returns (agent id, trace).

        The routing capture is opened **here**, not around the offload.
        ``anyio.to_thread.run_sync`` propagates a *copy* of the caller's
        context, and because the ContextVar holds a mutable recorder a capture
        opened outside would in fact still see the appends — but its lifetime
        would then straddle the thread boundary, which is exactly the ambiguity
        ``run_in_thread`` exists to forbid. Opening it inside keeps the span owned
        by the thread, and the trace travels back as a plain return value like
        the id beside it.
        """
        from app.core.db import create_session

        with create_session() as db:
            user = db.get(User, user_id)
            if user is None:
                return None, None
            trace: RoutingTrace | None = None
            try:
                with RoutingTrace.capture(
                    origin=origin,
                    user_id=user_id,
                    channel_id=channel_id,
                    actor_user_id=actor_user_id,
                    thread_key=thread_key,
                    message=text,
                    stage=routing_trace.STAGE_PASS_1,
                ) as trace:
                    agent = ChannelRoutingService._route_installed(db, user, text)
                    agent_id = agent.id if agent is not None else None
                    if agent_id is not None:
                        trace.record_outcome(
                            routing_trace.OUTCOME_ROUTED, selected_agent_id=agent_id
                        )
            except Exception:
                # ``capture`` stamps ``outcome="error"`` and re-raises unchanged,
                # so the caller never sees this trace — an error decision, the
                # single most useful row this table can hold, would otherwise be
                # the one outcome no code path can produce. Written here, not by
                # the caller.
                #
                # **Deliberately no ``db.rollback()`` before the persist.** One
                # stood here while it was still an open question; it has been
                # settled by execution, not by reading, and removed.
                #
                # It could only ever have mattered to ``persist``, and ``persist``
                # cannot see this session: it opens its own ``Session(engine)``
                # (its docstring says why), so the doomed state of ``db`` is
                # invisible to it. Nothing else touches ``db`` after this point —
                # the ``raise`` below unwinds straight out of the enclosing
                # ``with create_session() as db:``, whose ``__exit__`` closes the
                # session and discards the doomed transaction anyway. That leaves
                # the statement with no observable effect in production.
                #
                # It was a small hazard of its own, too: a ``rollback()`` that
                # raises inside an ``except`` block replaces the original
                # exception and loses the error row entirely — the debugging aid
                # breaking the thing it observes, §11a Rule 2. Guarding it would
                # have meant guarding a statement with no effect; deleting it
                # removes the hazard instead.
                #
                # The property this relied on is pinned by
                # ``tests/api/routing/routing_persist_session_ownership_test.py``,
                # which escapes the domain fixture on purpose — under the standard
                # fixture ``persist`` is handed the caller's session, so nothing
                # else in the suite can see this. Read that file before putting a
                # rollback back.
                #
                # One thing the *suite* does not establish, and which the argument
                # above does not need: under the routing/server_channels fixtures
                # this session, the caller's, and ``persist``'s are all the same
                # object, and the errors those tests induce here are plain Python
                # exceptions that never poison a transaction. So the green suite
                # is consistent with the removal but is not what justifies it —
                # the justification is that in production these are three
                # different sessions on three different connections, which is
                # what the escaping test pins.
                RoutingTraceService.persist(trace)
                raise
            # The happy path is persisted by the CALLER, which is the only place
            # that knows whether Pass 1 was the whole decision or just its first
            # half. Persisting here would write a ``no_match`` row for every
            # message Pass 2 then handled.
            return agent_id, trace

    @staticmethod
    def _route_catalog_in_thread(
        user_id: uuid.UUID,
        text: str,
        channel_id: uuid.UUID | None = None,
        thread_key: str | None = None,
        pass1_trace: RoutingTrace | None = None,
        origin: str = routing_trace.ORIGIN_SERVER_CHANNEL,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID | None, RoutingTrace | None]:
        """Thread target for Pass 2. Owns its session; returns (bundle id, trace).

        Same capture-inside-the-thread rule as Pass 1 above. ``pass1_trace`` is
        the finished Pass-1 recorder, carried in only so the error path below can
        write the two passes as the one decision they are — a plain immutable-by-
        now object, not a session, so it does not violate ``run_in_thread``'s rule.
        """
        from app.core.db import create_session

        with create_session() as db:
            user = db.get(User, user_id)
            if user is None:
                return None, None
            trace: RoutingTrace | None = None
            try:
                with RoutingTrace.capture(
                    origin=origin,
                    user_id=user_id,
                    channel_id=channel_id,
                    actor_user_id=actor_user_id,
                    thread_key=thread_key,
                    message=text,
                    stage=routing_trace.STAGE_PASS_2,
                ) as trace:
                    bundle = ChannelRoutingService._route_catalog(db, user, text)
                    bundle_uuid = bundle.id if bundle is not None else None
            except Exception:
                # Same as Pass 1: write the error trace the caller will never
                # get to see, and do NOT rollback first. The ``db.rollback()``
                # that used to stand here has been removed for the reasons set
                # out at length on Pass 1 — ``persist`` owns its own session and
                # cannot observe this one, and the ``raise`` below closes this
                # one on the way out regardless.
                RoutingTraceService.persist(trace, preceded_by=pass1_trace)
                raise
            # Happy path persisted by the caller, which merges both passes.
            return bundle_uuid, trace

    @staticmethod
    def _route_installed(db: DBSession, user: User, text: str) -> Agent | None:
        """Pass 1 — route over the sender's installed agents.

        **Ownership filter.** ``AppMCPRoutingService.route_message`` answers with
        every route *effective for* the user, which is a broader set than the
        agents they own: identity routes deliberately resolve to another user's
        agent, and an admin-created route can point anywhere. Handing an
        external caller one of those would put their session inside somebody
        else's workspace, so two filters apply:

          1. reject ``is_identity`` outright — by construction someone else's
             agent, and the identity flow's own consent model doesn't cover an
             anonymous external sender;
          2. require ``agent.owner_id == user.id`` — the authoritative check,
             which also catches admin routes pointing at a foreign agent.

        This is the same invariant ``ChannelIngestionService.assert_access``
        asserts for ``channel_caller``; enforcing it here means the pipeline
        declines cleanly (falls through to Pass 2) instead of raising.
        """
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        # The message-text log lines this router used to emit at INFO
        # (`app_mcp_routing_service` "[Stage1] Routing message ..." and
        # `app_agent_router.py`) are now `debug`: the content class here is
        # EXTERNAL, non-platform users' text, and the routing trace carries
        # what those lines were being read for.
        try:
            result = AppMCPRoutingService.route_message(db, user.id, text)
        except Exception as exc:  # noqa: BLE001 — router outage must not 500 the webhook
            logger.exception("%s Pass 1 routing failed", _LOG_PREFIX)
            routing_trace.record_error(exc)
            return None

        if result is None:
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        if result.is_identity:
            # Recorded as its own candidate row rather than by marking the
            # effective route: an identity route's candidate carries a
            # placeholder agent id (Stage 2 resolves the real one), so the
            # agent named here is not in the Pass 1 candidate list.
            routing_trace.record_skip(
                kind=routing_trace.KIND_AGENT,
                ref_id=result.agent_id,
                name=result.agent_name,
                reason=routing_trace.SKIP_IDENTITY_ROUTE,
                source="identity",
            )
            # ...and flip the *Stage 2* row for the same agent, which now
            # exists. Stage 2 records its ballot before this filter runs, and
            # `_candidates()` de-duplicates by ref_id with the **last** stage
            # winning — so without this flip the eligible Stage-2 row would
            # overwrite the skip above and the trace would report an agent as
            # "considered" that this branch had just refused. The skip is the
            # more settled verdict; both rows must carry it.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id,
                reason=routing_trace.SKIP_IDENTITY_ROUTE,
                stage=routing_trace.STAGE_IDENTITY_STAGE2,
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.info(
                "%s Pass 1 matched identity route for user %s — not eligible for "
                "channel routing (agent is not the sender's own install)",
                _LOG_PREFIX,
                user.id,
            )
            return None

        agent = db.get(Agent, result.agent_id)
        if agent is None:
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_AGENT_MISSING
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None
        if agent.owner_id != user.id:
            # This agent IS in the candidate list, so flip that row instead of
            # appending a second one for the same id. ``result.agent_id`` rather
            # than ``agent.id``: identical values (the ``db.get`` above used it),
            # but it is a plain dataclass attribute instead of an ORM one, so no
            # argument in this guarded call can reach the database at all —
            # §11a Rule 2 applied to a read that is merely safe today.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_FOREIGN_OWNER
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.warning(
                "%s Pass 1 matched agent %s owned by %s for sender %s — rejected "
                "(channel sessions must run on the sender's own install)",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                user.id,
            )
            return None

        logger.info(
            "%s Pass 1 matched own install %s for user %s", _LOG_PREFIX, agent.id, user.id
        )
        return agent

    @staticmethod
    def _route_catalog(db: DBSession, user: User, text: str) -> AgentBundle | None:
        """Pass 2 — classify against the server-wide auto-install list.

        Candidates must satisfy all three: not already installed by this user,
        installable *by this user* per catalog visibility (list membership is
        not a grant), and carrying a router trigger prompt to classify on.
        """
        from app.services.bundles.catalog_service import CatalogService
        from app.services.routing.agent_classifier import AgentClassifier, Candidate

        entries = db.exec(select(ServerAutoInstallBundle)).all()
        if not entries:
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        candidates: list[Candidate] = []
        by_id: dict[str, AgentBundle] = {}

        # Every drop below is recorded with a reason. A candidate list showing
        # only the finalists cannot explain the failure that actually bites —
        # the expected bundle was never a candidate at all.
        for entry in entries:
            bundle = db.get(AgentBundle, entry.bundle_uuid)
            # §11a Rule 2, swept rather than spot-fixed. Every ``record_*`` in
            # this loop used to read ``bundle.id`` / ``bundle.display_name``
            # inline in its argument list. Those reads are safe *today* only
            # because nothing between here and the end of the loop commits —
            # ``CatalogService.user_can_install`` below is read-only, checked,
            # not assumed. That is safety by coincidence, the same shape §7
            # rejects for ``stages[].prompt`` resting on a file's byte length: a
            # commit added to any callee in this loop would silently turn these
            # into lazy reloads that raise *before* the recorder's guard is
            # entered, and nothing in a review of THIS function would show it.
            # Read once, up front, off an instance ``db.get`` just materialised.
            bundle_name = bundle.display_name if bundle is not None else ""
            bundle_ref = bundle.id if bundle is not None else entry.bundle_uuid
            if bundle is None or bundle.latest_revision_id is None:
                routing_trace.record_skip(
                    kind=routing_trace.KIND_BUNDLE,
                    ref_id=entry.bundle_uuid,
                    name=bundle_name,
                    reason=routing_trace.SKIP_NO_REVISION,
                    source="catalog",
                )
                continue

            # Already installed → Pass 1 would have handled it if it matched.
            # Publisher installs count as installed: a publisher whose own
            # bundle is on the list should not get a second consumer copy
            # provisioned behind their back by a chat message.
            already = db.exec(
                select(Agent.id).where(
                    Agent.bundle_uuid == bundle.id,
                    Agent.owner_id == user.id,
                )
            ).first()
            if already is not None:
                routing_trace.record_skip(
                    kind=routing_trace.KIND_BUNDLE,
                    ref_id=bundle_ref,
                    name=bundle_name,
                    reason=routing_trace.SKIP_ALREADY_INSTALLED,
                    source="catalog",
                )
                continue

            # Visibility gate — the auto-install list never bypasses it.
            if not CatalogService.user_can_install(db, bundle, user):
                routing_trace.record_skip(
                    kind=routing_trace.KIND_BUNDLE,
                    ref_id=bundle_ref,
                    name=bundle_name,
                    reason=routing_trace.SKIP_NOT_INSTALLABLE,
                    source="catalog",
                )
                logger.debug(
                    "%s Bundle %s not installable by user %s — skipping",
                    _LOG_PREFIX,
                    bundle.id,
                    user.id,
                )
                continue

            revision = db.get(AgentBundleRevision, bundle.latest_revision_id)
            trigger = (revision.router_trigger_prompt or "").strip() if revision else ""
            if not trigger:
                # A dangling latest_revision_id is a data-integrity problem, not
                # a missing prompt. Reporting it as the latter would tell the
                # admin to go write a trigger prompt that already exists.
                routing_trace.record_skip(
                    kind=routing_trace.KIND_BUNDLE,
                    ref_id=bundle_ref,
                    name=bundle_name,
                    reason=(
                        routing_trace.SKIP_NO_TRIGGER_PROMPT
                        if revision is not None
                        else routing_trace.SKIP_NO_REVISION
                    ),
                    source="catalog",
                )
                continue

            routing_trace.record_candidate(
                kind=routing_trace.KIND_BUNDLE,
                ref_id=bundle_ref,
                name=bundle_name,
                source="catalog",
                trigger_prompt=trigger,
            )
            candidates.append(
                Candidate(
                    ref_id=str(bundle.id),
                    name=bundle.display_name,
                    trigger_prompt=trigger,
                    # A catalog bundle has no ``prompt_examples`` of its own —
                    # the field lives on the route/binding a consumer creates at
                    # install time, which by definition does not exist yet here.
                    prompt_examples=None,
                )
            )
            by_id[str(bundle.id)] = bundle

        if not candidates:
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        result = AgentClassifier.classify(candidates, text)
        if result is None or not result.agent_id:
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        matched = by_id.get(str(result.agent_id))
        if matched is None:
            routing_trace.record_parse_outcome(
                reason="classifier picked a bundle that is not among the candidates"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        # Stage-level too, so the debug-feed summary shows "method=ai" for
        # Pass 2 the way it already does for Pass 1.
        routing_trace.record_match(method=routing_trace.MATCH_AI)
        routing_trace.record_outcome(
            routing_trace.OUTCOME_PARKED_INSTALL,
            match_method=routing_trace.MATCH_AI,
            selected_bundle_uuid=matched.id,
        )
        return matched

