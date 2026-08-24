"""
App MCP Routing Service — determines which agent should handle a message.

Stage 1's ballot is **composed** from two candidate providers rather than built
by one query:

- ``AppAgentRouteService.get_effective_routes_for_user`` — the routes this user
  can address (admin-assigned + personal). Dies with the ``AppAgentRoute``
  family later in the same refactor.
- ``IdentityCandidateProvider`` — the *people* this user can address, one
  candidate per identity owner. Extracted out of the route service so any
  surface can offer identity without inheriting the App MCP enablement toggles
  that come with a route (channels/identity unification, phase 1 §2.1/§2.3).

Routing priority is unchanged:
  1. Pattern matching (fnmatch globs on a route; an identity candidate has
     none, exactly as the identity *route* never had any)
  2. AI classification over both providers' candidates together
  3. Return None if no match found

When the winner is an identity candidate, Stage 2
(``IdentityRoutingService.route_within_identity``) picks the agent.
"""
import fnmatch
import logging
import uuid
from dataclasses import dataclass

from sqlmodel import Session as DBSession

from app.services.app_mcp.app_agent_route_service import (
    AppAgentRouteService,
    EffectiveRoute,
)
from app.services.routing import routing_trace
from app.services.routing.agent_classifier import Candidate
from app.services.routing.identity_candidate_provider import (
    IdentityCandidateProvider,
    parse_identity_ref,
)

logger = logging.getLogger(__name__)

#: ``RoutingResult.route_id`` for an identity pick. Identity has never had a
#: route row behind it, and ``route_id`` is not optional on ``RoutingResult``,
#: so the placeholder the old identity ``EffectiveRoute`` carried is preserved
#: verbatim rather than replaced with something new. No identity path reads it:
#: the plain App MCP path stamps ``session_metadata["app_mcp_route_id"]``, the
#: identity path does not. It disappears with ``RoutingResult`` when the
#: ``AppAgentRoute`` family is deleted.
_IDENTITY_ROUTE_ID = uuid.UUID(int=0)


@dataclass(frozen=True)
class IdentityPick:
    """Stage 1's answer when the winner is a person rather than a route.

    Field names mirror the identity half of ``EffectiveRoute``, which is where
    this lived before the extraction — so ``_route_identity`` and the shared
    log lines in ``route_message`` read the same attributes off either kind of
    winner, and neither has to ask which one it holds.

    ``agent_name`` duplicating ``identity_owner_name`` is the one wart, and it
    is inherited rather than invented: on ``EffectiveRoute`` the two could
    differ (``identity_owner_name`` fell back to empty, ``agent_name`` to the
    email), and ``_route_identity`` still reads them as ``owner_name or
    agent_name``. ``IdentityCandidateProvider`` collapses both fallbacks into
    one ``Candidate.name``, so today they are always equal.
    """

    identity_owner_id: uuid.UUID
    identity_owner_name: str
    agent_name: str
    route_id: uuid.UUID = _IDENTITY_ROUTE_ID


@dataclass
class RoutingResult:
    """Result of routing a message to an agent."""

    agent_id: uuid.UUID
    agent_name: str
    session_mode: str
    route_id: uuid.UUID
    route_source: str  # "admin" | "user" | "identity"
    match_method: str  # "pattern" | "ai" | "only_one"
    # Identity-specific fields (only set when route_source == "identity")
    is_identity: bool = False
    identity_owner_id: uuid.UUID | None = None
    identity_owner_name: str | None = None
    identity_stage2_match_method: str | None = None
    identity_binding_id: uuid.UUID | None = None
    identity_binding_assignment_id: uuid.UUID | None = None
    # Message transformation (only set when AI routing stripped a routing prefix)
    transformed_message: str | None = None


