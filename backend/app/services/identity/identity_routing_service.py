"""Identity Stage 2 — which of *this person's* agents should take the message.

Stage 1 resolves a person (``IdentityCandidateProvider`` puts them on the
ballot); Stage 2 resolves an agent from that person's portfolio, filtered to
what the specific caller may reach. Two stages is also the recursion cap: an
identity's agents are agents, never further identities, and that is the
intended guard.

**This module is written to the four structural facts in
``channel_routing_service.py``'s module docstring**, because
``ChannelRoutingService.decide`` calls this from inside itself: Stage 1 puts a
person on the ballot, and a person winning hands off here. A violation of any
of the four *here* is therefore a violation *there*, one call deeper, on a
route documented as having no side effects. Same four, same numbering:

1. **No caller session crosses the boundary.** ``route_within_identity`` takes
   ids and text. It cannot add to, commit, or roll back a transaction the
   caller is holding, and a caller therefore cannot be surprised by what a
   routing decision did to its unit of work.
2. **The session it opens is read-only in practice.** One short-lived session,
   ``SELECT``s only, closed on the way out. No ``add`` / ``commit`` /
   ``delete`` anywhere in this module.
3. **Nothing effectful is imported here.** No binding model, no ingestion,
   install or outbound service. Those names are not in this module's namespace,
   so no branch added later can reach one by accident — it would have to add
   the import, which is visible in a diff.
4. **It returns plain data.** ``IdentityRoutingResult`` carries ids and two
   strings read off rows *inside* that session — never an ORM instance, whose
   session is closed by the time the caller sees it and whose next attribute
   read would be a lazy reload against a dead connection.

**These four are executed, not reviewed.**
``tests/architecture/channel_routing_purity_test.py`` used to parse one module
path and this list was a claim — the one kind of claim a reader has no way to
spot as false. That test is now parameterized over both modules, closing it the
second of the two ways this docstring proposed: the change that calls Stage 2
from inside ``decide`` did it, because that is the point at which a violation
here becomes a violation there. As with the list over there, do not extend this
one without extending that test in the same change.

Not a structural fact and stated separately for that reason: the trace this
module writes is the ambient ``RoutingTrace`` recorder, which is a no-op when
nobody opened a capture. Before the channels & identity unification nobody ever
did on this path, so every recorder call here was dead instrumentation; the
channel Pass-1 capture is now open around it.

**Glob pre-matching is gone.** ``IdentityAgentBinding.message_patterns`` is no
longer read here (settled decision §2.9 of the channels/identity unification
master plan): a second routing mechanism with silently higher priority than the
classifier, which no trace explains well, is worse than one classifier call.
The field is gone from the model and its DTOs; the column is dropped by the
phase's own migration.
"""
import logging
import uuid
from dataclasses import dataclass

from sqlmodel import Session as DBSession

from app.models import Agent
from app.models.identity.identity_models import IdentityAgentBinding, IdentityBindingAssignment
from app.services.identity.identity_service import IdentityService
from app.services.routing import routing_trace
from app.services.routing.agent_classifier import AgentClassifier, Candidate

logger = logging.getLogger(__name__)


@dataclass
class IdentityRoutingResult:
    """What Stage 2 decided — ids and plain strings, no rows.

    ``agent_name`` and ``session_mode`` are values, not references: both are
    read off the binding and agent inside the service's own session and copied
    out. They are on the result because the two consumers need them to build a
    session (``session_mode``) and to label the reply, and re-reading them
    would mean a second query for facts this stage already had in hand.
    """

    agent_id: uuid.UUID
    agent_name: str
    session_mode: str
    binding_id: uuid.UUID
    binding_assignment_id: uuid.UUID
    match_method: str  # "only_one" | "ai"
    # Message transformation (only set when AI routing stripped a routing prefix)
    transformed_message: str | None = None


