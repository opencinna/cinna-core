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
   ``ChannelCandidateProvider`` / ``IdentityCandidateProvider`` /
   ``CatalogService``, and close it. Neither calls ``add``/``commit``/
   ``delete``, and ``create_session`` does not commit on exit. Identity
   Stage 2 opens a third such session of its own, under the same rule and for
   the same reason — see the identity paragraph below. Note the pairing is
   not one service per pass: Pass 1 reads the
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

**Identity Stage 2 runs inside this boundary, and inherits all four.** When
Stage 1's ballot puts a *person* on it (``IdentityCandidateProvider``, gated on
``policy.allow_identity_routing``) and that person wins, ``decide`` hands off to
``IdentityRoutingService.route_within_identity``, which resolves *which of that
person's agents* takes the message. It is called from inside ``decide``, so the
four facts above are facts about it too: a caller session it accepted, a write
it made, an effect it imported, or an ORM row it returned would each be that
violation one call deeper — and would land on a route documented as having no
side effects. ``identity_routing_service.py``'s own module docstring states the
same four; the purity test is parameterized over both modules, so that
statement is executed there rather than reviewed.

What crosses back from Stage 2 is ``IdentityGrant`` — three UUIDs on a frozen
dataclass — carried on :class:`RoutingDecisionResult` for the caller to hand to
``ChannelIngestionService.assert_access``, which **re-reads and re-verifies**
every one of them. This module never treats the grant as a conclusion, and
nothing downstream may either: the decision and the session are separated by a
thread hop, and the identity owner may have revoked in between.

**Channel policy arrives as a value, and is never looked up here.** ``decide``
takes a ``ResolvedChannelPolicy`` — the sender's agent scope, their pinned
agent, and whether the auto-install pass may run — resolved by
``ChannelPolicyService`` in the caller, which has a session and a
``ServerChannel`` row. Only the frozen dataclass crosses into this module, and
that is fact 1 restated in the shape this feature needed it: importing
``ChannelPolicyService`` here would put a resolution — three ``SELECT``s — on
the far side of a boundary whose whole guarantee is that it holds no caller
session, and the row it resolves from would then have to cross too. Every
inherit rule lives in exactly one place (that service's docstring says so),
and this module deliberately cannot re-derive one.

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
    CHANNEL_AGENT_SCOPE_ALL,
    Agent,
    AgentBundle,
    AgentBundleRevision,
    IdentityGrant,
    ServerAutoInstallBundle,
    User,
)
from app.services.routing import routing_trace
from app.services.routing.routing_trace import RoutingTrace
from app.services.routing.routing_trace_service import RoutingTraceService

if TYPE_CHECKING:  # pragma: no cover — annotations only, see PEP 563
    from app.services.routing.agent_classifier import Candidate
    from app.services.server_channels.channel_policy_service import (
        ResolvedChannelPolicy,
    )

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

#: ``StageTrace.reason`` written on a Pass-2 stage that **could not run at
#: all**, because this sender's channel policy forbids auto-installing a bundle
#: for them (``ServerChannel.allow_auto_install``).
#:
#: Same field, same gating caveat and same reasoning as
#: :data:`CATALOG_AVAILABILITY_ONLY_NOTE`: ``reason`` is absent from
#: ``routing_trace.SAFE_STAGE_FIELDS``, so with ``ROUTING_TRACE_STORE_MESSAGE_
#: TEXT`` off this note is withheld along with every other free-text stage
#: field. That is the allowlist working as designed — a missing diagnostic
#: rather than a widened exposure — and one server-authored string does not
#: earn a place on it.
PASS_2_NOT_ALLOWED_NOTE = (
    "Pass 2 (auto-install from the catalog) did not run: installing a bundle "
    "for this sender is switched off for this channel. No catalog bundle was "
    "scanned, offered or classified — the absence of rows here is a policy "
    "decision, not an empty auto-install list."
)

#: ``StageTrace.reason`` for the second policy bar, and the one that is a
#: **product decision rather than a switch read literally**: the sender has
#: restricted this channel to specific agents (``agent_scope`` is ``"list"`` or
#: ``"none"``), so Pass 2 does not run even with ``allow_auto_install`` on.
#:
#: See :meth:`ChannelRoutingService._catalog_may_run` for the reasoning. The
#: short version, which is the part a reader of a trace needs: an agent arriving
#: from the catalog is out of scope by construction, so the install would
#: succeed and the binding would still dead-end.
#:
#: **Deliberately says nothing about whose setting it is.** ``agent_scope``
#: inherits — a sender with no ``channel_user_setting`` row follows the admin's
#: ``default_agent_scope``, which is the normal state for an auto-registered
#: sender — and the provenance bit is not on ``ResolvedChannelPolicy``, because
#: that dataclass carries what routing needs to decide and nothing a router
#: could misread as a UI hint. So this note states the fact and leaves the
#: attribution to the reachability verdict on the same trace, which resolves it
#: from ``ChannelPolicyService.describe``. Guessing here would be an
#: attribution nothing in this module can check.
#:
#: Worded for ``"none"`` as much as for ``"list"``: under ``"none"`` the chosen
#: set is empty rather than absent, and an unknown stored scope normalises to
#: ``"none"`` too, so "an explicitly chosen set" is true of all three where
#: "restricted to specific agents" would imply a list that may not exist.
#:
#: Same field and same gating caveat as the two notes around it.
PASS_2_SCOPE_RESTRICTED_NOTE = (
    "Pass 2 (auto-install from the catalog) did not run: routing on this "
    "channel is limited to an explicitly chosen set of this sender's own "
    "agents, and an agent installed from the catalog would not be in that set "
    "from the moment it arrived. No catalog bundle was scanned, offered or "
    "classified — the absence of rows here is a policy decision, not an empty "
    "auto-install list. Whether that limit is this sender's own choice or the "
    "channel's admin default is not recorded here; the verdict on this trace "
    "resolves it."
)

