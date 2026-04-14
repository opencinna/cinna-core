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

        # If only one route, use it directly (no need to classify)
        if len(effective_routes) == 1:
            route = effective_routes[0]
            selected = route
            stage1_method = "only_one"
        else:
            # 1. Try pattern matching (identity routes have no patterns)
            matched = AppMCPRoutingService._try_pattern_match(message, effective_routes)
            if matched:
                selected = matched
                stage1_method = "pattern"
            else:
                # 2. Fall back to AI classification
                ai_matched = AppMCPRoutingService._ai_classify(message, effective_routes)
                if ai_matched:
                    selected = ai_matched
                    stage1_method = "ai"
                else:
                    logger.debug("No route matched for message (user=%s)", user_id)
                    return None

        # Stage 2: If the selected route is an identity contact, invoke identity routing
        if selected.source == "identity" and selected.identity_owner_id:
            return AppMCPRoutingService._route_identity(
                db_session=db_session,
                selected_route=selected,
                caller_user_id=user_id,
                message=message,
                stage1_method=stage1_method,
            )

        return RoutingResult(
            agent_id=selected.agent_id,
            agent_name=selected.agent_name,
            session_mode=selected.session_mode,
            route_id=selected.route_id,
            route_source=selected.source,
            match_method=stage1_method,
        )

    @staticmethod
    def _route_identity(
        db_session: DBSession,
        selected_route: "EffectiveRoute",
        caller_user_id: uuid.UUID,
        message: str,
        stage1_method: str,
    ) -> RoutingResult | None:
        """Invoke Stage 2 routing for an identity contact.

        Returns a RoutingResult with identity fields populated,
        or None if Stage 2 cannot find an accessible agent.
        """
        from app.services.identity.identity_routing_service import IdentityRoutingService

        owner_id = selected_route.identity_owner_id
        owner_name = selected_route.identity_owner_name or selected_route.agent_name

        stage2_result = IdentityRoutingService.route_within_identity(
            db_session=db_session,
            owner_id=owner_id,
            caller_user_id=caller_user_id,
            message=message,
        )

        if not stage2_result:
            logger.debug(
                "[AppMCPRouting] Stage 2 returned no result for identity owner=%s caller=%s",
                owner_id,
                caller_user_id,
            )
            return None

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
    ) -> EffectiveRoute | None:
        """Call AI function to classify message against available routes.

        Builds a list of agent dicts with trigger_prompt descriptions
        and asks the LLM to pick the best match.
        Returns matched route or None.
        """
        from app.services.ai_functions.ai_functions_service import AIFunctionsService

        available_agents = [
            {
                "id": str(route.agent_id),
                "name": route.agent_name,
                "trigger_prompt": route.trigger_prompt,
            }
            for route in routes
        ]

        agent_id_str = AIFunctionsService.route_to_agent(
            message=message,
            available_agents=available_agents,
        )

        if not agent_id_str:
            return None

        # Find the matching route
        try:
            agent_id = uuid.UUID(agent_id_str)
        except ValueError:
            logger.warning("AI router returned invalid UUID: %r", agent_id_str)
            return None

        for route in routes:
            if route.agent_id == agent_id:
                return route

        logger.warning("AI router returned agent_id %s not in effective routes", agent_id_str)
        return None