class IdentityRoutingService:
    """Stage 2 routing: selects an agent from the identity owner's portfolio.

    Only considers agents that are accessible to the specific caller
    (``target_user_id``).
    """

    @staticmethod
    def route_within_identity(
        owner_id: uuid.UUID,
        caller_user_id: uuid.UUID,
        message: str,
    ) -> IdentityRoutingResult | None:
        """Select the best agent from owner's identity, filtered by caller's access.

        Algorithm:
        1. Get active bindings accessible to ``caller_user_id``
        2. If none → return ``None``
        3. If one → use directly (no classifier call)
        4. Otherwise classify

        The **single-binding shortcut** in step 3 stays a Stage-2 property and
        is not flattened into Stage 1 (master plan §2.15): Stage 1 chose a
        *person*, and whether that person happens to have exactly one reachable
        agent is not a fact Stage 1's ballot can hold without re-deriving the
        caller's access for every candidate on it.

        Opens its own database session — see fact 1 in the module docstring.

        **Cost, stated because it is not free.** Both callers hold a session
        across this call, so an identity request occupies two pooled
        connections instead of one while Stage 2 runs — App MCP holds the
        request's own session, and the channel path holds Pass 1's worker-thread
        session (``ChannelRoutingService._route_identity`` runs inside
        ``_route_installed``, which owns one). The difference between them is
        which transaction is outstanding: App MCP's is the caller's own unit of
        work, the channel's is a read-only pass that commits nothing. Holding a
        connection while waiting for a second is the classic pool-exhaustion
        deadlock, and identity routing is not a hot path today — but if it
        becomes one, this is the line to revisit, not the pool size.
        """
        # Debug, not info: Stage 2 can carry EXTERNAL, non-platform users'
        # message text. Same reasoning as the [Stage1]/[AIRouter] downgrades.
        logger.debug(
            "[Stage2] Identity routing: owner=%s caller=%s | message=%r",
            owner_id, caller_user_id, message[:120],
        )

        # Function-local so the name resolves through the (patchable)
        # ``app.core.db`` module at call time rather than becoming a module
        # attribute — the convention `ChannelRoutingService` follows, and the
        # one `tests/architecture/patch_target_drift_test.py` documents.
        from app.core.db import create_session

        with create_session() as db_session:
            return IdentityRoutingService._select(
                db_session=db_session,
                owner_id=owner_id,
                caller_user_id=caller_user_id,
                message=message,
            )

    @staticmethod
    def _select(
        db_session: DBSession,
        owner_id: uuid.UUID,
        caller_user_id: uuid.UUID,
        message: str,
    ) -> IdentityRoutingResult | None:
        """The decision itself, on an already-open read session."""
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

        # Enrich bindings with their assignment IDs for the caller. The
        # assignment id is stamped on the session, so a binding without one
        # cannot produce a usable result and every branch below aborts on it.
        binding_assignments = IdentityRoutingService._get_binding_assignments(
            db_session, bindings, caller_user_id
        )

        # Record the ballot before anything narrows it. ``_binding_candidates``
        # is the one builder both this capture and the classifier use, so the
        # tuning card can never show a ballot the classifier did not receive.
        candidates = IdentityRoutingService._binding_candidates(db_session, bindings)
        IdentityRoutingService._record_candidates(candidates, binding_assignments, bindings)

        logger.debug(
            "[Stage2] %d accessible binding(s), %d with an assignment for caller=%s",
            len(bindings),
            len(binding_assignments),
            caller_user_id,
        )

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
        candidate rather than dropped: every one of the paths below aborts on a
        missing assignment, and "the agent you expected has no assignment for
        this caller" is precisely the diagnosis the tuning card exists to give.
        Wrapped whole because it builds its arguments from ORM objects — the
        recorder guards the *recording*, never the caller's expressions
        (auto-routing plan §11a, Rule 2).
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

        # No ``record_match`` here, deliberately: this stage has never recorded
        # one on the AI branch, and adding it would change what the trace's
        # decision-level ``match_method`` reports (it reads "how the *last*
        # stage matched", and Stage 2 overwrites Stage 1's value). That field's
        # shape is settled — do not restructure it from here.
        for binding in bindings:
            if binding.agent_id == agent_id:
                return binding, routing_result.transformed_message

        logger.warning(
            "[IdentityRouting] AI router returned agent_id %s not in accessible bindings",
            routing_result.agent_id,
        )
        return None
