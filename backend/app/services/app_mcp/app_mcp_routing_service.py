"""
App MCP Routing Service — determines which agent should handle a message.

Routing priority:
  1. Pattern matching (fnmatch-based glob patterns)
  2. AI classification (LLM picks the best agent from trigger prompts)
  3. Return None if no match found
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

logger = logging.getLogger(__name__)


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

        1. Get effective routes for user (includes identity contacts).
        2. Try pattern matching (identity routes have no patterns, so won't match here).
        3. Fall back to AI classification.
        4. If selected route is an identity contact, invoke Stage 2 routing.

        Returns RoutingResult or None if routing fails.
        """
        effective_routes = AppAgentRouteService.get_effective_routes_for_user(
            db_session=db_session,
            user_id=user_id,
            channel=channel,
        )

        if not effective_routes:
            logger.debug("No effective routes for user %s", user_id)
            return None

        # Debug, not info: this line and the per-route dump below carry EXTERNAL
        # users' message text and every candidate's trigger prompt. The routing
        # trace is where that detail belongs now.
        logger.debug(
            "[Stage1] Routing message for user=%s | message=%r | %d effective routes:",
            user_id, message[:120], len(effective_routes),
        )
        for i, r in enumerate(effective_routes):
            logger.debug(
                "[Stage1]   route[%d] source=%s agent=%s (%s) trigger=%r patterns=%r",
                i, r.source, r.agent_name, r.agent_id,
                (r.trigger_prompt or "")[:80],
                (r.message_patterns or "")[:60] or None,
            )

        stage1_transformed_message: str | None = None

        # If only one route, use it directly (no need to classify)
        if len(effective_routes) == 1:
            route = effective_routes[0]
            selected = route
            stage1_method = "only_one"
            routing_trace.record_match(method=routing_trace.MATCH_ONLY_ONE)
            logger.info("[Stage1] Single route — using directly: %s (%s)", selected.agent_name, selected.agent_id)
        else:
            # 1. Try pattern matching (identity routes have no patterns)
            matched = AppMCPRoutingService._try_pattern_match(message, effective_routes)
            if matched:
                selected = matched
                stage1_method = "pattern"
                logger.info("[Stage1] Pattern match hit: %s (%s)", selected.agent_name, selected.agent_id)
            else:
                logger.info("[Stage1] No pattern match — falling back to AI classification")
                # 2. Fall back to AI classification
                ai_result = AppMCPRoutingService._ai_classify(message, effective_routes)
                if ai_result:
                    selected, stage1_transformed_message = ai_result
                    stage1_method = "ai"
                    logger.debug(
                        "[Stage1] AI selected: %s (%s) | transformed_message=%r",
                        selected.agent_name, selected.agent_id,
                        stage1_transformed_message[:120] if stage1_transformed_message else None,
                    )
                else:
                    logger.info("[Stage1] AI classification returned no match (user=%s)", user_id)
                    return None

        logger.info(
            "[Stage1] Result: method=%s agent=%s source=%s is_identity=%s",
            stage1_method, selected.agent_name, selected.source,
            selected.source == "identity",
        )

        # Stage 2: If the selected route is an identity contact, invoke identity routing
        if selected.source == "identity" and selected.identity_owner_id:
            logger.debug(
                "[Stage1→Stage2] Identity route detected — handing off to Stage 2 | "
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
    def _route_identity(
        db_session: DBSession,
        selected_route: "EffectiveRoute",
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
        """
        from app.services.identity.identity_routing_service import IdentityRoutingService

        owner_id = selected_route.identity_owner_id
        owner_name = selected_route.identity_owner_name or selected_route.agent_name

        # Pass Stage 1's transformed message to Stage 2 if available
        stage2_input_message = transformed_message or message

        stage2_result = IdentityRoutingService.route_within_identity(
            db_session=db_session,
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
    ) -> tuple[EffectiveRoute, str | None] | None:
        """Classify the message against the available routes.

        Builds :class:`Candidate` objects — the one candidate shape every
        routing consumer uses — and hands them to ``AgentClassifier``, which
        owns prompt rendering (``prompt_examples`` included), parsing and trace
        emission.

        Returns (matched_route, transformed_message) or None.
        The transformed_message is None when the AI did not strip a routing prefix.
        """
        from app.services.routing.agent_classifier import AgentClassifier, Candidate

        candidates = [
            Candidate(
                ref_id=str(route.agent_id),
                name=route.agent_name,
                trigger_prompt=route.trigger_prompt,
                prompt_examples=route.prompt_examples,
            )
            for route in routes
        ]

        routing_result = AgentClassifier.classify(candidates, message)

        if not routing_result:
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
