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
   ``ChannelCandidateProvider`` / ``CatalogService``, and close it. Neither
   calls ``add``/``commit``/``delete``, and ``create_session`` does not commit
   on exit. Note the pairing is not one service per pass: Pass 1 reads the
   catalog too, through the same ``CatalogService``, when its single-candidate
   short-circuit needs to know whether Pass 2 could offer this sender anything
   (see ``_catalog_ballot``). That read is an availability probe — it selects,
   and it neither classifies nor installs.
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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:  # pragma: no cover — annotations only, see PEP 563
    from app.services.routing.agent_classifier import Candidate

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelRouting]"

#: ``StageTrace.reason`` written on a Pass-2 stage that was **scanned but never
#: classified** — the single-candidate short-circuit consulted the catalog to
#: find out whether there was anything to choose between, and there was not.
#:
#: Without it a reader meets a ``pass_2`` stage carrying candidate rows and no
#: verdict, which looks exactly like a Pass 2 that ran and found nothing. It is
#: written into ``reason`` because that is the field the admin card already
#: renders under the stage heading; note that ``reason`` is deliberately absent
#: from ``routing_trace.SAFE_STAGE_FIELDS``, so with
#: ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` off this note is withheld along with
#: every other free-text stage field. That is the allowlist working as designed
#: — a missing diagnostic rather than a widened exposure — and widening it for
#: one server-authored string is not a trade this change is entitled to make.
CATALOG_AVAILABILITY_ONLY_NOTE = (
    "Pass 2 was evaluated for availability only and never classified: Pass 1 "
    "had a single eligible candidate, so the catalog was scanned to find out "
    "whether there was anything to choose between it and an auto-install "
    "bundle. The rows below are that scan."
)


@dataclass(frozen=True)
class CatalogSkip:
    """One auto-install bundle that did not make the ballot, and why.

    Plain data — three strings — because it crosses a thread boundary. See
    :class:`CatalogBallot`.
    """

    ref_id: str
    name: str
    reason: str


