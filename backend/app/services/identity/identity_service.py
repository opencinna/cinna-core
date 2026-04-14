"""
Identity Service — business logic for identity agent bindings and assignments.

The Identity MCP Server allows users to expose themselves as a routable identity.
Callers address people by name; Stage 2 routing selects the right agent from
the identity owner's portfolio (filtered to those accessible to the specific caller).
"""
import uuid
import logging
from datetime import datetime, UTC

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
        message_patterns=binding.message_patterns,
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
            message_patterns=data.message_patterns,
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