#: The third reason Pass 2 can be barred, and the one a reader is least likely
#: to guess: the sender pinned an agent, and the pin did not resolve. Its own
#: note rather than a shared "policy said no", because the two send the reader
#: to different controls — an admin's channel setting versus this sender's own
#: pin — and a decision carrying ``match_method=pinned`` with a dangling agent
#: is confusing enough without the trace implying the catalog was consulted.
PASS_2_PINNED_NOTE = (
    "Pass 2 (auto-install from the catalog) did not run: this sender has "
    "pinned an agent to this channel, so the routing question was already "
    "answered and no catalog bundle was scanned, offered or classified. "
    "Installing a bundle over an explicit pin would overrule the sender."
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
class IdentitySelection:
    """Stage 1 chose a **person**, and what Stage 2 then made of that.

    Plain data — one UUID and a frozen dataclass of three more — because it
    crosses the worker-thread boundary alongside :class:`CatalogBallot`. No
    ``IdentityAgentBinding`` row: the session Stage 2 read it in is closed
    before ``decide`` returns.

    ``grant is None`` means **Stage 2 ran and selected nothing** — every
    binding this sender could reach was switched off, or the classifier picked
    none of them. It does not mean Stage 2 was skipped; this object exists only
    on the branch where it ran. That distinction is the whole reason the object
    is returned at all instead of just the grant: *"the sender addressed a
    person"* is a fact about the decision that outlives Stage 2's answer, and
    it is what bars Pass 2 (see :meth:`ChannelRoutingService.decide`).
    """

    owner_id: uuid.UUID
    grant: IdentityGrant | None = None


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
    #: Set only when routing crossed into another person's workspace: Stage 1
    #: picked an identity and Stage 2 selected one of that person's agents.
    #: ``agent_id`` is then an agent the **sender does not own**, and this is
    #: the only thing that permits a session on it — the caller passes it to
    #: ``ChannelIngestionService.assert_access`` as
    #: ``ChannelAccessPolicy.identity_grant``.
    #:
    #: A *claim*, never a conclusion. ``assert_access`` re-reads all six facts
    #: behind it; nothing here or downstream may skip that (see
    #: ``IdentityGrant``'s own docstring). ``None`` on every other branch,
    #: which leaves the three-way owner invariant exactly as strict as it was.
    identity_grant: IdentityGrant | None = None

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
        policy: ResolvedChannelPolicy,
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

        ``origin`` / ``actor_user_id`` travel onto the trace unchanged. Simulate
        and replay pass ``ORIGIN_SIMULATE`` and the acting admin, which is what
        makes a simulated decision distinguishable from a real one in the admin
        list rather than something a reader has to infer. The real path passes
        an origin resolved from the transport that accepted the message
        (``_trace_origin`` in ``channel_inbound_service``), so it is
        ``server_channel`` for Google Chat and ``email`` for email since phase
        6 of the channels & identity unification. The default below is what an
        unnamed caller gets, not what the real path relies on.

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

        **Identity is an outcome of Pass 1, and it is terminal.** When
        ``policy.allow_identity_routing`` is on, Pass 1's ballot also carries
        the people this sender may address, and a person winning it hands off
        to Stage 2 (:meth:`_route_identity`). Both of Stage 2's answers end the
        decision: an agent, returned with the :class:`IdentityGrant` that
        permits a session on it, or nothing, which is an ordinary ``no_match``.
        Neither falls through to Pass 2 — see the gate below for why, and
        :meth:`_route_installed` for how the two ballots compose.

        **``policy`` is required, and it is not the door.** It carries three
        things into the decision — the sender's agent scope, their pinned
        agent, and whether Pass 2 may run — each consumed below or in
        :meth:`_route_installed`. The scope is the one read **twice**, and
        deliberately: it narrows the Pass-1 ballot in
        ``ChannelCandidateProvider``, and it separately bars Pass 2 in
        :meth:`_catalog_may_run`, which is a product decision rather than a
        second application of the same filter — that method's docstring is
        where it is argued. What it does **not** do here is
        gate on ``is_available``. That term is the inbound pipeline's, checked
        in ``ChannelInboundService.process_inbound`` before routing is ever
        scheduled, and it produces a *reply* rather than a route. Two reasons
        it is not re-checked here, and the second is the one that decides it:
        this function has no way to express "declined" (its whole vocabulary is
        an agent id, a bundle id, or neither), and an admin running
        ``POST /admin/routing/simulate`` against a user whose channel is
        currently switched off is asking how the message *would* route once it
        is switched back on — which is exactly the question the tuning surface
        exists for, and is separately answerable through
        ``GET /users/me/channels``.
        """
        (
            agent_id,
            pass1_trace,
            ballot,
            identity,
        ) = await ChannelRoutingService.run_in_thread(
            ChannelRoutingService._route_installed_in_thread,
            user_id,
            text,
            policy,
            channel_id,
            thread_key,
            origin,
            actor_user_id,
            include_catalog,
        )
        if (
            agent_id is not None
            # Stage 1 picked a *person*. Pass 2 does not run, whether or not
            # Stage 2 then found one of their agents: auto-installing a catalog
            # bundle is not an answer to "ask HR about my time off", and a
            # sender whose addressee turned out to be unreachable is owed
            # ``no_match`` — the existing "couldn't find an agent" reply —
            # rather than an install they never asked for. Pass 1 was the whole
            # decision on this branch, which is also what makes
            # ``persist_args`` write it as one row.
            or identity is not None
            or not ChannelRoutingService._catalog_may_run(
                policy, include_catalog=include_catalog
            )
        ):
            return RoutingDecisionResult(
                agent_id=agent_id,
                pass1_trace=pass1_trace,
                identity_grant=identity.grant if identity is not None else None,
            )

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
    def _catalog_may_run(
        policy: ResolvedChannelPolicy, *, include_catalog: bool
    ) -> bool:
        """May Pass 2 run at all? The conjunction of four unrelated switches.

        They are genuinely different things and are kept apart everywhere but
        here. ``include_catalog`` is the *admin's diagnostic* toggle on
        simulate — "would this have matched something they already have?" —
        and is ``True`` on every real inbound message.
        ``policy.allow_auto_install`` is the *channel's* configuration, an
        admin default a user cannot override, and it is what makes
        auto-installing a bundle for a whitelisted stranger something a server
        opts into rather than something Google Chat does implicitly.

        **The agent scope is the third term, and it is a decided product semantic
        rather than an implementation convenience.** Pass 2 does not
        run unless the resolved ``agent_scope`` is ``"all"``.

        ``agent_scope="none"`` or ``"list"`` with ``allow_auto_install=True`` is
        otherwise a dead configuration: Pass 2 installs a bundle whose agent is
        out of scope **by construction** — ``ChannelCandidateProvider._in_scope``
        admits an installed agent only under ``"all"``, or under ``"list"`` if
        the sender goes and adds it — so the first message installs and every
        later thread dead-ends on an agent that can never be a candidate again.

        The reasoning, stated here because a future reader will meet the term
        before they meet the argument: a sender who restricted their channel to
        specific agents has said *"nothing routes here but these"*. Performing
        an install and a binding attempt on a path that cannot succeed violates
        least-surprise and this feature's fail-closed ethos, and the one defence
        of the old behaviour — that the install is useful later, once the sender
        manually adds the new agent to their list — is speculative about an
        action nothing prompts them to take. So the restriction is read as the
        instruction it is.

        **A pin is the fourth term, and it is the one that is easy to lose.**
        :meth:`_route_pinned_agent` argues at length that a pinned channel
        never auto-installs — installing a bundle over an explicit instruction
        is the router overruling the person it routes for — and the pin
        short-circuit alone does *not* deliver that. Its two failure branches
        (the agent was deleted, the agent changed hands) return ``None``, which
        is indistinguishable at :meth:`decide` from "Pass 1 found nothing", so
        without this term the sender whose pinned agent vanished between the
        policy resolution and the background routing task would silently be
        auto-installed a catalog bundle and bound to it. That window is a task
        hop plus an LLM cascade wide, not a microsecond. The gate is written
        off ``pinned_agent_id`` — what the sender asked for — rather than off
        whether the pin resolved, because the sender's instruction stands
        whether or not the platform could honour it.

        One function because the conjunction is read in two places that must
        not disagree: here, deciding whether to run Pass 2 at all, and inside
        Pass 1, deciding whether the single-candidate short-circuit may skip
        the classifier. A Pass 1 that thought the catalog was live while
        ``decide`` knew it was not would classify where the real path routes
        directly, which is the divergence ``include_catalog``'s own reach into
        Pass 1 was added to prevent (see :meth:`decide`). The pin term is inert
        on that second reading — a pinned decision returns before the probe is
        reached — and is included anyway so the two readings stay one rule.

        **What the scope term does to that second reading, which is a real
        behaviour change and not a side effect.** On a scope-restricted channel
        the short-circuit now fires where it previously classified: a sender
        with exactly one in-scope eligible agent takes it directly, because
        this function answers ``False`` and :meth:`_catalog_ballot` returns
        ``scanned=False``, whose ``offers_an_alternative`` is ``False``. That is
        correct under the short-circuit's own governing rule — *sound only when
        there is no alternative to choose between* — and it is now **provably**
        so rather than approximately so: Pass 2 cannot run on this channel, so
        the choice space is one by construction, not one by an empty catalog
        that could fill up tomorrow. The scan is also skipped entirely, which
        removes a per-message catalog read that could never have changed the
        answer.
        """
        return (
            include_catalog
            and policy.allow_auto_install
            and policy.agent_scope == CHANNEL_AGENT_SCOPE_ALL
            and policy.pinned_agent_id is None
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
        policy: ResolvedChannelPolicy,
        channel_id: uuid.UUID | None = None,
        thread_key: str | None = None,
        origin: str = routing_trace.ORIGIN_SERVER_CHANNEL,
        actor_user_id: uuid.UUID | None = None,
        include_catalog: bool = True,
    ) -> tuple[
        uuid.UUID | None,
        RoutingTrace | None,
        CatalogBallot | None,
        IdentitySelection | None,
    ]:
        """Thread target for Pass 1. Owns its session.

        Returns ``(agent id, trace, ballot, identity)``. The third element is
        Pass 2's candidate set when the single-candidate probe computed one
        *and* Pass 2 is still going to run — see :class:`CatalogBallot` and the
        recording rule below. The fourth is set only when Stage 1 picked a
        person; it is plain data for the same reason everything else here is
        (:class:`IdentitySelection`), and ``decide`` reads it both for the
        grant and for the fact that Pass 2 must not run.

        ``policy`` is positional and third rather than tacked on the end with a
        default: ``run_in_thread`` forwards positional arguments only, so
        appending it would have meant giving it a default, and the only
        available default is a permissive one. A scope restriction with a
        permissive default is a restriction that stops applying silently the
        first time somebody adds a call site.

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
                return None, None, None, None
            trace: RoutingTrace | None = None
            ballot: CatalogBallot | None = None
            identity: IdentitySelection | None = None
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
                    agent, ballot, identity = ChannelRoutingService._route_installed(
                        db, user, text, policy=policy, include_catalog=include_catalog
                    )
                    agent_id = agent.id if agent is not None else None
                    if agent_id is None and include_catalog and identity is None:
                        # Pass 1 found nothing, so ``decide`` is about to reach
                        # for Pass 2 — and this sender's policy may stop it.
                        # Said here, inside Pass 1's capture, because by the
                        # time ``decide`` applies the gate this is the only
                        # recorder still open and Pass 2 will never open one of
                        # its own.
                        #
                        # Gated on ``include_catalog`` so a simulate that asked
                        # for no catalog pass is not told about a *second*
                        # reason it did not run: the admin who unticked the box
                        # already knows why, and reporting the policy underneath
                        # their own toggle answers a question nobody asked.
                        #
                        # Also gated on ``identity is None``: when Stage 1
                        # picked a person, policy is not why Pass 2 will not
                        # run — the identity decision is (see ``decide``) —
                        # and the three notes this writes each name a control
                        # that is not the one to go and look at.
                        ChannelRoutingService._record_pass_2_not_run(policy)
                    if agent_id is not None:
                        trace.record_outcome(
                            routing_trace.OUTCOME_ROUTED, selected_agent_id=agent_id
                        )
                    if agent_id is not None or identity is not None:
                        # Pass 1 was terminal, so ``decide`` will not run
                        # Pass 2 — this is the ballot's only chance to be
                        # recorded, and the scan behind it genuinely happened.
                        # Written under the ``pass_2`` stage (not ``pass_1``:
                        # they are Pass 2's candidates) and marked as an
                        # availability check, so it cannot be read as a Pass 2
                        # that classified.
                        #
                        # ``identity is not None`` belongs in this condition
                        # for exactly the reason the ``agent_id`` half does:
                        # Stage 1 picking a person also ends the decision, and
                        # a ballot dropped here would lose every skip reason on
                        # it — "each skip is recorded exactly once on every
                        # path" is the rule :class:`CatalogBallot` states, and
                        # it does not have a Stage-2-shaped exception.
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
            return agent_id, trace, ballot, identity

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
        db: DBSession,
        user: User,
        text: str,
        *,
        policy: ResolvedChannelPolicy,
        include_catalog: bool = True,
    ) -> tuple[Agent | None, CatalogBallot | None, IdentitySelection | None]:
        """Pass 1 — route the message over what the sender may address.

        Returns ``(agent, ballot, identity)``. The ballot is Pass 2's candidate
        set when the single-candidate probe below computed one, and ``None``
        otherwise; the caller is responsible for recording it exactly once (see
        :class:`CatalogBallot`). ``identity`` is set only when the winning
        candidate named a *person* — see :meth:`_route_identity`.

        **The pin is answered before anything else runs** — see
        :meth:`_route_pinned_agent` for why it outranks both short-circuits and
        Pass 2, and what that costs. Everything below this paragraph describes
        the unpinned path, which is every sender who has not made that choice.

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
        module docstring, and
        ``docs/application/server_channels/server_channels.md``.

        **Identities are a second provider, composed — not the old branch
        restored.** ``IdentityCandidateProvider.build`` appends one candidate
        per *person* this sender may address, and only when
        ``policy.allow_identity_routing`` says so. Read the difference
        carefully, because the two look alike from a distance and one of them
        was an incident: the deleted branch inherited *somebody else's ballot*
        — ``AppAgentRouteService.get_effective_routes_for_user`` — complete with
        three App MCP enablement toggles, and then tried to reject the parts it
        did not want downstream of the classifier. This adds a set built for
        this surface, gated on this sender's own consent, *before* the
        classifier, and a winner from it goes to Stage 2 rather than to a
        rejection. Providers compose; surfaces do not borrow (master plan §3.1).
        ``SKIP_IDENTITY_ROUTE`` still has no producer here — it is the old
        branch's rejection reason, and rows written before the split still
        carry it, so the admin UI goes on rendering it.

        Two further consequences worth stating, because both are deliberate:

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
          either one of the sender's own agents or a person this sender
          consented to address, by construction, and ineligible candidates are
          still filtered *before* the classifier rather than after it.

          **A lone identity candidate short-circuits too, and that branch is
          the HR story's simplest shape rather than a corner case.** A sender
          who owns no agents and can reach exactly one identity owner produces
          a one-candidate ballot, and on a server with an empty auto-install
          list it takes this path with no classifier call at all. So
          :meth:`_route_only_candidate` runs Stage 2 as well — the difference
          between the two branches is only whether a model was asked *which*
          candidate, never what happens to the one that wins.

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
          with an empty catalog there is no install to redirect it to.

          What it does still cost, stated because :meth:`_catalog_may_run` now
          has more ways to answer ``False``: whenever Pass 2 cannot run **as a
          matter of policy** — the channel's ``allow_auto_install`` is off, or
          its agent scope is restricted — a single-candidate sender's off-topic
          message routes to that sole agent without the classifier being given
          the chance to answer *"none of mine"*. That is the short-circuit's own
          rule applied honestly rather than a regression: there is genuinely no
          alternative to choose between, because the only other pass is barred.
          It is worth knowing it is what a scope restriction buys.

        The ``agent.owner_id != user.id`` guard below is a defence-in-depth
        postcondition on the **non-identity** path, and on that path it remains
        unreachable by construction: every candidate the channel provider
        builds came out of a ``WHERE owner_id = :user_id``. It is the same
        invariant ``ChannelIngestionService.assert_access`` asserts for
        ``channel_caller`` sessions, and a cheap assertion is worth more than
        the two lines it costs on a path that hands an external sender a
        session inside a workspace.

        **The identity path is foreign by design, and is dispatched before
        that guard is reached** — in both the classifier branch and
        :meth:`_route_only_candidate`. This is not the guard being relaxed: an
        identity-selected agent is permitted precisely because Stage 2 produced
        an :class:`IdentityGrant` naming the binding and assignment behind it,
        which ``assert_access`` then re-reads and re-verifies before any session
        opens. What the guard forbids — a foreign agent with *nothing*
        authorising it — is still forbidden, and :meth:`_route_identity` adds
        its own postcondition in the same shape (the agent must belong to the
        identity owner Stage 1 chose).
        """
        from app.services.routing.agent_classifier import AgentClassifier
        from app.services.routing.channel_candidate_provider import (
            ChannelCandidateProvider,
        )
        from app.services.routing.identity_candidate_provider import (
            IdentityCandidateProvider,
            parse_identity_ref,
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
            if policy.pinned_agent_id is not None:
                return (
                    ChannelRoutingService._route_pinned_agent(
                        db, user, policy.pinned_agent_id
                    ),
                    None,
                    None,
                )

            candidates = ChannelCandidateProvider.build(db, user.id, policy=policy)
            # Owned agents first, identities after. That ordering is for the
            # trace and the prompt to read top-down — the common case (my own
            # agents) before the exceptional one (another person's) — and
            # nothing turns on it: both providers sort internally, and the
            # classifier is given a set, not a priority list.
            #
            # `policy.allow_identity_routing` is the sender's consent to being
            # routed into somebody else's workspace, and it is enforced INSIDE
            # the provider (phase 7 of the channels & identity unification):
            # with it off, `build` returns an empty list before it queries
            # anything, so this line adds nothing and the two surfaces that
            # compose the same provider cannot drift on what the switch means.
            # There is deliberately no `if` here to forget.
            #
            # That empty return is also the DELIBERATE INVERSION of master plan
            # §3.5 ("a candidate excluded without a skip_reason cannot diagnose
            # the failure that actually bites") — the only one in the feature.
            # Identity owners this sender *could* have reached leave no trace
            # rows at all, not even skips, because a trace an external sender
            # can trigger at will would otherwise enumerate other people's
            # identities, one row per person who has ever named them on a
            # binding. The §3.5 rule buys a diagnosis; here it would sell an
            # enumeration. The diagnosis is not lost, only moved — the sender's
            # own Settings > Channels page says whether the switch is on, and
            # that is the one control that changes this outcome. The reasoning
            # in full, and the "do not fix it by filtering after" warning, are
            # in `identity_candidate_provider`'s module docstring, which is now
            # where somebody would go to "fix" it.
            candidates += IdentityCandidateProvider.build(db, user.id, policy=policy)
            if not candidates:
                # Zero eligible candidates: no probe, straight to Pass 2, which
                # is the onboarding path this state exists for. Unchanged.
                routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
                return None, None, None

            if len(candidates) == 1:
                # The ONLY branch that probes. See the docstring: with two or
                # more candidates the choice space is already bigger than one,
                # so what Pass 2 holds cannot change whether the classifier
                # runs, and paying for the scan would be pure cost on the
                # common path.
                ballot = ChannelRoutingService._catalog_ballot(
                    db,
                    user,
                    include_catalog=ChannelRoutingService._catalog_may_run(
                        policy, include_catalog=include_catalog
                    ),
                )
                if not ballot.offers_an_alternative:
                    agent, identity = ChannelRoutingService._route_only_candidate(
                        db, user, candidates[0], message=text
                    )
                    return agent, ballot, identity

            result = AgentClassifier.classify(candidates, text)
        except Exception as exc:  # noqa: BLE001 — router outage must not 500 the webhook
            logger.exception("%s Pass 1 routing failed", _LOG_PREFIX)
            routing_trace.record_error(exc)
            return None, None, None

        if result is None or not result.agent_id:
            # ``classify`` already recorded *which* negative outcome this was.
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, ballot, None

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
            return None, ballot, None

        # **Before the UUID parse below, and that ordering is load-bearing.**
        # An identity ref is ``identity:{owner_id}`` — deliberately not a UUID
        # (``identity_candidate_provider``'s module docstring says why), so
        # ``uuid.UUID(result.agent_id)`` would raise on every identity win and
        # the decision would come back as "the classifier returned a value that
        # is not a UUID". The ``except ValueError`` below stays regardless: it
        # is the backstop for a *malformed* ref, not for this one.
        owner_id = parse_identity_ref(result.agent_id)
        if owner_id is not None:
            agent, identity = ChannelRoutingService._route_identity(
                db,
                user,
                owner_id,
                # Stage 1's transformed message, not the raw text: when the
                # classifier stripped a routing prefix ("hey, ask HR ...") the
                # rewrite is what Stage 2 must classify over, or the prefix
                # naming the *person* competes with the wording naming their
                # *agent*. ``None`` whenever nothing was stripped.
                message=result.transformed_message or text,
            )
            return agent, ballot, identity

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
            return None, ballot, None

        agent = db.get(Agent, agent_uuid)
        if agent is None:
            # The row was selected moments ago in this same session; only a
            # concurrent delete gets here.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_AGENT_MISSING
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, ballot, None
        if agent.owner_id != user.id:
            # Unreachable on THIS path — see the docstring. Every candidate
            # that reaches here came from ``WHERE owner_id = :user_id``; the
            # one candidate class that is foreign by design returned above,
            # carrying an ``IdentityGrant``, before this guard was reached. So
            # the guard still means what it always meant: a foreign agent with
            # nothing authorising it.
            #
            # ``result.agent_id`` rather than ``agent.id``: identical values
            # (the ``db.get`` above used it), but a plain dataclass attribute
            # instead of an ORM one, so no argument in this guarded call can
            # reach the database at all.
            routing_trace.mark_candidate_skipped(
                ref_id=result.agent_id, reason=routing_trace.SKIP_FOREIGN_OWNER
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.error(
                "%s Pass 1 matched agent %s owned by %s for sender %s — rejected. "
                "This postcondition is unreachable by construction (non-identity "
                "candidates come from WHERE owner_id = sender, and an identity "
                "candidate returns before this guard); reaching it means the "
                "candidate provider stopped scoping to the sender's own agents",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                user.id,
            )
            return None, ballot, None

        logger.info(
            "%s Pass 1 matched own agent %s for user %s", _LOG_PREFIX, agent.id, user.id
        )
        return agent, ballot, None

    @staticmethod
    def _route_pinned_agent(
        db: DBSession, user: User, pinned_agent_id: uuid.UUID
    ) -> Agent | None:
        """The sender pinned an agent to this channel. Use it, ask nothing.

        **Ordering, which is the whole design of this method.** It runs before
        the candidate provider, before the ``only_one`` short-circuit and
        before Pass 2, and each of those three is a separate decision:

        - **Before the provider.** A pin has already answered the question the
          ballot exists to ask. Building one anyway would cost a query and fill
          the trace with candidates that were never in contention, and it could
          not change the answer — a trace listing four agents under
          ``match_method=pinned`` invites the reader to wonder which of them
          lost, when none of them ran.
        - **Before ``only_one``.** The two look alike from outside (both route
          without a model) and mean opposite things. ``only_one`` is an
          inference from a choice space that happens to hold one thing; a pin
          is an instruction that holds however many agents the sender owns. If
          they could ever disagree the pin must win, and it does — by never
          reaching that branch.
        - **Before Pass 2.** Auto-install exists to onboard a sender who owns
          nothing that matches. A sender who pinned an agent has said where
          their messages go, and installing a catalog bundle over that would be
          the router overruling the person it is routing for. So a pinned
          channel never auto-installs, including for a message the pinned agent
          is a poor fit for — "everything I send here goes to this one" is what
          the pin means, and a router that quietly made exceptions to it would
          be worse than one that never offered it.

        **The scope is deliberately not consulted.** A pin can name an agent
        that the sender's own ``agent_scope`` would exclude, and it still wins.
        Both settings come from the same person on the same screen, and the pin
        is the more specific of the two statements; a pin that lost to its
        neighbour on the same card would be a self-contradiction with nothing
        to show the sender why. It costs nothing in reach: the pin is
        ownership-checked twice (see below), so the widest thing it can do is
        route the sender to an agent they own.

        **It does not skip the trace**, which is the rule this codebase has
        already had to fix once. A pinned decision records the match method,
        the winning candidate and the terminal outcome (settled by the caller),
        so a reader can tell "the classifier never ran because there was a pin"
        from "the classifier ran and chose this". The candidate row is recorded
        as **eligible**, unlike the availability-only Pass-2 rows: it was the
        ballot, in full, and it won — ``eligible=True`` feeds the "chosen from
        N eligible candidates" arithmetic and here N genuinely is one.

        **Ownership is re-checked, though it was already checked.**
        ``ChannelPolicyService._owned_pin`` nulls a pin whose agent the sender
        no longer owns, so this guard is unreachable through that path — the
        same status as the ``owner_id`` guard in :meth:`_route_installed`, and
        kept for the same reason: this hands an external sender a session
        inside a workspace, and two lines of assertion is a cheap price for the
        one that is not reachable *yet*. Unlike that guard, this one protects
        an id that came in as **data on a settings row** rather than out of a
        ``WHERE owner_id = :user_id``, so the property it asserts is one hop
        further from where it is established.
        """
        from app.services.routing.channel_candidate_provider import (
            SOURCE_OWNED,
            example_prompt_text,
        )

        routing_trace.record_match(method=routing_trace.MATCH_PINNED)

        agent = db.get(Agent, pinned_agent_id)
        if agent is None:
            # ``record_skip``, not ``mark_candidate_skipped``: no provider ran,
            # so there is no recorded candidate to flip and the flip would be a
            # silent no-op — a pinned decision that failed with an empty trace,
            # which is the exact shape this method's docstring refuses. The
            # name is blank because the row is gone; the id is the diagnosis.
            routing_trace.record_skip(
                kind=routing_trace.KIND_AGENT,
                ref_id=pinned_agent_id,
                name="",
                reason=routing_trace.SKIP_AGENT_MISSING,
                source=SOURCE_OWNED,
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.info(
                "%s Pinned agent %s for user %s no longer exists — no match",
                _LOG_PREFIX,
                pinned_agent_id,
                user.id,
            )
            return None

        # Read every attribute once, off the instance ``db.get`` just
        # materialised, before any of them reaches a recorder's argument list.
        # §11a Rule 2 — the same sweep the candidate provider and Pass 2 apply.
        agent_id = agent.id
        agent_name = agent.name or ""
        agent_owner_id = agent.owner_id
        trigger = (agent.router_trigger_prompt or "").strip()
        examples = example_prompt_text(agent.example_prompts)

        if agent_owner_id != user.id:
            routing_trace.record_skip(
                kind=routing_trace.KIND_AGENT,
                ref_id=agent_id,
                name=agent_name,
                reason=routing_trace.SKIP_FOREIGN_OWNER,
                source=SOURCE_OWNED,
                trigger_prompt=trigger,
                prompt_examples=examples,
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.error(
                "%s Pinned agent %s is owned by %s, not by sender %s — rejected. "
                "ChannelPolicyService._owned_pin should already have cleared "
                "this pin; reaching here means the pin's ownership check and "
                "routing's have come apart",
                _LOG_PREFIX,
                agent_id,
                agent_owner_id,
                user.id,
            )
            return None

        # The ballot, in full: one candidate, and it is the answer. Recorded
        # with its wording so the trace shows what the pinned agent actually
        # carries — useful precisely when the pin is the thing being questioned
        # ("it went here again even though I asked about X").
        routing_trace.record_candidate(
            kind=routing_trace.KIND_AGENT,
            ref_id=agent_id,
            name=agent_name,
            source=SOURCE_OWNED,
            trigger_prompt=trigger,
            prompt_examples=examples,
        )
        logger.info(
            "%s Pass 1 routed to pinned agent %s for user %s without "
            "classifying (the sender pinned it to this channel)",
            _LOG_PREFIX,
            agent_id,
            user.id,
        )
        return agent

    @staticmethod
    def _route_only_candidate(
        db: DBSession, user: User, candidate: Candidate, *, message: str
    ) -> tuple[Agent | None, IdentitySelection | None]:
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

        **The sole candidate can be an identity, and this is not the rare
        shape.** A sender who owns no agents and can reach exactly one identity
        owner produces a one-candidate ballot, and with an empty auto-install
        list the probe short-circuits here without the classifier ever running
        — which is the HR story at its simplest. So Stage 2 is dispatched from
        here as well as from the classifier branch, over the raw ``message``:
        no classifier ran, so there is no transformed message to prefer.

        Re-loads the agent by id in this session rather than trusting the
        candidate: the ballot is a projection built moments ago, and this is the
        same ``db.get`` the classifier branch does. The two guards below are the
        same two it applies, for the same reasons — a concurrent delete is
        genuinely reachable, and the ownership check is defence in depth against
        a candidate provider that stopped scoping. Both are on the
        **non-identity** path only; the identity dispatch above them carries its
        own postcondition (:meth:`_route_identity`).
        """
        from app.services.routing.identity_candidate_provider import (
            parse_identity_ref,
        )

        routing_trace.record_match(method=routing_trace.MATCH_ONLY_ONE)

        # Before the parse below, for the reason the classifier branch states:
        # an identity ref is not a UUID, so parsing it as one would report the
        # HR story as a malformed classifier answer. Stage 2 may record a match
        # method of its own over the ``only_one`` just written, which is the
        # documented behaviour — the trace's decision-level ``match_method``
        # reports how the *last* stage matched.
        owner_id = parse_identity_ref(candidate.ref_id)
        if owner_id is not None:
            return ChannelRoutingService._route_identity(
                db, user, owner_id, message=message
            )

        try:
            agent_uuid = uuid.UUID(candidate.ref_id)
        except ValueError:
            # Unreachable — the provider builds every non-identity ``ref_id``
            # from an ``Agent.id``. Carries a reason anyway, matching the
            # classifier path's guard: a bare ``no_match`` under
            # ``match_method=only_one`` would be the one outcome shape with
            # nothing at all explaining it.
            routing_trace.record_parse_outcome(
                reason="the sole candidate's id is not a UUID"
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, None

        agent = db.get(Agent, agent_uuid)
        if agent is None:
            routing_trace.mark_candidate_skipped(
                ref_id=candidate.ref_id, reason=routing_trace.SKIP_AGENT_MISSING
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, None
        if agent.owner_id != user.id:
            routing_trace.mark_candidate_skipped(
                ref_id=candidate.ref_id, reason=routing_trace.SKIP_FOREIGN_OWNER
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.error(
                "%s Pass 1 short-circuited to agent %s owned by %s for sender %s "
                "— rejected. Unreachable by construction (non-identity candidates "
                "come from WHERE owner_id = sender, and an identity candidate "
                "returns before this guard)",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                user.id,
            )
            return None, None

        logger.info(
            "%s Pass 1 routed to sole eligible agent %s for user %s without "
            "classifying (Pass 2 had nothing to offer, or policy barred it)",
            _LOG_PREFIX,
            agent.id,
            user.id,
        )
        return agent, None

    @staticmethod
    def _route_identity(
        db: DBSession, user: User, owner_id: uuid.UUID, *, message: str
    ) -> tuple[Agent | None, IdentitySelection]:
        """Stage 2 — Stage 1 chose a person; pick one of *their* agents.

        Always returns an :class:`IdentitySelection`, because "the sender
        addressed a person" is true of every branch here and is what stops
        ``decide`` reaching for Pass 2. The ``Agent`` beside it is ``None``
        whenever Stage 2 selected nothing, which is an ordinary ``no_match``
        for the whole decision rather than an error: the sender gets the
        existing "couldn't find an agent" reply, and auto-installing a catalog
        bundle for somebody who asked to speak to a colleague is not a better
        answer.

        **Two stages, and the second one does not recurse.** Stage 2 chooses
        among *agents* — an identity's bindings name agents, never further
        identities — so there is no depth to count and no cycle to guard
        (master plan §2.10). A counter here would imply chains are supported.

        **Written under its own stage.** ``stage_scope`` rather than
        ``begin_stage``: this is a handoff that returns, and a latched stage
        would file everything recorded afterwards — including this method's own
        rejections, which are Pass-1 facts — under ``identity_stage2``. Same
        call shape the App MCP path uses, for the same reason its comment gives.

        **Total, like Pass 1's own catch-all.** Stage 2 ends in a blocking LLM
        call whenever the caller can reach two or more of the owner's agents,
        so it has exactly the outage exposure ``_route_installed`` wraps its
        candidate build and classification in — and this method is reached from
        the classifier branch, which is *outside* that try. An outage here is
        recorded on the trace and declines; it does not 500 an externally
        triggerable webhook.

        **Its postcondition is the identity twin of the ownership guard.** A
        channel session may run on a foreign agent only when a grant names the
        binding behind it, so the agent Stage 2 returns must belong to the
        person Stage 1 chose. ``IdentityService`` enforces that on write and
        ``ChannelIngestionService.assert_access`` re-reads it before any session
        opens; this is the middle check, and it is what keeps
        :class:`RoutingDecisionResult`'s ``agent_id`` and ``identity_grant``
        describing the same person.
        """
        from app.services.identity.identity_routing_service import (
            IdentityRoutingService,
        )

        selection = IdentitySelection(owner_id=owner_id)
        try:
            with routing_trace.stage_scope(routing_trace.STAGE_IDENTITY_STAGE2):
                result = IdentityRoutingService.route_within_identity(
                    owner_id=owner_id,
                    caller_user_id=user.id,
                    message=message,
                )
        except Exception as exc:  # noqa: BLE001 — see "Total" above
            logger.exception(
                "%s Identity Stage 2 failed for owner %s (sender %s)",
                _LOG_PREFIX,
                owner_id,
                user.id,
            )
            routing_trace.record_error(exc)
            return None, selection

        if result is None:
            # Stage 2 recorded its own ballot and skips; what it cannot record
            # is that the decision ended here, because it does not know it was
            # the last stage.
            logger.info(
                "%s Identity Stage 2 selected nothing for owner %s (sender %s)",
                _LOG_PREFIX,
                owner_id,
                user.id,
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, selection

        agent = db.get(Agent, result.agent_id)
        if agent is None:
            # Stage 2 read this row in a session of its own that is already
            # closed; only a delete in between gets here.
            routing_trace.mark_candidate_skipped(
                ref_id=str(result.agent_id), reason=routing_trace.SKIP_AGENT_MISSING
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            return None, selection
        if agent.owner_id != owner_id:
            # Unreachable by construction — ``IdentityService.create_binding``
            # refuses an agent the owner does not own, and ``assert_access``
            # re-asserts ``binding.owner_id == agent.owner_id == owner_id``.
            # Kept for the same reason its sibling guards are: this is the path
            # that hands an external sender a session inside somebody else's
            # workspace, and the grant below would name a binding that does not
            # describe the agent beside it.
            routing_trace.mark_candidate_skipped(
                ref_id=str(result.agent_id), reason=routing_trace.SKIP_FOREIGN_OWNER
            )
            routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
            logger.error(
                "%s Identity Stage 2 selected agent %s owned by %s, but Stage 1 "
                "chose identity owner %s — rejected. Unreachable by construction "
                "(a binding may only name its owner's own agent)",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                owner_id,
            )
            return None, selection

        logger.info(
            "%s Identity routing: sender %s reached agent %s in %s's workspace",
            _LOG_PREFIX,
            user.id,
            agent.id,
            owner_id,
        )
        return agent, IdentitySelection(
            owner_id=owner_id,
            grant=IdentityGrant(
                owner_id=owner_id,
                binding_id=result.binding_id,
                assignment_id=result.binding_assignment_id,
            ),
        )

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

        Note the parameter is narrower than its name: what the caller passes is
        :meth:`_catalog_may_run`'s answer — the admin's toggle **and** the
        channel's ``allow_auto_install`` **and** its agent scope — not
        ``decide``'s raw flag. They stopped being the same value when channel
        policy landed, and it is the whole conjunction that has to reach here:
        a probe that saw only the toggle would report a choice space of two on
        a channel where Pass 2 can never run, and the short-circuit would
        classify where the real path routes directly.

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
    def _record_pass_2_not_run(policy: ResolvedChannelPolicy) -> None:
        """Write *why* Pass 2 will not run into the trace, when policy is why.

        A no-op unless one of the three policy terms in :meth:`_catalog_may_run`
        is what stops it, so the call site stays one unconditional line and
        every condition lives with the reason it belongs to.

        **One note per cause, and the causes must stay distinguishable.** The
        three are fixed on different screens — the sender's own pin, the agent
        scope, and the admin's ``allow_auto_install`` — so a shared "policy
        said no" would be a diagnosis that names no control. Checked
        narrowest-first: a pin answers the routing question outright, an agent
        scope narrows this channel for this sender, and ``allow_auto_install``
        is the channel-wide admin default underneath both.

        Note that only the first and last of those have a single owner. The
        agent scope **inherits**, so it is the sender's own setting or the
        channel's admin default depending on whether a
        ``channel_user_setting`` row says so, and this module cannot tell:
        ``ResolvedChannelPolicy`` deliberately carries no provenance. Hence
        :data:`PASS_2_SCOPE_RESTRICTED_NOTE` states the fact without claiming
        an owner, and the reachability verdict — which has a session and calls
        ``ChannelPolicyService.describe`` — is where the screen gets named.

        Scope is reported ahead of ``allow_auto_install`` when both bar the
        pass, and that order is the diagnosis rather than a coin toss. A
        restricted scope invalidates the *other* remedy too: under it, giving
        this sender a new agent with a trigger prompt does not make them
        routable either, because the new agent would be out of scope as well.
        Naming the auto-install switch first would send the reader to fix one
        thing and come back to a channel that still routes nothing.

        **Silent when Pass 1 already recorded an error.** On that branch the
        pass blew up before it decided anything, and the truthful answer to
        "why did Pass 2 not run" is "Pass 1 failed first" — stamping a policy
        note over it would assert a reason that was never reached. The error is
        the story; this note would be a second, quieter claim competing with it.

        **Why this writes a stage for a pass that never ran**, which
        :meth:`_record_catalog_ballot` refuses to do a few lines up and which
        ``routing_trace``'s docstring makes a rule of ("a pass that ran always
        leaves a stage"). The refusal there is about *noise*: an empty
        ``pass_2`` heading on every message from every sender on a server with
        an empty auto-install list, saying only that there was nothing to look
        at. This one is the opposite — it is written only when Pass 1 found
        nothing *and* this sender's own policy is what stopped the catalog,
        which is precisely the trace somebody is reading to find out why they
        were never offered anything, and the answer is not visible anywhere
        else on the row. The stage is the finding, not a heading over one.

        **Each note is written alongside a code**, and the pair is the whole
        arrangement. With ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` off the note is
        still withheld — ``reason`` is not on ``routing_trace``'s allowlist and
        should not be, since it is the field the classifier's own prose lands
        in — but ``not_run_code`` is, so the fact the note carries survives the
        gate while the sentence does not. That fact is precisely *which switch
        to go and look at*: without it this stage read, gate closed,
        indistinguishable from a Pass 2 that ran over an empty catalog.

        The code is assigned **inside this same if/elif chain**, never
        recomputed from ``policy`` afterwards. The narrowest-first ordering
        above is the diagnosis, and a second independent pass over the same
        three terms is how a code ends up naming a different control from the
        sentence beside it.
        """
        trace = routing_trace.current()
        if trace is not None and trace.error:
            return
        if policy.pinned_agent_id is not None:
            note = PASS_2_PINNED_NOTE
            code = routing_trace.NOT_RUN_PINNED
        elif policy.agent_scope != CHANNEL_AGENT_SCOPE_ALL:
            note = PASS_2_SCOPE_RESTRICTED_NOTE
            code = routing_trace.NOT_RUN_CHANNEL_SCOPE
        elif not policy.allow_auto_install:
            note = PASS_2_NOT_ALLOWED_NOTE
            code = routing_trace.NOT_RUN_AUTO_INSTALL_OFF
        else:
            return
        with routing_trace.stage_scope(routing_trace.STAGE_PASS_2):
            routing_trace.record_parse_outcome(reason=note, not_run_code=code)

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

