"""App MCP Routing Service — determines which agent should handle a message.

Stage 1's ballot is **composed** from the same two candidate providers every
other surface composes, in the same order:

- ``ChannelCandidateProvider`` — the agents the caller owns, narrowed to the
  App MCP channel's resolved agent scope.
- ``IdentityCandidateProvider`` — the *people* the caller can address, one
  candidate per identity owner, and only when the caller has switched identity
  routing on for this channel.

That composition is the whole point of the phase that produced it: App MCP is a
``ServerChannel`` row like any other, so "what may this person address here" is
answered by ``ChannelPolicyService`` and nothing else, and the answer is
identical to the one a Google Chat sender gets. The ``AppAgentRoute`` family —
admin routes, assignments, personal routes, ``is_auto_managed`` auto-creation,
the backfill — is deleted; ``Agent.router_trigger_prompt`` plus
``Agent.example_prompts`` are the sole routing source of truth.

**Ordering is load-bearing and is copied from
``ChannelRoutingService._route_installed``**: owned agents first, identities
after — the common case before the exceptional one, so the trace and the prompt
read top-down. Nothing turns on it (both providers sort internally and the
classifier is handed a set), but the two surfaces are asserted to produce
identical candidate lists for the same user, and ordering is part of that.

Routing priority is now two steps, not three:
  1. AI classification over the whole ballot (short-circuited when the ballot
     holds exactly one candidate)
  2. Return None if no match found

Glob pre-matching is gone with ``message_patterns`` (settled decision §2.9): a
second, silently-higher-priority routing mechanism that no trace explained
well. ``routing_trace.MATCH_PATTERN`` survives as a *rendering* constant —
historical ``routing_decision`` rows still carry it — exactly as
``SKIP_IDENTITY_ROUTE`` did.

**``allow_identity_routing`` defaults false and never inherits** (master plan
§3.4), so identity contacts stop appearing on App MCP until the caller opts in
from Settings → Channels. That is a deliberate, ruled behaviour change, not an
oversight: routing into another person's workspace is opt-in, per person, by
the person whose message it is, and an admin default must not consent on their
behalf.

**Not implemented here: pinned agents.** ``ResolvedChannelPolicy.pinned_agent_id``
is honoured by ``ChannelRoutingService``, not by the candidate provider, so it
would need its own arm on this path. Deliberately out of scope.

When the winner is an identity candidate, Stage 2
(``IdentityRoutingService.route_within_identity``) picks the agent and returns
the binding + assignment ids that become the ``IdentityGrant``
``ChannelIngestionService.assert_access`` re-verifies before any session opens.
"""
import logging
import uuid
from dataclasses import dataclass

from sqlmodel import Session as DBSession

from app.core.config import ROUTING_TRACE_APP_MCP_OFF, settings
from app.services.routing import routing_trace
from app.services.routing.agent_classifier import Candidate
from app.services.routing.channel_candidate_provider import (
    SOURCE_OWNED,
    ChannelCandidateProvider,
)
from app.services.routing.identity_candidate_provider import (
    SOURCE_IDENTITY,
    IdentityCandidateProvider,
    parse_identity_ref,
)
from app.services.routing.routing_trace import RoutingTrace
from app.services.routing.routing_trace_service import RoutingTraceService
from app.services.server_channels.adapters.app_mcp import AppMCPChannelAdapter
from app.services.server_channels.channel_policy_service import (
    ChannelPolicyService,
    ResolvedChannelPolicy,
)
from app.services.server_channels.server_channel_service import ServerChannelService

logger = logging.getLogger(__name__)

#: ``RoutingResult.session_mode`` for a candidate this surface picked itself.
#: There is no per-candidate session mode any more — it used to be a column on
#: the route row, and every auto-created route carried this value — so the
#: owned-agent path now uses the platform default, which is also what a channel
#: session gets. Only Stage 2 supplies a real one, off
#: ``IdentityAgentBinding.session_mode``, and that field is still owner-editable.
DEFAULT_SESSION_MODE = "conversation"


@dataclass(frozen=True)
class IdentityPick:
    """Stage 1's answer when the winner is a person rather than an agent.

    ``agent_name`` duplicating ``identity_owner_name`` is inherited rather than
    invented: ``_route_identity`` still reads them as ``owner_name or
    agent_name``, and ``IdentityCandidateProvider`` collapses both of the old
    fallbacks into one ``Candidate.name``, so today they are always equal.
    """

    identity_owner_id: uuid.UUID
    identity_owner_name: str
    agent_name: str


