"""
Identity Routing Service — Stage 2 routing for the Identity MCP Server.

After Stage 1 routing identifies an identity contact (a person), Stage 2
selects the appropriate agent from that person's identity portfolio,
filtered to only those accessible to the specific caller.
"""
import fnmatch
import logging
import uuid
from dataclasses import dataclass

from sqlmodel import Session as DBSession

from app.models import Agent
from app.services.identity.identity_service import IdentityService
from app.models.identity.identity_models import IdentityAgentBinding, IdentityBindingAssignment

logger = logging.getLogger(__name__)


@dataclass
class IdentityRoutingResult:
    """Result of Stage 2 identity routing."""

    agent_id: uuid.UUID
    agent_name: str
    session_mode: str
    binding_id: uuid.UUID
    binding_assignment_id: uuid.UUID
    match_method: str  # "only_one" | "pattern" | "ai"


class IdentityRoutingService:
    """Stage 2 routing: selects an agent from the identity owner's portfolio.

    Only considers agents that are accessible to the specific caller (target_user_id).
    """

    @staticmethod
    def route_within_identity(
        db_session: DBSession,
        owner_id: uuid.UUID,
        caller_user_id: uuid.UUID,
        message: str,
    ) -> IdentityRoutingResult | None:
        """Select the best agent from owner's identity, filtered by caller's access.

        Algorithm:
        1. Get active bindings accessible to caller_user_id
        2. If none → return None
        3. If one → use directly (no AI needed)
        4. Try pattern matching
        5. Fall back to AI classification via route_to_agent()

        Returns IdentityRoutingResult or None if no agent available.
        """
        bindings = IdentityService.get_active_bindings_for_user(
            db_session=db_session,
            owner_id=owner_id,
            target_user_id=caller_user_id,
        )

        if not bindings:
            logger.debug(
                "[IdentityRouting] No accessible bindings for caller=%s in identity=%s",
                caller_user_id,
                owner_id,
            )
            return None

        # Enrich bindings with their assignment IDs for the caller
        # We need the assignment ID (to store on session) for each binding
        binding_assignments = IdentityRoutingService._get_binding_assignments(
            db_session, bindings, caller_user_id
        )

        # Single binding — use directly
        if len(bindings) == 1:
            binding = bindings[0]
            agent = db_session.get(Agent, binding.agent_id)
            agent_name = agent.name if agent else ""
            assignment_id = binding_assignments.get(binding.id)
            if not assignment_id:
                return None
            return IdentityRoutingResult(
                agent_id=binding.agent_id,
                agent_name=agent_name,
                session_mode=binding.session_mode,
                binding_id=binding.id,
                binding_assignment_id=assignment_id,
                match_method="only_one",
            )

        # Try pattern matching
        matched = IdentityRoutingService._try_pattern_match(message, bindings)
        if matched:
            agent = db_session.get(Agent, matched.agent_id)
            agent_name = agent.name if agent else ""
            assignment_id = binding_assignments.get(matched.id)
            if not assignment_id:
                return None
            return IdentityRoutingResult(
                agent_id=matched.agent_id,
                agent_name=agent_name,
                session_mode=matched.session_mode,
                binding_id=matched.id,
                binding_assignment_id=assignment_id,
                match_method="pattern",
            )

        # Fall back to AI classification
        ai_matched = IdentityRoutingService._ai_classify(message, bindings, db_session)
        if ai_matched:
            agent = db_session.get(Agent, ai_matched.agent_id)
            agent_name = agent.name if agent else ""
            assignment_id = binding_assignments.get(ai_matched.id)
            if not assignment_id:
                return None
            return IdentityRoutingResult(
                agent_id=ai_matched.agent_id,
                agent_name=agent_name,
                session_mode=ai_matched.session_mode,
                binding_id=ai_matched.id,
                binding_assignment_id=assignment_id,
                match_method="ai",
            )

        logger.debug(
            "[IdentityRouting] No agent matched for caller=%s in identity=%s",
            caller_user_id,
            owner_id,
        )
        return None

    @staticmethod
    def _get_binding_assignments(
        db_session: DBSession,
        bindings: list[IdentityAgentBinding],
        caller_user_id: uuid.UUID,
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Return mapping of binding_id → assignment_id for this caller."""
        from sqlmodel import select

        binding_ids = [b.id for b in bindings]
        if not binding_ids:
            return {}

        stmt = (
            select(IdentityBindingAssignment)
            .where(
                IdentityBindingAssignment.binding_id.in_(binding_ids),
                IdentityBindingAssignment.target_user_id == caller_user_id,
            )
        )
        return {a.binding_id: a.id for a in db_session.exec(stmt).all()}

    @staticmethod
    def _try_pattern_match(
        message: str,
        bindings: list[IdentityAgentBinding],
    ) -> IdentityAgentBinding | None:
        """Try fnmatch-based pattern matching against binding message_patterns."""
        message_lower = message.lower()
        for binding in bindings:
            if not binding.message_patterns:
                continue
            patterns = [
                p.strip()
                for p in binding.message_patterns.splitlines()
                if p.strip()
            ]
            for pattern in patterns:
                if fnmatch.fnmatch(message_lower, pattern.lower()):
                    logger.debug(
                        "[IdentityRouting] Pattern match: binding=%s pattern=%r",
                        binding.id,
                        pattern,
                    )
                    return binding
        return None

    @staticmethod
    def _ai_classify(
        message: str,
        bindings: list[IdentityAgentBinding],
        db_session: DBSession,
    ) -> IdentityAgentBinding | None:
        """Use AI classification to pick the best binding for the message."""
        from app.agents.app_agent_router import route_to_agent

        available_agents = []
        for binding in bindings:
            agent = db_session.get(Agent, binding.agent_id)
            agent_name = agent.name if agent else str(binding.agent_id)
            available_agents.append({
                "id": str(binding.agent_id),
                "name": agent_name,
                "trigger_prompt": binding.trigger_prompt,
            })

        agent_id_str = route_to_agent(
            message=message,
            available_agents=available_agents,
        )

        if not agent_id_str:
            return None

        try:
            agent_id = uuid.UUID(agent_id_str)
        except ValueError:
            logger.warning("[IdentityRouting] AI router returned invalid UUID: %r", agent_id_str)
            return None

        for binding in bindings:
            if binding.agent_id == agent_id:
                return binding

        logger.warning(
            "[IdentityRouting] AI router returned agent_id %s not in accessible bindings",
            agent_id_str,
        )
        return None
