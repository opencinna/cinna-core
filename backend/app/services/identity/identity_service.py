"""
Identity Service — business logic for identity agent bindings and assignments.

The Identity MCP Server allows users to expose themselves as a routable identity.
Callers address people by name; Stage 2 routing selects the right agent from
the identity owner's portfolio (filtered to those accessible to the specific caller).
"""
import uuid
import logging
from datetime import datetime, UTC
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, select

from app.models import Agent, User
from app.models.identity.identity_models import (
    IdentityAgentBinding,
    IdentityBindingAssignment,
    IdentityAgentBindingCreate,
    IdentityAgentBindingUpdate,
    IdentityBindingAssignmentPublic,
    IdentityAgentBindingPublic,
    IdentityContactPublic,
)

logger = logging.getLogger(__name__)


class IdentityPermissionError(Exception):
    """Raised when caller lacks permission for an identity operation."""


class IdentityNotFoundError(Exception):
    """Raised when a binding or assignment is not found."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assignment_to_public(
    db_session: DBSession,
    assignment: IdentityBindingAssignment,
) -> IdentityBindingAssignmentPublic:
    target_user = db_session.get(User, assignment.target_user_id)
    return IdentityBindingAssignmentPublic(
        id=assignment.id,
        binding_id=assignment.binding_id,
        target_user_id=assignment.target_user_id,
        target_user_name=target_user.full_name or "" if target_user else "",
        target_user_email=target_user.email or "" if target_user else "",
        is_active=assignment.is_active,
        is_enabled=assignment.is_enabled,
        created_at=assignment.created_at,
    )


def _binding_to_public(
    db_session: DBSession,
    binding: IdentityAgentBinding,
) -> IdentityAgentBindingPublic:
    agent = db_session.get(Agent, binding.agent_id)
    agent_name = agent.name if agent else ""

    stmt = select(IdentityBindingAssignment).where(
        IdentityBindingAssignment.binding_id == binding.id
    )
    assignments = [
        _assignment_to_public(db_session, a)
        for a in db_session.exec(stmt).all()
    ]

    return IdentityAgentBindingPublic(
        id=binding.id,
        agent_id=binding.agent_id,
        agent_name=agent_name,
        trigger_prompt=binding.trigger_prompt,
        prompt_examples=binding.prompt_examples,
        session_mode=binding.session_mode,
        is_active=binding.is_active,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
        assignments=assignments,
    )


# ---------------------------------------------------------------------------
# IdentityService
# ---------------------------------------------------------------------------


class IdentityService:
    """Business logic for identity agent bindings and user assignments."""

    # ------------------------------------------------------------------
    # Binding management (identity owner perspective)
    # ------------------------------------------------------------------

    @staticmethod
    def create_binding(
        db_session: DBSession,
        owner_id: uuid.UUID,
        data: IdentityAgentBindingCreate,
        is_superuser: bool = False,
    ) -> IdentityAgentBindingPublic:
        """Create a new identity agent binding.

        Validates:
        - Agent is owned by the binding owner
        - auto_enable requires superuser
        - No duplicate (owner_id, agent_id)

        Raises ValueError for validation failures.
        Raises IntegrityError if unique constraint violated.
        """
        agent = db_session.get(Agent, data.agent_id)
        if not agent:
            raise IdentityNotFoundError(f"Agent {data.agent_id} not found")
        if agent.owner_id != owner_id:
            raise IdentityPermissionError("You can only add your own agents to your identity")
        if data.auto_enable and not is_superuser:
            raise IdentityPermissionError("Only administrators can auto-enable identities for users")

        binding = IdentityAgentBinding(
            owner_id=owner_id,
            agent_id=data.agent_id,
            trigger_prompt=data.trigger_prompt,
            prompt_examples=data.prompt_examples,
            session_mode=data.session_mode,
            is_active=True,
        )
        db_session.add(binding)
        db_session.flush()  # get binding.id

        # Create assignments for provided user IDs
        for user_id in data.assigned_user_ids:
            if user_id == owner_id:
                continue  # self-exclusion
            assignment = IdentityBindingAssignment(
                binding_id=binding.id,
                target_user_id=user_id,
                is_active=True,
                is_enabled=data.auto_enable,
                auto_enable=data.auto_enable,
            )
            db_session.add(assignment)

        db_session.commit()
        db_session.refresh(binding)
        return _binding_to_public(db_session, binding)

    @staticmethod
    def list_bindings(
        db_session: DBSession,
        owner_id: uuid.UUID,
    ) -> list[IdentityAgentBindingPublic]:
        """List all identity agent bindings for the given owner."""
        stmt = select(IdentityAgentBinding).where(
            IdentityAgentBinding.owner_id == owner_id
        )
        bindings = db_session.exec(stmt).all()
        return [_binding_to_public(db_session, b) for b in bindings]

    @staticmethod
    def update_binding(
        db_session: DBSession,
        binding_id: uuid.UUID,
        owner_id: uuid.UUID,
        data: IdentityAgentBindingUpdate,
    ) -> IdentityAgentBindingPublic | None:
        """Update a binding. Returns None if not found or owner mismatch."""
        binding = db_session.get(IdentityAgentBinding, binding_id)
        if not binding or binding.owner_id != owner_id:
            return None
        update_dict = data.model_dump(exclude_unset=True)
        binding.sqlmodel_update(update_dict)
        binding.updated_at = datetime.now(UTC)
        db_session.add(binding)
        db_session.commit()
        db_session.refresh(binding)
        return _binding_to_public(db_session, binding)

    @staticmethod
    def delete_binding(
        db_session: DBSession,
        binding_id: uuid.UUID,
        owner_id: uuid.UUID,
    ) -> bool:
        """Delete a binding and cascade its assignments. Returns False if not found."""
        binding = db_session.get(IdentityAgentBinding, binding_id)
        if not binding or binding.owner_id != owner_id:
            return False
        db_session.delete(binding)
        db_session.commit()
        return True

    @staticmethod
    def get_active_bindings_for_user(
        db_session: DBSession,
        owner_id: uuid.UUID,
        target_user_id: uuid.UUID,
    ) -> list[IdentityAgentBinding]:
        """Get active bindings from owner accessible to target_user_id.

        Used by Stage 2 routing to filter agents the caller can reach.
        Returns bindings where binding.is_active=True AND assignment.is_active=True
        AND assignment.is_enabled=True.
        """
        stmt = (
            select(IdentityAgentBinding)
            .join(
                IdentityBindingAssignment,
                IdentityBindingAssignment.binding_id == IdentityAgentBinding.id,
            )
            .where(
                IdentityAgentBinding.owner_id == owner_id,
                IdentityAgentBinding.is_active == True,  # noqa: E712
                IdentityBindingAssignment.target_user_id == target_user_id,
                IdentityBindingAssignment.is_active == True,  # noqa: E712
                IdentityBindingAssignment.is_enabled == True,  # noqa: E712
            )
        )
        return list(db_session.exec(stmt).all())

    # ------------------------------------------------------------------
    # Assignment management
    # ------------------------------------------------------------------

    @staticmethod
    def assign_users(
        db_session: DBSession,
        binding_id: uuid.UUID,
        owner_id: uuid.UUID,
        user_ids: list[uuid.UUID],
        auto_enable: bool = False,
    ) -> list[IdentityBindingAssignmentPublic]:
        """Bulk assign users to a binding. Skips duplicates and self-assignments.

        Returns all current assignments for this binding.
        Raises ValueError if binding not found or owner mismatch.
        """
        binding = db_session.get(IdentityAgentBinding, binding_id)
        if not binding:
            raise IdentityNotFoundError("Binding not found")
        if binding.owner_id != owner_id:
            raise IdentityPermissionError("Access denied to this binding")

        # Get existing assignments
        existing_stmt = select(IdentityBindingAssignment).where(
            IdentityBindingAssignment.binding_id == binding_id
        )
        existing_user_ids = {a.target_user_id for a in db_session.exec(existing_stmt).all()}

        for user_id in user_ids:
            if user_id == owner_id:
                continue  # self-exclusion
            if user_id in existing_user_ids:
                continue  # skip duplicates
            assignment = IdentityBindingAssignment(
                binding_id=binding_id,
                target_user_id=user_id,
                is_active=True,
                is_enabled=auto_enable,
                auto_enable=auto_enable,
            )
            db_session.add(assignment)

        db_session.commit()

        all_assignments = db_session.exec(existing_stmt).all()
        return [_assignment_to_public(db_session, a) for a in all_assignments]

    @staticmethod
    def remove_assignment(
        db_session: DBSession,
        binding_id: uuid.UUID,
        owner_id: uuid.UUID,
        target_user_id: uuid.UUID,
    ) -> bool:
        """Remove a user assignment from a binding. Returns False if not found."""
        binding = db_session.get(IdentityAgentBinding, binding_id)
        if not binding or binding.owner_id != owner_id:
            return False

        stmt = select(IdentityBindingAssignment).where(
            IdentityBindingAssignment.binding_id == binding_id,
            IdentityBindingAssignment.target_user_id == target_user_id,
        )
        assignment = db_session.exec(stmt).first()
        if not assignment:
            return False

        db_session.delete(assignment)
        db_session.commit()
        return True

    # ------------------------------------------------------------------
    # User-facing (target user perspective)
    # ------------------------------------------------------------------

    @staticmethod
    def get_identity_contacts(
        db_session: DBSession,
        user_id: uuid.UUID,
    ) -> list[IdentityContactPublic]:
        """List people who shared agents with this user via identity.

        Groups by owner — one IdentityContactPublic per distinct identity owner.
        is_enabled is True if ALL of this owner's assignments to the user are enabled
        (for simplicity; the per-person toggle enables/disables all at once).
        """
        # Get all assignments for this target user where binding is active
        stmt = (
            select(IdentityBindingAssignment, IdentityAgentBinding)
            .join(
                IdentityAgentBinding,
                IdentityAgentBinding.id == IdentityBindingAssignment.binding_id,
            )
            .where(
                IdentityBindingAssignment.target_user_id == user_id,
                IdentityBindingAssignment.is_active == True,  # noqa: E712
                IdentityAgentBinding.is_active == True,  # noqa: E712
            )
        )
        rows = db_session.exec(stmt).all()

        # Group by owner_id
        owner_data: dict[uuid.UUID, dict] = {}
        for assignment, binding in rows:
            oid = binding.owner_id
            if oid not in owner_data:
                owner_data[oid] = {
                    "assignment_ids": [],
                    "enabled_flags": [],
                    "agent_count": 0,
                }
            owner_data[oid]["assignment_ids"].append(assignment.id)
            owner_data[oid]["enabled_flags"].append(assignment.is_enabled)
            owner_data[oid]["agent_count"] += 1

        contacts: list[IdentityContactPublic] = []
        for owner_id, data in owner_data.items():
            owner = db_session.get(User, owner_id)
            if not owner:
                continue
            # Per-person toggle: consider enabled if ANY assignment is enabled
            is_enabled = any(data["enabled_flags"])
            contacts.append(
                IdentityContactPublic(
                    owner_id=owner_id,
                    owner_name=owner.full_name or "",
                    owner_email=owner.email or "",
                    is_enabled=is_enabled,
                    agent_count=data["agent_count"],
                    assignment_ids=data["assignment_ids"],
                )
            )

        return contacts

    @staticmethod
    def toggle_identity_contact(
        db_session: DBSession,
        owner_id: uuid.UUID,
        user_id: uuid.UUID,
        is_enabled: bool,
    ) -> bool:
        """Toggle all assignments from a given owner for the target user.

        Per-person toggle: affects all assignments from that owner to this user.
        Returns False if no assignments found.
        """
        stmt = (
            select(IdentityBindingAssignment)
            .join(
                IdentityAgentBinding,
                IdentityAgentBinding.id == IdentityBindingAssignment.binding_id,
            )
            .where(
                IdentityAgentBinding.owner_id == owner_id,
                IdentityBindingAssignment.target_user_id == user_id,
            )
        )
        assignments = db_session.exec(stmt).all()
        if not assignments:
            return False

        for assignment in assignments:
            assignment.is_enabled = is_enabled
            db_session.add(assignment)

        db_session.commit()
        return True

    #: What a caller is told when any identity check fails. One message for
    #: every condition, deliberately: the reasons differ (revoked, disabled,
    #: opted out, mismatched ids) but the caller is somebody else's guest, and
    #: naming *which* fact failed would tell them about the owner's
    #: configuration. The specific reason goes to the log.
    IDENTITY_REVOKED_MESSAGE = "This identity connection is no longer active."

    @staticmethod
    def _deny(reason: str, **context: Any) -> str:
        """Log the real reason, return the caller-safe one."""
        logger.warning(
            "[Identity] Access denied: %s (%s)",
            reason,
            " ".join(f"{k}={v}" for k, v in context.items()),
        )
        return IdentityService.IDENTITY_REVOKED_MESSAGE

    @staticmethod
    def _live_binding(
        db: DBSession, binding_id: uuid.UUID
    ) -> tuple[IdentityAgentBinding | None, str | None]:
        """Condition 1 — the binding exists and the owner has it switched on.

        Returns ``(binding, None)`` or ``(None, message)``. One implementation,
        shared by the session-resume check and the access-grant check, so a
        liveness rule can never be tightened on one path and forgotten on the
        other — which for an authorization check is how one side quietly stops
        enforcing what the other still does.
        """
        binding = db.get(IdentityAgentBinding, binding_id)
        if binding is None or not binding.is_active:
            return None, IdentityService._deny(
                "binding is missing or inactive", binding=binding_id,
            )
        return binding, None

    @staticmethod
    def _live_assignment(
        db: DBSession, assignment_id: uuid.UUID
    ) -> tuple[IdentityBindingAssignment | None, str | None]:
        """Condition 2 — the assignment exists, the owner has it on, the caller opted in.

        Sibling of :meth:`_live_binding`; see there for why these are shared.
        """
        assignment = db.get(IdentityBindingAssignment, assignment_id)
        if assignment is None or not assignment.is_active or not assignment.is_enabled:
            return None, IdentityService._deny(
                "assignment is missing, inactive, or not enabled",
                assignment=assignment_id,
            )
        return assignment, None

    @staticmethod
    def verify_identity_access(
        db: DBSession,
        *,
        owner_id: uuid.UUID | None,
        binding_id: uuid.UUID | None,
        assignment_id: uuid.UUID | None,
        caller_user_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
    ) -> str | None:
        """Re-verify an identity **authorization claim** against the database.

        This is what stands behind ``ChannelAccessPolicy.identity_grant``: a
        set of ids handed in by the routing layer, asserting that this caller
        may hold a session on this agent inside that owner's workspace. It is a
        claim, not a conclusion — the routing decision and the session creation
        are separated by a worker-thread hop and possibly an auto-install wait,
        and the owner may have revoked in between.

        **Six conditions, all of them, every time.** A grant that passes because
        only five were checked is the failure mode that matters here:

        1. the ``IdentityAgentBinding`` exists and is ``is_active``
        2. the ``IdentityBindingAssignment`` exists, ``is_active``, ``is_enabled``
        3. ``assignment.binding_id == binding.id``
        4. ``assignment.target_user_id == caller_user_id``
        5. ``binding.agent_id == agent_id``
        6. ``binding.owner_id == agent.owner_id == owner_id``

        1–2 come from :meth:`_live_binding` / :meth:`_live_assignment`, shared
        with the resume check.
        3–6 are specific to a claim: they are what stops three individually-live
        rows belonging to three different authorizations from being assembled
        into a fourth one that never existed. A resume check does not need them
        (see :meth:`check_session_validity`); a grant cannot do without them.

        Returns ``None`` when every condition holds, else a caller-safe message.
        """
        if binding_id is None or assignment_id is None:
            return IdentityService._deny(
                "grant is missing a binding or assignment id",
                binding=binding_id, assignment=assignment_id,
            )

        binding, denial = IdentityService._live_binding(db, binding_id)
        if denial:
            return denial
        assignment, denial = IdentityService._live_assignment(db, assignment_id)
        if denial:
            return denial

        # 3. The assignment belongs to *this* binding.
        if assignment.binding_id != binding.id:
            return IdentityService._deny(
                "assignment belongs to a different binding",
                assignment=assignment_id, binding=binding_id,
            )

        # 4. The assignment was issued to *this* caller.
        if caller_user_id is None or assignment.target_user_id != caller_user_id:
            return IdentityService._deny(
                "assignment was issued to a different user",
                assignment=assignment_id, caller=caller_user_id,
            )

        # 5. The binding exposes *this* agent.
        if agent_id is None or binding.agent_id != agent_id:
            return IdentityService._deny(
                "binding exposes a different agent",
                binding=binding_id, agent=agent_id,
            )

        # 6. The binding's owner is the agent's owner and the claimed owner.
        agent = db.get(Agent, binding.agent_id)
        if agent is None:
            return IdentityService._deny(
                "binding points at an agent that no longer exists",
                binding=binding_id,
            )
        if owner_id is None or not (binding.owner_id == agent.owner_id == owner_id):
            return IdentityService._deny(
                "binding owner, agent owner and claimed owner disagree",
                binding_owner=binding.owner_id,
                agent_owner=agent.owner_id,
                claimed_owner=owner_id,
            )

        return None

    @staticmethod
    def check_session_validity(
        db: DBSession,
        session: Any,
    ) -> str | None:
        """Verify an existing identity session is still authorized.

        This is the canonical implementation shared by the App MCP and External A2A
        handlers.  Both ``A2ARequestHandler._check_identity_session_validity`` and
        ``AppMCPRequestHandler._check_identity_session_validity`` delegate here.

        **Liveness only — conditions 1 and 2 — and that is the right scope.**
        The linkage conditions 3–6 that :meth:`verify_identity_access` adds
        answer "do these ids actually belong together", which is a question
        about a claim someone handed in. A session's ids were not handed in:
        they were written together, in one statement, by
        ``ChannelIngestionService.create_identity_session`` *after* the grant
        was verified, and none of the fields they link (``binding.agent_id``,
        ``binding.owner_id``, ``assignment.binding_id``,
        ``assignment.target_user_id``) is editable afterwards. So 3–6 are
        invariants of how the row was made rather than facts to re-derive, and
        re-deriving them here would only reject rows written past the API — at
        the cost of turning a revocation check into an integrity check.

        What *can* change after creation is exactly what this does re-read: the
        owner can deactivate the binding or the assignment, and the caller can
        opt out. That is the whole point of the per-message check.

        Args:
            db: Active database session.
            session: A ``Session`` ORM row that may carry ``identity_binding_id``
                and ``identity_binding_assignment_id`` fields.

        Returns:
            ``None`` when the session is still valid.
            An error message string when the identity authorization no longer holds.
        """
        # A session carrying neither id is not identity-routed.
        if not session.identity_binding_id and not session.identity_binding_assignment_id:
            return None

        # Historically each id was checked independently, so a session with one
        # of the two set was validated on that one alone. Preserved: the pair is
        # always written together today, but a row from before that was true
        # must not start failing on the half it never had.
        if session.identity_binding_id:
            _, denial = IdentityService._live_binding(db, session.identity_binding_id)
            if denial:
                return denial

        if session.identity_binding_assignment_id:
            _, denial = IdentityService._live_assignment(
                db, session.identity_binding_assignment_id
            )
            if denial:
                return denial

        return None