@dataclass
class RoutingResult:
    """Result of routing a message to an agent.

    ``source`` is ``"owned"`` or ``"identity"`` — the two candidate providers'
    own ``CandidateTrace.source`` strings, reused rather than re-spelled so a
    result and the trace row behind it cannot disagree about where a candidate
    came from. It replaces ``route_source`` (``"admin"``/``"user"``/
    ``"identity"``), whose first two values named where a route row came from
    and no longer describe anything.
    """

    agent_id: uuid.UUID
    agent_name: str
    session_mode: str
    source: str  # SOURCE_OWNED | SOURCE_IDENTITY
    match_method: str  # "ai" | "only_one"
    # Identity-specific fields (only set when source == SOURCE_IDENTITY)
    is_identity: bool = False
    identity_owner_id: uuid.UUID | None = None
    identity_owner_name: str | None = None
    identity_stage2_match_method: str | None = None
    identity_binding_id: uuid.UUID | None = None
    identity_binding_assignment_id: uuid.UUID | None = None
    # Message transformation (only set when AI routing stripped a routing prefix)
    transformed_message: str | None = None


class AppMCPRoutingService:
    """Routes App MCP messages to the appropriate agent."""

    @staticmethod
    def route_message(
        db_session: DBSession,
        user_id: uuid.UUID,
        message: str,
    ) -> RoutingResult | None:
        """Determine which agent should handle a message.

        1. Resolve the caller's policy on the App MCP channel.
        2. Compose the ballot: the agents they own + (opt-in) the people they
           can address.
        3. Classify over the whole ballot.
        4. If an identity candidate won, invoke Stage 2 routing.

        Returns RoutingResult or None if routing fails.

        Steps 2–4 are :meth:`_decide`; this method resolves the channel and
        policy, then wraps that call in the ``origin="app_mcp"`` routing
        capture and persists it. How much of the resulting row is written is
        governed by ``ROUTING_TRACE_APP_MCP_MODE`` — ``off`` opens no capture
        here at all, ``metadata`` (the default) writes the row without the
        sender's words, ``full`` writes it like any other origin. The setting's
        comment in ``config.py`` lists the fields each value stores and omits.

        ``policy.is_available`` is **not** re-checked here. It is the token
        verifier's gate (``app_token_verifier.is_app_mcp_available``, cached and
        fail-closed), and a second copy of an availability rule is exactly what
        ``ChannelPolicyService``'s docstring forbids: two readers of the same
        conjunction drift, and the one that drifts silently is the one nobody
        is testing.
        """
        # ``get_or_create_singleton`` commits when it has to materialize the
        # row, and it commits the *whole* transaction — so its precondition is
        # that the caller has nothing staged on ``db_session``. That holds
        # here: the handler has only read (a resume lookup and an agent get)
        # before routing. In practice the row already exists by now anyway —
        # every App MCP request passes ``AppMCPTokenVerifier`` first, and that
        # asks the same question through the same function.
        channel = ServerChannelService.get_or_create_singleton(
            db_session, AppMCPChannelAdapter.channel_type
        )
        policy = ChannelPolicyService.resolve(db_session, channel, user_id)

        # ``off`` short-circuits HERE: no capture is opened at all, which is
        # what "suppresses the capture" literally means. It costs nothing —
        # every recorder call inside :meth:`_decide` (``record_match``,
        # ``record_parse_outcome``, ``stage_scope``) is already a no-op with no
        # capture open — and, more to the point, it attempts nothing, so
        # nothing can half-succeed. ``persist`` swallows its own failures and
        # returns ``None``, so "suppressed" and "attempted and quietly failed"
        # would be indistinguishable from outside if the mode were honoured
        # only at the write. It is honoured at both ends deliberately: this is
        # the cheap path, and ``RoutingTraceService.persist``'s refusal is the
        # invariant a second App MCP producer cannot bypass.
        if settings.ROUTING_TRACE_APP_MCP_MODE == ROUTING_TRACE_APP_MCP_OFF:
            return AppMCPRoutingService._decide(
                db_session, user_id, message, policy=policy
            )

        # The capture opens after the policy resolve and **before** the first
        # candidate provider runs, so every candidate and every skip those
        # providers record lands inside the span. ``channel_id`` is populated
        # on every App MCP trace: App MCP is a singleton ``ServerChannel``, so
        # "which channel was this" has a real answer here — unlike a hand-typed
        # simulate, which carries NULL by design.
        #
        # ``actor_user_id`` and ``thread_key`` are NULL and stay NULL: there is
        # no admin standing behind an App MCP call (that field is simulate's)
        # and this surface has no thread key — the MCP ``context_id`` is a
        # session id, resolved by the handler after routing has already
        # finished, so it is not this decision's to record.
        trace: RoutingTrace | None = None
        try:
            with RoutingTrace.capture(
                origin=routing_trace.ORIGIN_APP_MCP,
                user_id=user_id,
                channel_id=channel.id,
                actor_user_id=None,
                thread_key=None,
                message=message,
                stage=routing_trace.STAGE_PASS_1,
            ) as trace:
                result = AppMCPRoutingService._decide(
                    db_session, user_id, message, policy=policy
                )
                if result is not None:
                    trace.record_outcome(
                        routing_trace.OUTCOME_ROUTED,
                        selected_agent_id=result.agent_id,
                    )
        except Exception:
            # ``capture`` stamps ``outcome="error"`` and re-raises unchanged, so
            # this trace never reaches the persist below. Written here, inside
            # the failing path, exactly as both channel passes do
            # (``ChannelRoutingService._route_installed_in_thread`` and
            # ``_route_catalog_in_thread``) — otherwise ``outcome="error"``
            # becomes the one verdict no App MCP code path can produce, and
            # ``?origin=app_mcp&outcome=error``, the filter that exists for
            # precisely this, stays empty while App MCP is failing.
            #
            # Deliberately no ``db_session.rollback()`` first, for the reason
            # set out at length at the channel sites: ``persist`` opens its own
            # session and cannot observe this one, and a ``rollback()`` that
            # raises inside an ``except`` replaces the original exception and
            # loses the error row — the diagnostic breaking the thing it
            # observes.
            RoutingTraceService.persist(trace)
            raise
        # Happy path, persisted after the block exits so ``latency_ms`` and the
        # settled outcome are on the trace. Persisted **inline** rather than
        # handed back: ``_decide`` is fully synchronous with no ``await``, so
        # the ContextVar is set and reset inside one frame and cannot leak
        # across a suspension, and ``route_message``'s ``RoutingResult | None``
        # signature stays what its caller expects. Unlike channel routing there
        # is no second pass to merge with, so there is nobody else who could
        # know better when to write.
        RoutingTraceService.persist(trace)
        return result

    @staticmethod
    def _decide(
        db_session: DBSession,
        user_id: uuid.UUID,
        message: str,
        *,
        policy: ResolvedChannelPolicy,
    ) -> RoutingResult | None:
        """The decision itself — ballot, classify, Stage 2 handoff.

        Split out of :meth:`route_message` so the routing trace's capture span
        can wrap the whole of it, including its several early returns, without
        a hundred-line ``with`` block. Every ``return None`` below is a
        ``no_match`` the recorder settles on its own (``capture`` finishes an
        unsettled trace as ``no_match``), so the caller records only the
        positive verdict.

        Takes the already-resolved ``policy`` rather than resolving its own:
        the channel row and the policy are the caller's, because the capture
        needs ``channel.id`` before this runs.
        """
        # Owned agents first, identities after — mirroring
        # ``ChannelRoutingService._route_installed`` exactly. See the module
        # docstring on why the order is copied rather than re-decided.
        candidates = ChannelCandidateProvider.build(db_session, user_id, policy=policy)
        owned_count = len(candidates)
        if policy.allow_identity_routing:
            candidates += IdentityCandidateProvider.build(db_session, user_id)
        # With the switch off the provider is simply not called, so identity
        # owners this caller *could* have reached leave no trace rows at all,
        # not even skips. That is the same deliberate inversion of master plan
        # §3.5 the channel path makes, for the same reason: recording them
        # would publish the existence of other people's identities into a trace
        # the caller can trigger at will. Do not "fix" it by building the
        # candidates and filtering them after.

        if not candidates:
            logger.debug("No routing candidates for user %s", user_id)
            return None

        # Debug, not info: this line carries the caller's message text and
        # every candidate's trigger prompt. The routing trace is where that
        # detail belongs.
        logger.debug(
            "[Stage1] Routing message for user=%s | message=%r | "
            "%d candidates (%d owned, %d identity)",
            user_id, message[:120], len(candidates),
            owned_count, len(candidates) - owned_count,
        )

        stage1_transformed_message: str | None = None
        selected: Candidate | IdentityPick

        # If only one candidate in total, use it directly (no need to classify)
        if len(candidates) == 1:
            only = candidates[0]
            # Positional, not by value: ``Candidate`` is an unfrozen dataclass
            # whose ``__eq__`` compares fields, so membership testing could
            # match the wrong provider's candidate. The identity half is
            # everything appended after the owned block.
            if owned_count == 0:
                only_identity = AppMCPRoutingService._identity_pick(only)
                if only_identity is None:
                    logger.warning(
                        "[Stage1] Sole candidate has a malformed identity ref — "
                        "refusing to route (user=%s)", user_id,
                    )
                    return None
                selected = only_identity
            else:
                selected = only
            stage1_method = "only_one"
            routing_trace.record_match(method=routing_trace.MATCH_ONLY_ONE)
            logger.info(
                "[Stage1] Single candidate — using directly: %s",
                selected.name if isinstance(selected, Candidate) else selected.agent_name,
            )
        else:
            ai_result = AppMCPRoutingService._ai_classify(candidates, message)
            if ai_result is None:
                logger.info("[Stage1] AI classification returned no match (user=%s)", user_id)
                return None
            selected, stage1_transformed_message = ai_result
            stage1_method = "ai"
            logger.debug(
                "[Stage1] AI selected: %s | transformed_message=%r",
                selected.name if isinstance(selected, Candidate) else selected.agent_name,
                stage1_transformed_message[:120] if stage1_transformed_message else None,
            )

        is_identity = isinstance(selected, IdentityPick)
        selected_name = selected.agent_name if is_identity else selected.name
        logger.info(
            "[Stage1] Result: method=%s agent=%s is_identity=%s",
            stage1_method, selected_name, is_identity,
        )

        # Stage 2: If the selected candidate is a person, invoke identity routing
        if is_identity:
            logger.debug(
                "[Stage1→Stage2] Identity candidate won — handing off to Stage 2 | "
                "identity_owner=%s (%s) | stage2_input=%r",
                selected.identity_owner_name, selected.identity_owner_id,
                (stage1_transformed_message or message)[:120],
            )
            # ``stage_scope``, not ``begin_stage``. Stage 2 is a handoff that
            # RETURNS, and ``begin_stage`` latches: it used to leave
            # ``identity_stage2`` current for everything recorded after this
            # call, including the caller's own Pass-1 rejection of the identity
            # route (``ChannelRoutingService._route_installed``'s
            # ``SKIP_IDENTITY_ROUTE`` candidate, a pass_1 fact). Confirmed by
            # running the sequence, not by reading it. Phase 4 groups candidates
            # by stage and would have rendered that row under the wrong heading
            # with nothing in the payload to give it away.
            with routing_trace.stage_scope(routing_trace.STAGE_IDENTITY_STAGE2):
                result = AppMCPRoutingService._route_identity(
                    selected_identity=selected,
                    caller_user_id=user_id,
                    message=message,
                    stage1_method=stage1_method,
                    transformed_message=stage1_transformed_message,
                )
            if result:
                logger.debug(
                    "[Stage1→Stage2] Final routing result: agent=%s (%s) | "
                    "stage1_method=%s stage2_method=%s | final_message=%r",
                    result.agent_name, result.agent_id,
                    result.match_method, result.identity_stage2_match_method,
                    (result.transformed_message or message)[:120],
                )
            else:
                logger.info("[Stage1→Stage2] Stage 2 returned no result — routing failed")
            return result

        try:
            agent_id = uuid.UUID(selected.ref_id)
        except ValueError:
            # Unreachable by construction — ``ChannelCandidateProvider`` writes
            # ``str(agent.id)`` — and handled rather than asserted because the
            # alternative is handing a non-UUID on to ``db.get(Agent, ...)``.
            logger.warning("[Stage1] Owned candidate has a non-UUID ref: %r", selected.ref_id)
            return None

        return RoutingResult(
            agent_id=agent_id,
            agent_name=selected.name,
            session_mode=DEFAULT_SESSION_MODE,
            source=SOURCE_OWNED,
            match_method=stage1_method,
            transformed_message=stage1_transformed_message,
        )

    @staticmethod
    def _identity_pick(candidate: Candidate) -> IdentityPick | None:
        """Turn an identity ``Candidate`` back into Stage 1's answer shape.

        ``None`` when the ref is not a well-formed identity ref — which cannot
        happen for a candidate this service built, and is handled rather than
        asserted because the alternative is treating a namespaced string as an
        agent id somewhere further down.
        """
        owner_id = parse_identity_ref(candidate.ref_id)
        if owner_id is None:
            return None
        return IdentityPick(
            identity_owner_id=owner_id,
            identity_owner_name=candidate.name,
            agent_name=candidate.name,
        )

    @staticmethod
    def _route_identity(
        selected_identity: "IdentityPick",
        caller_user_id: uuid.UUID,
        message: str,
        stage1_method: str,
        transformed_message: str | None = None,
    ) -> RoutingResult | None:
        """Invoke Stage 2 routing for an identity contact.

        Returns a RoutingResult with identity fields populated,
        or None if Stage 2 cannot find an accessible agent.

        The transformed_message from Stage 1 (if any) is passed as the message
        to Stage 2, so each stage strips one layer of routing prefixes.

        No ``db_session`` is forwarded, and none is taken: Stage 2 opens its own
        short-lived read session so it can be called from a context that must
        not hand its transaction to a routing decision (see that module's
        docstring, fact 1).

        ``binding_id`` and ``binding_assignment_id`` come back non-null on every
        Stage 2 success — the service aborts rather than returning a result with
        a missing assignment — and they are what
        ``AppMCPRequestHandler._create_identity_session`` turns into the
        ``IdentityGrant`` that ``assert_access`` re-verifies. They are not
        decoration on this result; they are the authorization.
        """
        from app.services.identity.identity_routing_service import IdentityRoutingService

        owner_id = selected_identity.identity_owner_id
        owner_name = selected_identity.identity_owner_name or selected_identity.agent_name

        # Pass Stage 1's transformed message to Stage 2 if available
        stage2_input_message = transformed_message or message

        stage2_result = IdentityRoutingService.route_within_identity(
            owner_id=owner_id,
            caller_user_id=caller_user_id,
            message=stage2_input_message,
        )

        if not stage2_result:
            logger.debug(
                "[AppMCPRouting] Stage 2 returned no result for identity owner=%s caller=%s",
                owner_id,
                caller_user_id,
            )
            return None

        # Cascade: Stage 2 transformation takes precedence; fall back to Stage 1; else None
        final_transformed = stage2_result.transformed_message or transformed_message

        return RoutingResult(
            agent_id=stage2_result.agent_id,
            agent_name=owner_name,  # Return person's name, not internal agent name
            session_mode=stage2_result.session_mode,
            source=SOURCE_IDENTITY,
            match_method=stage1_method,
            is_identity=True,
            identity_owner_id=owner_id,
            identity_owner_name=owner_name,
            identity_stage2_match_method=stage2_result.match_method,
            identity_binding_id=stage2_result.binding_id,
            identity_binding_assignment_id=stage2_result.binding_assignment_id,
            transformed_message=final_transformed,
        )

    @staticmethod
    def _ai_classify(
        candidates: list[Candidate],
        message: str,
    ) -> tuple["Candidate | IdentityPick", str | None] | None:
        """Classify the message against the composed ballot.

        Both providers already build :class:`Candidate` objects — the one
        candidate shape every routing consumer uses — so there is nothing to
        convert here. ``AgentClassifier`` owns prompt rendering
        (``prompt_examples`` included), parsing and trace emission.

        Returns (matched_candidate_or_identity, transformed_message) or None.
        The transformed_message is None when the AI did not strip a routing
        prefix.
        """
        from app.services.routing.agent_classifier import AgentClassifier

        routing_result = AgentClassifier.classify(candidates, message)

        if not routing_result:
            return None

        # Identity first: its ref is namespaced, so it can never be mistaken
        # for an agent id — and this check must come before the UUID parse,
        # which would otherwise reject it as malformed.
        owner_id = parse_identity_ref(routing_result.agent_id)
        if owner_id is not None:
            for candidate in candidates:
                if candidate.ref_id == routing_result.agent_id:
                    pick = AppMCPRoutingService._identity_pick(candidate)
                    if pick is not None:
                        routing_trace.record_match(method=routing_trace.MATCH_AI)
                        return pick, routing_result.transformed_message
            logger.warning(
                "AI router returned identity %s not among the identity candidates",
                routing_result.agent_id,
            )
            routing_trace.record_parse_outcome(
                reason="classifier picked an identity that is not on the ballot"
            )
            return None

        for candidate in candidates:
            if candidate.ref_id == routing_result.agent_id:
                routing_trace.record_match(method=routing_trace.MATCH_AI)
                return candidate, routing_result.transformed_message

        logger.warning(
            "AI router returned agent_id %s not among the candidates",
            routing_result.agent_id,
        )
        routing_trace.record_parse_outcome(
            reason="classifier picked an agent that is not among the candidates"
        )
        return None
