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
from app.services.routing import routing_trace
from app.services.routing.agent_classifier import AgentClassifier, Candidate

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
    # Message transformation (only set when AI routing stripped a routing prefix)
    transformed_message: str | None = None


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
        # Debug, not info: Stage 2 runs before the channel pipeline's Pass 1
        # ownership filter gets a chance to reject an identity route, so this
        # line can carry EXTERNAL, non-platform users' message text. Same
        # reasoning as the [Stage1]/[AIRouter] downgrades.
        logger.debug(
            "[Stage2] Identity routing: owner=%s caller=%s | message=%r",
            owner_id, caller_user_id, message[:120],
        )

        bindings = IdentityService.get_active_bindings_for_user(
            db_session=db_session,
            owner_id=owner_id,
            target_user_id=caller_user_id,
        )

        if not bindings:
            logger.info(
                "[Stage2] No accessible bindings for caller=%s in identity=%s",
                caller_user_id,
                owner_id,
            )
            return None

        logger.info("[Stage2] %d accessible binding(s) for caller:", len(bindings))
        for i, b in enumerate(bindings):
            agent = db_session.get(Agent, b.agent_id)
            logger.info(
                "[Stage2]   binding[%d] agent=%s (%s) trigger=%r patterns=%r active=%s mode=%s",
                i, agent.name if agent else "?", b.agent_id,
                (b.trigger_prompt or "")[:80],
                (b.message_patterns or "")[:60] or None,
                b.is_active, b.session_mode,
            )

        # Enrich bindings with their assignment IDs for the caller
        # We need the assignment ID (to store on session) for each binding
        binding_assignments = IdentityRoutingService._get_binding_assignments(
            db_session, bindings, caller_user_id
        )

        # Record the ballot before anything narrows it. Phase 1 left this stage
        # capturing a prompt and a raw response with **zero candidates**,
        # because the binding list is built here rather than by a shared
        # builder — a documented deferral that reads, on the tuning card, as a
        # broken recorder. It is closed here now that ``_binding_candidates``
        # is the one builder both the capture and the classifier use.
        candidates = IdentityRoutingService._binding_candidates(db_session, bindings)
        IdentityRoutingService._record_candidates(candidates, binding_assignments, bindings)

        # Single binding — use directly
        if len(bindings) == 1:
            binding = bindings[0]
            agent = db_session.get(Agent, binding.agent_id)
            agent_name = agent.name if agent else ""
            assignment_id = binding_assignments.get(binding.id)
            if not assignment_id:
                logger.info("[Stage2] Single binding but no assignment found — aborting")
                return None
            logger.info(
                "[Stage2] Single binding — using directly: %s (%s)",
                agent_name, binding.agent_id,
            )
            routing_trace.record_match(method=routing_trace.MATCH_ONLY_ONE)
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
                logger.info("[Stage2] Pattern matched binding but no assignment — aborting")
                return None
            logger.info(
                "[Stage2] Pattern match hit: %s (%s)",
                agent_name, matched.agent_id,
            )
            return IdentityRoutingResult(
                agent_id=matched.agent_id,
                agent_name=agent_name,
                session_mode=matched.session_mode,
                binding_id=matched.id,
                binding_assignment_id=assignment_id,
                match_method="pattern",
            )

        # Fall back to AI classification
        logger.info("[Stage2] No pattern match — falling back to AI classification")
        ai_result = IdentityRoutingService._ai_classify(message, bindings, db_session)
        if ai_result:
            ai_matched, ai_transformed_message = ai_result
            agent = db_session.get(Agent, ai_matched.agent_id)
            agent_name = agent.name if agent else ""
            assignment_id = binding_assignments.get(ai_matched.id)
            if not assignment_id:
                logger.info("[Stage2] AI matched binding but no assignment — aborting")
                return None
            # transformed_message is a rewrite of the user's text — see above.
            logger.debug(
                "[Stage2] AI selected: %s (%s) | transformed_message=%r",
                agent_name, ai_matched.agent_id,
                ai_transformed_message[:120] if ai_transformed_message else None,
            )
            return IdentityRoutingResult(
                agent_id=ai_matched.agent_id,
                agent_name=agent_name,
                session_mode=ai_matched.session_mode,
                binding_id=ai_matched.id,
                binding_assignment_id=assignment_id,
                match_method="ai",
                transformed_message=ai_transformed_message,
            )

        logger.info(
            "[Stage2] No agent matched for caller=%s in identity=%s",
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
    def _binding_candidates(
        db_session: DBSession,
        bindings: list[IdentityAgentBinding],
    ) -> list[Candidate]:
        """The Stage-2 ballot: one :class:`Candidate` per accessible binding.

        The **single** builder for this stage. It feeds both the classifier and
        the trace's candidate capture, which is the point: a second builder is
        how ``prompt_examples`` came to be collected on the App MCP path and
        silently not on this one, even though ``IdentityAgentBinding`` has
        carried the field all along.
        """
        candidates: list[Candidate] = []
        for binding in bindings:
            agent = db_session.get(Agent, binding.agent_id)
            candidates.append(
                Candidate(
                    ref_id=str(binding.agent_id),
                    name=agent.name if agent else str(binding.agent_id),
                    trigger_prompt=binding.trigger_prompt or "",
                    prompt_examples=binding.prompt_examples,
                )
            )
        return candidates

    @staticmethod
    def _record_candidates(
        candidates: list[Candidate],
        binding_assignments: dict[uuid.UUID, uuid.UUID],
        bindings: list[IdentityAgentBinding],
    ) -> None:
        """Put the Stage-2 ballot on the active trace, skips included.

        A binding with no assignment for this caller is recorded as an excluded
        candidate rather than dropped: every one of the three paths below aborts
        on a missing assignment, and "the agent you expected has no assignment
        for this caller" is precisely the diagnosis the tuning card exists to
        give. Wrapped whole because it builds its arguments from ORM objects —
        the recorder guards the *recording*, never the caller's expressions
        (plan §11a, Rule 2).
        """
        try:
            for candidate, binding in zip(candidates, bindings):
                if binding_assignments.get(binding.id):
                    routing_trace.record_candidate(
                        kind=routing_trace.KIND_AGENT,
                        ref_id=candidate.ref_id,
                        name=candidate.name,
                        source="identity",
                        trigger_prompt=candidate.trigger_prompt,
                        prompt_examples=candidate.prompt_examples,
                    )
                else:
                    routing_trace.record_skip(
                        kind=routing_trace.KIND_AGENT,
                        ref_id=candidate.ref_id,
                        name=candidate.name,
                        reason=routing_trace.SKIP_NO_ASSIGNMENT,
                        source="identity",
                        trigger_prompt=candidate.trigger_prompt,
                        prompt_examples=candidate.prompt_examples,
                    )
        except Exception:  # noqa: BLE001
            logger.debug("[Stage2] Candidate capture failed", exc_info=True)

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
                    # Without this the stage reported ``match_method=None`` on a
                    # pattern hit — a lying field, not a missing one: the trace
                    # said "nothing matched this way" about a match that had
                    # just happened. Its App MCP twin has always recorded it.
                    routing_trace.record_match(
                        method=routing_trace.MATCH_PATTERN,
                        matched_pattern=pattern,
                    )
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
    ) -> tuple[IdentityAgentBinding, str | None] | None:
        """Use AI classification to pick the best binding for the message.

        Returns (matched_binding, transformed_message) or None.
        The transformed_message is None when the AI did not strip a routing prefix.

        Candidates come from ``_binding_candidates`` — the same builder the
        stage's candidate capture uses, so the tuning card can never show a
        ballot the classifier did not actually receive.
        """
        candidates = IdentityRoutingService._binding_candidates(db_session, bindings)

        routing_result = AgentClassifier.classify(candidates, message)

        if not routing_result:
            return None

        try:
            agent_id = uuid.UUID(routing_result.agent_id)
        except ValueError:
            logger.warning("[IdentityRouting] AI router returned invalid UUID: %r", routing_result.agent_id)
            return None

        for binding in bindings:
            if binding.agent_id == agent_id:
                return binding, routing_result.transformed_message

        logger.warning(
            "[IdentityRouting] AI router returned agent_id %s not in accessible bindings",
            routing_result.agent_id,
        )
        return None
