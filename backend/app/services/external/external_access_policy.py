"""
ExternalAccessPolicy — centralised access-check layer for the External A2A surface.

Both target types (agent, identity) funnel through the methods here so access
rules live in exactly one place.

Raises domain exceptions from ``app.services.external.errors`` instead of plain
``ValueError`` — the route and request-handler layers catch those and translate to
JSON-RPC / HTTP error responses.
"""
from __future__ import annotations

import uuid
import logging
from typing import Optional

from sqlmodel import Session as DBSession

from app.models import Agent, User
from app.models.environments.environment import AgentEnvironment
from app.models.identity.identity_models import IdentityAgentBinding
from app.services.external.errors import (
    NoActiveEnvironmentError,
    TargetNotAccessibleError,
)
from app.services.identity.identity_service import IdentityService

logger = logging.getLogger(__name__)


class ExternalAccessPolicy:
    """Stateless access-check layer for the External A2A surface.

    All methods are static — this class is a namespace, not a service instance.
    """

    @staticmethod
    def resolve_agent(
        db: DBSession,
        user: User,
        agent_id: uuid.UUID,
        *,
        require_env: bool = False,
    ) -> tuple[Agent, Optional[AgentEnvironment]]:
        """Resolve and verify ownership of an agent.

        Ownership rule: ``agent.owner_id`` must equal ``user.id``.
        Does NOT require ``a2a_config.enabled``.

        Args:
            db: Active database session.
            user: Authenticated caller — must be the agent owner.
            agent_id: UUID of the agent to resolve.
            require_env: When ``True``, also require an active environment and
                return it in the tuple.  Raises ``NoActiveEnvironmentError`` when
                the agent has no active environment.

        Returns:
            ``(agent, environment)`` — ``environment`` is ``None`` when
            ``require_env=False`` and no environment resolution is performed.

        Raises:
            TargetNotAccessibleError: Agent not found or not owned by caller.
            NoActiveEnvironmentError: ``require_env=True`` and no active environment.
        """
        agent = db.get(Agent, agent_id)
        if not agent or agent.owner_id != user.id:
            raise TargetNotAccessibleError("Agent not found or access denied")

        if not require_env:
            return agent, None

        environment = ExternalAccessPolicy._require_active_environment(db, agent)
        return agent, environment

    @staticmethod
    def require_identity_access(
        db: DBSession,
        user: User,
        owner_id: uuid.UUID,
    ) -> list[IdentityAgentBinding]:
        """Verify the caller has at least one active binding with the identity owner.

        Args:
            db: Active database session.
            user: Authenticated caller.
            owner_id: UUID of the identity owner (the other User).

        Returns:
            Non-empty list of active ``IdentityAgentBinding`` objects that the
            caller is currently allowed to reach.

        Raises:
            TargetNotAccessibleError: Identity owner not found or the caller has no
                active accessible bindings.
        """
        owner = db.get(User, owner_id)
        if not owner:
            raise TargetNotAccessibleError("Identity not found or access denied")

        bindings = IdentityService.get_active_bindings_for_user(
            db_session=db,
            owner_id=owner_id,
            target_user_id=user.id,
        )
        if not bindings:
            raise TargetNotAccessibleError("Identity not found or access denied")

        return bindings

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_active_environment(
        db: DBSession,
        agent: Agent,
    ) -> AgentEnvironment:
        """Return the active AgentEnvironment or raise NoActiveEnvironmentError."""
        if not agent.active_environment_id:
            raise NoActiveEnvironmentError()
        env = db.get(AgentEnvironment, agent.active_environment_id)
        if not env:
            raise NoActiveEnvironmentError()
        return env