class AppMCPRoutingService:
    """Routes MCP messages to the appropriate agent."""

    @staticmethod
    def route_message(
        db_session: DBSession,
        user_id: uuid.UUID,
        message: str,
        channel: str = "app_mcp",
    ) -> RoutingResult | None:
        """Determine which agent should handle a message.

        1. Compose the ballot: the user's effective routes + the identity
           owners they can address.
        2. Try pattern matching (identity candidates have no patterns).
        3. Fall back to AI classification over the whole ballot.
        4. If an identity candidate won, invoke Stage 2 routing.

        Returns RoutingResult or None if routing fails.
        """
        effective_routes = AppAgentRouteService.get_effective_routes_for_user(
            db_session=db_session,
            user_id=user_id,
            channel=channel,
        )
        identity_candidates = IdentityCandidateProvider.build(db_session, user_id)

        if not effective_routes and not identity_candidates:
            logger.debug("No effective routes or identity contacts for user %s", user_id)
            return None

        # Debug, not info: this line and the per-route dump below carry EXTERNAL
        # users' message text and every candidate's trigger prompt. The routing
        # trace is where that detail belongs now.
        logger.debug(
            "[Stage1] Routing message for user=%s | message=%r | "
            "%d effective routes, %d identity contacts:",
            user_id, message[:120], len(effective_routes), len(identity_candidates),
        )
        for i, r in enumerate(effective_routes):
            logger.debug(
                "[Stage1]   route[%d] source=%s agent=%s (%s) trigger=%r patterns=%r",
                i, r.source, r.agent_name, r.agent_id,
                (r.trigger_prompt or "")[:80],
                (r.message_patterns or "")[:60] or None,
            )

        stage1_transformed_message: str | None = None
        selected: EffectiveRoute | IdentityPick

        # If only one candidate in total, use it directly (no need to classify)
        if len(effective_routes) + len(identity_candidates) == 1:
            if effective_routes:
                selected = effective_routes[0]
            else:
                only_identity = AppMCPRoutingService._identity_pick(
                    identity_candidates[0]
                )
                if only_identity is None:
                    logger.warning(
                        "[Stage1] Sole candidate has a malformed identity ref — "
                        "refusing to route (user=%s)", user_id,
                    )
                    return None
                selected = only_identity
            stage1_method = "only_one"
            routing_trace.record_match(method=routing_trace.MATCH_ONLY_ONE)
            logger.info(
                "[Stage1] Single candidate — using directly: %s",
                selected.agent_name,
            )
        else:
            # 1. Try pattern matching (identity candidates have no patterns)
            matched = AppMCPRoutingService._try_pattern_match(message, effective_routes)
            if matched:
                selected = matched
                stage1_method = "pattern"
                logger.info("[Stage1] Pattern match hit: %s (%s)", selected.agent_name, selected.agent_id)
            else:
                logger.info("[Stage1] No pattern match — falling back to AI classification")
                # 2. Fall back to AI classification over the whole ballot
                ai_result = AppMCPRoutingService._ai_classify(
                    message, effective_routes, identity_candidates
                )
                if ai_result:
                    selected, stage1_transformed_message = ai_result
                    stage1_method = "ai"
                    logger.debug(
                        "[Stage1] AI selected: %s | transformed_message=%r",
                        selected.agent_name,
                        stage1_transformed_message[:120] if stage1_transformed_message else None,
                    )
                else:
                    logger.info("[Stage1] AI classification returned no match (user=%s)", user_id)
                    return None

        is_identity = isinstance(selected, IdentityPick)
        logger.info(
            "[Stage1] Result: method=%s agent=%s is_identity=%s",
            stage1_method, selected.agent_name, is_identity,
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
                    db_session=db_session,
                    selected_route=selected,
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

        return RoutingResult(
            agent_id=selected.agent_id,
            agent_name=selected.agent_name,
            session_mode=selected.session_mode,
            route_id=selected.route_id,
            route_source=selected.source,
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
        db_session: DBSession,
        selected_route: "IdentityPick",
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

        ``db_session`` is not forwarded: Stage 2 opens its own short-lived read
        session so it can be called from a context that must not hand its
        transaction to a routing decision (see that module's docstring, fact 1).
        """
        from app.services.identity.identity_routing_service import IdentityRoutingService

        owner_id = selected_route.identity_owner_id
        owner_name = selected_route.identity_owner_name or selected_route.agent_name

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
            route_id=selected_route.route_id,
            route_source="identity",
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
    def _try_pattern_match(
        message: str,
        routes: list[EffectiveRoute],
    ) -> EffectiveRoute | None:
        """Try each route's message_patterns against the message using fnmatch.

        Patterns are newline-separated glob-style strings (e.g. 'sign this document *').
        Returns the first matching route or None.
        """
        message_lower = message.lower()
        for route in routes:
            if not route.message_patterns:
                continue
            patterns = [
                p.strip()
                for p in route.message_patterns.splitlines()
                if p.strip()
            ]
            for pattern in patterns:
                if fnmatch.fnmatch(message_lower, pattern.lower()):
                    routing_trace.record_match(
                        method=routing_trace.MATCH_PATTERN,
                        matched_pattern=pattern,
                    )
                    logger.debug(
                        "Pattern match: route=%s pattern=%r message=%r",
                        route.route_id,
                        pattern,
                        message[:80],
                    )
                    return route
        return None

    @staticmethod
    def _ai_classify(
        message: str,
        routes: list[EffectiveRoute],
        identity_candidates: list[Candidate] | None = None,
    ) -> tuple["EffectiveRoute | IdentityPick", str | None] | None:
        """Classify the message against the composed ballot.

        Builds :class:`Candidate` objects for the routes — the one candidate
        shape every routing consumer uses — appends the identity candidates the
        provider already built, and hands the lot to ``AgentClassifier``, which
        owns prompt rendering (``prompt_examples`` included), parsing and trace
        emission.

        Returns (matched_route_or_identity, transformed_message) or None.
        The transformed_message is None when the AI did not strip a routing prefix.
        """
        from app.services.routing.agent_classifier import AgentClassifier

        identity_candidates = identity_candidates or []
        candidates = [
            Candidate(
                ref_id=str(route.agent_id),
                name=route.agent_name,
                trigger_prompt=route.trigger_prompt,
                prompt_examples=route.prompt_examples,
            )
            for route in routes
        ] + identity_candidates

        routing_result = AgentClassifier.classify(candidates, message)

        if not routing_result:
            return None

        # Identity first: its ref is namespaced, so it can never be mistaken
        # for an agent id — and this check must come before the UUID parse,
        # which would otherwise reject it as malformed.
        owner_id = parse_identity_ref(routing_result.agent_id)
        if owner_id is not None:
            for candidate in identity_candidates:
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

        # Find the matching route
        try:
            agent_id = uuid.UUID(routing_result.agent_id)
        except ValueError:
            logger.warning("AI router returned invalid UUID: %r", routing_result.agent_id)
            routing_trace.record_parse_outcome(
                reason="classifier returned a value that is not a UUID"
            )
            return None

        for route in routes:
            if route.agent_id == agent_id:
                routing_trace.record_match(method=routing_trace.MATCH_AI)
                return route, routing_result.transformed_message

        logger.warning("AI router returned agent_id %s not in effective routes", routing_result.agent_id)
        routing_trace.record_parse_outcome(
            reason="classifier picked an agent that is not among the effective routes"
        )
        return None