@dataclass
class CatalogBallot:
    """Pass 2's candidate set, computed but **not yet written to the trace**.

    The separation is the whole point, and it is the same discipline
    :attr:`RoutingDecisionResult.persist_args` encodes one level up: *gather
    returns plain data, the caller decides when to commit it*. Pass 2's scan
    records every drop with a reason — a candidate list showing only the
    finalists cannot explain the failure that actually bites — so a probe that
    re-ran the scan **and recorded** would write every bundle twice when Pass 2
    then ran for real, and a probe that recorded **nothing** would lose those
    reasons entirely on the branch where Pass 2 never runs. Neither is
    acceptable, and the hard requirement is that each skip is recorded exactly
    once on every path.

    Everything on it survives a thread hop: ``Candidate`` is a frozen dataclass
    of strings and :class:`CatalogSkip` is three more. **No ``AgentBundle``
    row** — the probe runs in Pass 1's session, which is closed before Pass 2
    opens its own, so the winning bundle is re-loaded by id over there.

    ``scanned`` and ``known`` are separate flags because they lead to opposite
    decisions:

    - ``scanned=False`` — the catalog was never looked at, because
      ``include_catalog`` is off and Pass 2 therefore cannot run at all. The
      choice space genuinely is one, so this short-circuits, and nothing is
      recorded (there is no scan to report).
    - ``known=False`` — the scan was attempted and failed. The choice space is
      *unknown*, which must never be read as empty: this does **not**
      short-circuit, and Pass 2 recomputes from scratch if it runs.
    """

    candidates: list[Candidate] = field(default_factory=list)
    skips: list[CatalogSkip] = field(default_factory=list)
    scanned: bool = True
    known: bool = True

    @property
    def offers_an_alternative(self) -> bool:
        """Is there anything for Pass 2 to offer this sender?

        ``True`` when the scan failed: an unknown choice space is treated as a
        populated one, so a catalog outage costs an LLM call rather than
        silently changing which agent a message reaches.
        """
        return not self.known or bool(self.candidates)


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

        **``include_catalog`` reaches Pass 1**, which it did not before, and
        that is load-bearing rather than tidy: Pass 1's single-candidate
        short-circuit turns on whether Pass 2 could offer an alternative, and
        with the catalog switched off it provably cannot. A Pass 1 that could
        not see the flag would classify where the real path short-circuits, and
        simulate would then diverge from the path it exists to reproduce.

        ``ballot`` is Pass 1's already-computed Pass-2 candidate set, carried
        across so the catalog is scanned **at most once per decision**. Plain
        data (see :class:`CatalogBallot`), and ``None`` on every branch where
        Pass 1 did not scan — in which case Pass 2 scans for itself exactly as
        it always has.
        """
        agent_id, pass1_trace, ballot = await ChannelRoutingService.run_in_thread(
            ChannelRoutingService._route_installed_in_thread,
            user_id,
            text,
            channel_id,
            thread_key,
            origin,
            actor_user_id,
            include_catalog,
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
            ballot,
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
        include_catalog: bool = True,
    ) -> tuple[uuid.UUID | None, RoutingTrace | None, CatalogBallot | None]:
        """Thread target for Pass 1. Owns its session.

        Returns ``(agent id, trace, ballot)``. The third element is Pass 2's
        candidate set when the single-candidate probe computed one *and* Pass 2
        is still going to run — see :class:`CatalogBallot` and the recording
        rule below.

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
                return None, None, None
            trace: RoutingTrace | None = None
            ballot: CatalogBallot | None = None
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
                    agent, ballot = ChannelRoutingService._route_installed(
                        db, user, text, include_catalog=include_catalog
                    )
                    agent_id = agent.id if agent is not None else None
                    if agent_id is not None:
                        trace.record_outcome(
                            routing_trace.OUTCOME_ROUTED, selected_agent_id=agent_id
                        )
                        # Pass 1 routed, so ``decide`` will not run Pass 2 —
                        # this is the ballot's only chance to be recorded, and
                        # the scan behind it genuinely happened. Written under
                        # the ``pass_2`` stage (not ``pass_1``: they are Pass 2's
                        # candidates) and marked as an availability check, so it
                        # cannot be read as a Pass 2 that classified.
                        ChannelRoutingService._record_catalog_ballot(
                            ballot, availability_only=True
                        )
                        ballot = None
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
            #
            # ``ballot`` reaching the caller un-recorded is the same rule in the
            # other direction: Pass 2 is about to run and will record it there,
            # exactly once.
            return agent_id, trace, ballot

    @staticmethod
    def _route_catalog_in_thread(
        user_id: uuid.UUID,
        text: str,
        channel_id: uuid.UUID | None = None,
        thread_key: str | None = None,
        pass1_trace: RoutingTrace | None = None,
        origin: str = routing_trace.ORIGIN_SERVER_CHANNEL,
        actor_user_id: uuid.UUID | None = None,
        ballot: CatalogBallot | None = None,
    ) -> tuple[uuid.UUID | None, RoutingTrace | None]:
        """Thread target for Pass 2. Owns its session; returns (bundle id, trace).

        Same capture-inside-the-thread rule as Pass 1 above. ``pass1_trace`` is
        the finished Pass-1 recorder, carried in only so the error path below can
        write the two passes as the one decision they are — a plain immutable-by-
        now object, not a session, so it does not violate ``run_in_thread``'s rule.

        ``ballot`` is Pass 1's already-computed catalog scan, when it made one,
        so the catalog is read at most once per decision. Also plain data, and
        deliberately carrying no ``AgentBundle`` row: the session it was built
        in is closed, and this one re-loads the winner by id.
        """
        from app.core.db import create_session

        with create_session() as db:
            user = db.get(User, user_id)
            if user is None:
                # The sender's account was deleted between the two thread hops.
                # ``ballot`` is dropped here un-recorded, which is the one gap in
                # "every catalog skip is written exactly once" — and it costs
                # nothing observable, which is why it is documented rather than
                # designed around. This branch already returned before any Pass-2
                # row existed, so the persisted trace has no ``pass_2`` stage in
                # this race either way; the reused ballot changed what was
                # *computed*, not what is stored. Recording it would mean opening
                # a capture and returning a recorder from a pass that did not
                # run, which is a worse lie than a missing stage.
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
                    bundle = ChannelRoutingService._route_catalog(
                        db, user, text, ballot=ballot
                    )
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
    def _route_installed(
        db: DBSession, user: User, text: str, *, include_catalog: bool = True
    ) -> tuple[Agent | None, CatalogBallot | None]:
        """Pass 1 — route the message over the agents the sender **owns**.

        Returns ``(agent, ballot)``. The ballot is Pass 2's candidate set when
        the single-candidate probe below computed one, and ``None`` otherwise;
        the caller is responsible for recording it exactly once (see
        :class:`CatalogBallot`).

        **The candidate set is constructed, not filtered.** This used to call
        ``AppMCPRoutingService.route_message``, whose candidates answer *"what
        can this user address through the App MCP server"* — a set that both
        omitted the sender's own standalone agents (no auto-route is ever
        created for one) and included other people's agents by way of admin
        routes and identity contacts. Two filters then tried to correct that
        subtractively, *downstream of the classifier*, which is how the
        reported incident became a total failure rather than a near miss: the
        LLM spent the decision on an identity route, the filter rejected it
        afterwards, and nothing re-classified over the survivors.
        ``ChannelCandidateProvider`` builds the right set instead — see its
        module docstring, and ``docs/plans/channel_routing_scope_split_plan.md``.

        Two consequences worth stating, because both are deliberate:

        - **No identity branch.** An identity contact cannot be a candidate
          here any more, so there is nothing left to reject.
          ``SKIP_IDENTITY_ROUTE`` stays in ``routing_trace`` regardless — rows
          written before this change still carry it, and the admin UI has to go
          on rendering them.
        - **No route-supplied ``session_mode``.** Nothing on the channel path
          ever read it; ``ChannelIngestionService`` opens ``mode="conversation"``
          sessions by default, which is what those sessions already were.
        - **A ``only_one`` short-circuit, but a conditional one.** A ballot with
          exactly one eligible candidate routes to it without asking a model —
          *provided Pass 2 has nothing to offer this sender*. The governing
          principle is one line, and it is what the unconditional forms of this
          get wrong in both directions:

              **A short-circuit is sound only when there is no alternative to
              choose between.**

          The hazard in the *old* ``only_one`` — the one this pass ran before
          the scope split — was never the short-circuit. It was the set it ran
          over: the lone candidate came from
          ``AppAgentRouteService.get_effective_routes_for_user`` and could
          perfectly well be a foreign agent or an identity contact route, so
          "route to it, no questions asked" could hand an external sender a
          session inside somebody else's workspace. Eliminating that class is
          exactly what ``ChannelCandidateProvider`` did. Over the corrected set
          the short-circuit is safer than it has ever been: the one candidate is
          the sender's own agent by construction. Nothing about it re-breaks
          either property the scope split bought — the set is still the sender's
          own agents, and ineligible candidates are still filtered *before* the
          classifier rather than after it.

          What is genuinely load-bearing is the Pass-2 condition, and the
          sharpened version of the objection is decisive: a newly
          auto-registered sender owns **zero** agents, and the moment Pass 2
          onboards them they own **exactly one**. "Exactly one eligible agent"
          is therefore not an edge case — it is the immediate post-onboarding
          state of every user auto-install has ever served, and under an
          unconditional short-circuit the onboarding message would be the last
          one that could ever reach the catalog. Pass 2's candidates are part of
          the choice space, so the probe below asks whether that space is bigger
          than one before deciding it can be skipped.

          **The probe is confined to the branch that can use it.** Zero eligible
          candidates falls straight through to Pass 2 (unchanged); two or more
          classify regardless, because the choice space is already bigger than
          one and Pass-2 availability cannot change the answer. Only the
          exactly-one branch scans. That confinement is a cost decision as much
          as a tidiness one: an unconfined probe would put a
          ``CatalogService.user_can_install`` round per bundle in front of
          *every* inbound message on an externally triggerable endpoint,
          including the common case where Pass 1 matches and Pass 2 never runs
          today.

          What it costs — the note this replaces claimed two costs, and both
          described a state that no longer exists. The provider cascade is no
          longer paid on every message (a single-candidate sender with an empty
          catalog now pays none), and a single-agent sender's off-topic message
          can no longer be quietly redirected into a catalog install, because
          with an empty catalog there is no install to redirect it to and with a
          non-empty one the classifier still gets to answer *"none of mine"*.

        The ``agent.owner_id != user.id`` guard below is kept as a
        defence-in-depth postcondition and is **unreachable by construction**:
        every candidate came out of a ``WHERE owner_id = :user_id``. It is the
        same invariant ``ChannelIngestionService.assert_access`` asserts for
        ``channel_caller`` sessions, and a cheap assertion is worth more than
        the two lines it costs on a path that hands an external sender a
        session inside a workspace.
        """
        from app.services.routing.agent_classifier import AgentClassifier
        from app.services.routing.channel_candidate_provider import (
            ChannelCandidateProvider,
        )

        # One try block around candidate building *and* classification: a
        # database hiccup and a provider-cascade outage are the same thing to
        # this pass — Pass 1 declines, the pipeline falls through to Pass 2, and
        # the webhook never 500s. (``AgentClassifier.classify`` has its own
        # catch-all and returns ``None``; this is the belt for everything
        # around it.)
        #
        # The message-text log lines this router used to emit at INFO are
        # `debug` throughout: the content class here is EXTERNAL, non-platform
        # users' text, and the routing trace carries what they were read for.
        ballot: CatalogBallot | None = None
        try:
            candidates = ChannelCandidateProvider.build(db, user.id)
            if not candidates:
                # Zero eligible candidates: no probe, straight to Pass 2, which
                # is the onboarding path this state exists for. Unchanged.
                routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
                return None, None

            if len(candidates) == 1:
                # The ONLY branch that probes. See the docstring: with two or
                # more candidates the choice space is already bigger than one,
                # so what Pass 2 holds cannot change whether the classifier
                # runs, and paying for the scan would be pure cost on the
                # common path.
                ballot = ChannelRoutingService._catalog_ballot(
                    db, user, include_catalog=include_catalog
                )
                if not ballot.offers_an_alternative:
                    return (
                        ChannelRoutingService._route_only_candidate(
                            db, user, candidates[0]
                        ),
                        ballot,
                    )

            result = AgentClassifier.classify(candidates, text)
        except Exception as exc:  # noqa: BLE001 — router outage must not 500 the webhook
            logger.exception("%s Pass 1 routing failed", _LOG_PREFIX)
            routing_trace.record_error(exc)
            return None, None

        if result is None or not result.agent_id:
            # ``classify`` already recorded *which* negative outcome this was.
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, ballot

        # Recorded HERE, above the guards below, not after them.
        # ``note_match_method`` is documented to survive a later rejection on
        # purpose: ``outcome=no_match, match_method=ai, selected_agent_id=NULL``
        # says the classifier *did* pick something and a downstream guard threw
        # it out, which is a different and more useful diagnosis than "nothing
        # matched". Two of the guards below are genuinely reachable — a
        # concurrent delete, and a model naming an id that is not on the ballot
        # — and recording the match after them would report both as "the
        # classifier found nothing".
        routing_trace.record_match(method=routing_trace.MATCH_AI)

        known_ids = {c.ref_id for c in candidates}
        if result.agent_id not in known_ids:
            routing_trace.record_parse_outcome(
                reason="classifier picked an agent that is not among the candidates"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, ballot

        try:
            agent_uuid = uuid.UUID(result.agent_id)
        except ValueError:
            # Unreachable for *this* ballot: the membership test above proved
            # the id is one of ours, and every candidate this module builds
            # carries an agent or bundle UUID.
            #
            # Note what stopped being true. ``classify`` used to shape-check
            # every id it returned, so membership and UUID-ness were two
            # independent guarantees; it now accepts any id that is literally
            # on the ballot, because ``IdentityCandidateProvider`` namespaces
            # its refs (``identity:{owner_id}``) and a shape check alone would
            # reject them. Membership therefore no longer implies UUID shape —
            # only this module's own candidate sources do. Keep the guard: it
            # is what makes a future non-UUID ref on this ballot a recorded
            # no-match instead of a raised ``ValueError``.
            routing_trace.record_parse_outcome(
                reason="classifier returned a value that is not a UUID"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, ballot

        agent = db.get(Agent, agent_uuid)
        if agent is None:
            # The row was selected moments ago in this same session; only a
            # concurrent delete gets here.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_AGENT_MISSING
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, ballot
        if agent.owner_id != user.id:
            # Unreachable — see the docstring. ``result.agent_id`` rather than
            # ``agent.id``: identical values (the ``db.get`` above used it), but
            # a plain dataclass attribute instead of an ORM one, so no argument
            # in this guarded call can reach the database at all.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_FOREIGN_OWNER
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.error(
                "%s Pass 1 matched agent %s owned by %s for sender %s — rejected. "
                "This postcondition is unreachable by construction (candidates "
                "come from WHERE owner_id = sender); reaching it means the "
                "candidate provider stopped scoping to the sender's own agents",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                user.id,
            )
            return None, ballot

        logger.info(
            "%s Pass 1 matched own agent %s for user %s", _LOG_PREFIX, agent.id, user.id
        )
        return agent, ballot

    @staticmethod
    def _route_only_candidate(
        db: DBSession, user: User, candidate: Candidate
    ) -> Agent | None:
        """Route to the one eligible candidate without asking a model.

        Reached only when :meth:`_catalog_ballot` has already established that
        Pass 2 has nothing to offer this sender, so there is nothing to choose
        between — see :meth:`_route_installed`'s docstring for why that
        condition and not "exactly one candidate" alone.

        **It does not skip the trace.** The candidate rows and every skipped row
        with its reason were already written by
        ``ChannelCandidateProvider.build`` before this is called; what is added
        here is ``match_method="only_one"``, so a reader can tell that the
        classifier never ran rather than ran and agreed. A fast path that went
        quiet would take the diagnosis away precisely when routing was easy,
        which is the trace an admin least expects to be missing.

        Re-loads the agent by id in this session rather than trusting the
        candidate: the ballot is a projection built moments ago, and this is the
        same ``db.get`` the classifier branch does. The two guards below are the
        same two it applies, for the same reasons — a concurrent delete is
        genuinely reachable, and the ownership check is defence in depth against
        a candidate provider that stopped scoping.
        """
        routing_trace.record_match(method=routing_trace.MATCH_ONLY_ONE)
        try:
            agent_uuid = uuid.UUID(candidate.ref_id)
        except ValueError:
            # Unreachable — the provider builds every ``ref_id`` from an
            # ``Agent.id``. Carries a reason anyway, matching the classifier
            # path's guard: a bare ``no_match`` under ``match_method=only_one``
            # would be the one outcome shape with nothing at all explaining it.
            routing_trace.record_parse_outcome(
                reason="the sole candidate's id is not a UUID"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        agent = db.get(Agent, agent_uuid)
        if agent is None:
            routing_trace.mark_candidate_skipped(
                ref_id=candidate.ref_id, reason=routing_trace.SKIP_AGENT_MISSING
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None
        if agent.owner_id != user.id:
            routing_trace.mark_candidate_skipped(
                ref_id=candidate.ref_id, reason=routing_trace.SKIP_FOREIGN_OWNER
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.error(
                "%s Pass 1 short-circuited to agent %s owned by %s for sender %s "
                "— rejected. Unreachable by construction (candidates come from "
                "WHERE owner_id = sender)",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                user.id,
            )
            return None

        logger.info(
            "%s Pass 1 routed to sole eligible agent %s for user %s without "
            "classifying (no auto-install bundle available to choose against)",
            _LOG_PREFIX,
            agent.id,
            user.id,
        )
        return agent

    @staticmethod
    def _catalog_ballot(
        db: DBSession, user: User, *, include_catalog: bool
    ) -> CatalogBallot:
        """Pass 2's candidate set for ``user``, **availability only**.

        Answers exactly one question — *is the choice space bigger than one?* It
        does not classify, does not pick a bundle, and cannot install one. It is
        also total: any failure comes back as ``known=False``, which
        :attr:`CatalogBallot.offers_an_alternative` reads as "there might be
        something", so a catalog outage costs an LLM call rather than silently
        changing which agent a message reaches.

        With ``include_catalog`` off the database is not touched at all: Pass 2
        cannot run, so the choice space *is* one, and there is no scan to report.
        That is what keeps simulate's ``include_catalog=False`` form answering
        the same question the real path does rather than diverging from it.

        Note what is **not** here: no second copy of the eligibility rule. This
        and Pass 2 share :meth:`_gather_catalog_candidates`, because a probe and
        a pass that disagreed about what "available" means would surface as a
        wrong route months later, with nothing in either function to show why.
        """
        if not include_catalog:
            return CatalogBallot(scanned=False)
        # Read off the instance BEFORE the try, never inside the handler. A
        # ``user.id`` in the ``except`` below is a lazy reload against a session
        # the caught failure may have poisoned, and it would raise *while
        # building a log argument* — replacing the original exception with a
        # worse one. §11a Rule 2, the same sweep this module applies to the
        # catalog loop's ``bundle.display_name``.
        owner_id = user.id
        try:
            return ChannelRoutingService._gather_catalog_candidates(db, user)
        except Exception:  # noqa: BLE001 — an availability probe must not decide anything
            logger.warning(
                "%s Catalog availability probe failed for user %s; classifying "
                "rather than short-circuiting",
                _LOG_PREFIX,
                owner_id,
                exc_info=True,
            )
            return CatalogBallot(known=False)

    @staticmethod
    def _gather_catalog_candidates(db: DBSession, user: User) -> CatalogBallot:
        """The auto-install ballot: who is eligible, and why everyone else is not.

        **Reads and returns; records nothing.** Its two callers commit the
        result to the trace at different moments — Pass 2 when it classifies,
        Pass 1's short-circuit when Pass 2 will never run — and each skip has to
        land exactly once either way. See :class:`CatalogBallot`.

        Candidates must satisfy all four: the bundle resolves with a published
        revision, this user has not already installed it, it is installable *by
        this user* per catalog visibility (list membership is not a grant), and
        that revision carries a router trigger prompt to classify on.
        """
        from app.services.bundles.catalog_service import CatalogService
        from app.services.routing.agent_classifier import Candidate

        # Local import, matching ``CatalogService``'s own below: structural fact
        # 3 in the module docstring bars *effectful* imports, and both of these
        # are read-only.
        ballot = CatalogBallot()
        entries = db.exec(select(ServerAutoInstallBundle)).all()
        if not entries:
            return ballot

        for entry in entries:
            bundle = db.get(AgentBundle, entry.bundle_uuid)
            # §11a Rule 2, swept rather than spot-fixed. Every record built in
            # this loop used to read ``bundle.id`` / ``bundle.display_name``
            # inline in a recorder's argument list. Those reads are safe *today*
            # only because nothing between here and the end of the loop commits
            # — ``CatalogService.user_can_install`` below is read-only, checked,
            # not assumed. That is safety by coincidence, the same shape §7
            # rejects for ``stages[].prompt`` resting on a file's byte length: a
            # commit added to any callee in this loop would silently turn these
            # into lazy reloads that raise *before* the recorder's guard is
            # entered, and nothing in a review of THIS function would show it.
            # Read once, up front, off an instance ``db.get`` just materialised.
            bundle_name = bundle.display_name if bundle is not None else ""
            bundle_ref = str(bundle.id if bundle is not None else entry.bundle_uuid)
            if bundle is None or bundle.latest_revision_id is None:
                ballot.skips.append(
                    CatalogSkip(bundle_ref, bundle_name, routing_trace.SKIP_NO_REVISION)
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
                ballot.skips.append(
                    CatalogSkip(
                        bundle_ref, bundle_name, routing_trace.SKIP_ALREADY_INSTALLED
                    )
                )
                continue

            # Visibility gate — the auto-install list never bypasses it.
            if not CatalogService.user_can_install(db, bundle, user):
                ballot.skips.append(
                    CatalogSkip(
                        bundle_ref, bundle_name, routing_trace.SKIP_NOT_INSTALLABLE
                    )
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
                ballot.skips.append(
                    CatalogSkip(
                        bundle_ref,
                        bundle_name,
                        routing_trace.SKIP_NO_TRIGGER_PROMPT
                        if revision is not None
                        else routing_trace.SKIP_NO_REVISION,
                    )
                )
                continue

            ballot.candidates.append(
                Candidate(
                    ref_id=bundle_ref,
                    name=bundle_name,
                    trigger_prompt=trigger,
                    # A catalog bundle has no ``prompt_examples`` of its own —
                    # the field lives on the route/binding a consumer creates at
                    # install time, which by definition does not exist yet here.
                    prompt_examples=None,
                )
            )
        return ballot

    @staticmethod
    def _record_catalog_ballot(
        ballot: CatalogBallot | None, *, availability_only: bool = False
    ) -> None:
        """Write one ballot into the trace — **once, and only from one caller**.

        Every drop is recorded with a reason: a candidate list showing only the
        finalists cannot explain the failure that actually bites, which on this
        pass is *"why was this user never offered anything?"*.

        ``availability_only`` is for the branch where Pass 1 short-circuited or
        matched and Pass 2 therefore never classified. It addresses the
        ``pass_2`` stage explicitly — these are Pass 2's candidates even though
        Pass 1's capture is the one open — and stamps
        :data:`CATALOG_AVAILABILITY_ONLY_NOTE`, without which a stage carrying
        candidate rows and no verdict is indistinguishable from a Pass 2 that
        ran and found nothing.

        On that branch a scan that found **nothing at all** writes no stage.
        Every skip is still recorded exactly once — there are none — and the
        alternative is a ``pass_2`` heading with a note under it and no rows, on
        every message from every sender on a server with an empty auto-install
        list. That is the common case, and a stage that says only "there was
        nothing to look at" is noise where the note's whole job is to explain
        rows that *are* there.

        **And on that branch the surviving candidates are recorded as skipped,
        not eligible.** They passed every gate, but no classifier was ever given
        them, and ``eligible=True`` is not a description of a bundle — it is an
        input to arithmetic the diagnostic surface performs. Marked eligible
        they would join the "chosen from N eligible candidates" the verdict
        prints, the ``candidate_count`` the admin list shows, the near-miss
        ranking, and — worst — a reachability verdict reading *"X was an
        eligible candidate and the classifier did not pick it"* about a
        classifier that never saw X. ``SKIP_PASS_1_MATCHED`` says the true
        thing instead: available, and beaten to it by Pass 1.
        """
        if ballot is None or not ballot.scanned:
            return
        if availability_only:
            if not ballot.skips and not ballot.candidates:
                return
            with routing_trace.stage_scope(routing_trace.STAGE_PASS_2):
                ChannelRoutingService._record_catalog_rows(
                    ballot, unclassified_reason=routing_trace.SKIP_PASS_1_MATCHED
                )
                routing_trace.record_parse_outcome(
                    reason=CATALOG_AVAILABILITY_ONLY_NOTE
                )
            return
        ChannelRoutingService._record_catalog_rows(ballot)

    @staticmethod
    def _record_catalog_rows(
        ballot: CatalogBallot, *, unclassified_reason: str | None = None
    ) -> None:
        """The ballot's rows. ``unclassified_reason`` demotes the survivors.

        Passed only by the availability-only caller, and it is the difference
        between "Pass 2 offered these to a model" and "Pass 2 held these and was
        never asked". See :meth:`_record_catalog_ballot`.
        """
        for skip in ballot.skips:
            routing_trace.record_skip(
                kind=routing_trace.KIND_BUNDLE,
                ref_id=skip.ref_id,
                name=skip.name,
                reason=skip.reason,
                source="catalog",
            )
        for candidate in ballot.candidates:
            routing_trace.record_candidate(
                kind=routing_trace.KIND_BUNDLE,
                ref_id=candidate.ref_id,
                name=candidate.name,
                source="catalog",
                trigger_prompt=candidate.trigger_prompt,
                eligible=unclassified_reason is None,
                skip_reason=unclassified_reason,
            )

    @staticmethod
    def _route_catalog(
        db: DBSession,
        user: User,
        text: str,
        *,
        ballot: CatalogBallot | None = None,
    ) -> AgentBundle | None:
        """Pass 2 — classify against the server-wide auto-install list.

        ``ballot`` is Pass 1's already-computed scan, handed across when its
        single-candidate probe made one. Reused rather than recomputed so the
        catalog is read at most once per decision; scanned here from scratch
        when it is absent or was not usable, which is every other branch.

        **This is where the ballot is written to the trace**, exactly once —
        including the skips, whose reasons are the answer to "why was this user
        never offered anything?".

        A benign TOCTOU, checked and deliberately not designed around: a reused
        ballot was built in Pass 1's session, which is closed, so a bundle can
        be delisted or installed in between. The winner is therefore re-loaded
        by id in *this* session and a miss resolves to ``no_match`` — the same
        result the un-probed path gives when the catalog empties under it.
        """
        from app.services.routing.agent_classifier import AgentClassifier

        if ballot is None or not ballot.known or not ballot.scanned:
            ballot = ChannelRoutingService._gather_catalog_candidates(db, user)
        ChannelRoutingService._record_catalog_ballot(ballot)

        if not ballot.candidates:
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        result = AgentClassifier.classify(ballot.candidates, text)
        if result is None or not result.agent_id:
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        known_ids = {c.ref_id for c in ballot.candidates}
        if result.agent_id not in known_ids:
            routing_trace.record_parse_outcome(
                reason="classifier picked a bundle that is not among the candidates"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        try:
            # Unreachable while the id is on the ballot — every ``ref_id`` there
            # is ``str(bundle.id)`` — and cheaper to keep than to re-derive.
            bundle_uuid = uuid.UUID(str(result.agent_id))
        except ValueError:
            routing_trace.record_parse_outcome(
                reason="classifier returned a value that is not a UUID"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None

        matched = db.get(AgentBundle, bundle_uuid)
        if matched is None:
            # Reported as its own finding, not folded into "not among the
            # candidates": on a reused ballot the row was read in Pass 1's
            # session and delisted since, which is a real state and a different
            # answer from a classifier that invented an id.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_BUNDLE_MISSING
            )
            routing_trace.record_parse_outcome(
                reason="the chosen bundle no longer exists — it was removed "
                "between this decision's catalog scan and its lookup"
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

