"""
Agent Handover Service - Business logic for agent handover configurations.
"""
import uuid
import logging
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models import (
    Agent,
    AgentHandoverConfig,
    HandoverConfigCreate,
    HandoverConfigUpdate,
    HandoverConfigPublic,
    HandoverConfigsPublic,
)

logger = logging.getLogger(__name__)


class HandoverError(Exception):
    """Base exception for handover service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class HandoverNotFoundError(HandoverError):
    def __init__(self, message: str = "Handover configuration not found"):
        super().__init__(message, status_code=404)


class AgentNotFoundError(HandoverError):
    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class PermissionDeniedError(HandoverError):
    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=400)


class AgentHandoverService:
    """Service for managing agent handover configurations."""

    @staticmethod
    def verify_agent_access(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        is_superuser: bool = False,
    ) -> Agent:
        """Verify agent exists and user has access."""
        agent = session.get(Agent, agent_id)
        if not agent:
            raise AgentNotFoundError()
        if not is_superuser and agent.owner_id != user_id:
            raise PermissionDeniedError()
        return agent

    @staticmethod
    def _config_to_public(
        session: Session, config: AgentHandoverConfig
    ) -> HandoverConfigPublic:
        """Convert a handover config to its public representation."""
        target_agent = session.get(Agent, config.target_agent_id)
        return HandoverConfigPublic(
            id=config.id,
            source_agent_id=config.source_agent_id,
            target_agent_id=config.target_agent_id,
            target_agent_name=target_agent.name if target_agent else "Unknown",
            handover_prompt=config.handover_prompt,
            enabled=config.enabled,
            auto_feedback=config.auto_feedback,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )

    @staticmethod
    def list_configs(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        is_superuser: bool = False,
    ) -> HandoverConfigsPublic:
        """List all handover configs for an agent."""
        AgentHandoverService.verify_agent_access(
            session, agent_id, user_id, is_superuser=is_superuser
        )

        statement = select(AgentHandoverConfig).where(
            AgentHandoverConfig.source_agent_id == agent_id
        )
        configs = session.exec(statement).all()

        public_configs = [
            AgentHandoverService._config_to_public(session, c) for c in configs
        ]
        return HandoverConfigsPublic(data=public_configs, count=len(public_configs))

    @staticmethod
    async def create_config(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: HandoverConfigCreate,
        *,
        is_superuser: bool = False,
    ) -> HandoverConfigPublic:
        """Create a new handover configuration."""
        from app.services.agent_service import AgentService

        AgentHandoverService.verify_agent_access(
            session, agent_id, user_id, is_superuser=is_superuser
        )

        # Verify target agent exists and user has access
        target_agent = session.get(Agent, data.target_agent_id)
        if not target_agent:
            raise AgentNotFoundError("Target agent not found")
        if not is_superuser and target_agent.owner_id != user_id:
            raise PermissionDeniedError("Not enough permissions to access target agent")

        if agent_id == data.target_agent_id:
            raise HandoverError("Cannot create handover to the same agent")

        config = AgentHandoverConfig(
            source_agent_id=agent_id,
            target_agent_id=data.target_agent_id,
            handover_prompt=data.handover_prompt,
            enabled=True,
        )
        session.add(config)
        session.commit()
        session.refresh(config)

        await AgentService.sync_agent_handover_config(session, agent_id)

        return AgentHandoverService._config_to_public(session, config)

    @staticmethod
    async def update_config(
        session: Session,
        agent_id: uuid.UUID,
        handover_id: uuid.UUID,
        user_id: uuid.UUID,
        data: HandoverConfigUpdate,
        *,
        is_superuser: bool = False,
    ) -> HandoverConfigPublic:
        """Update a handover configuration."""
        from app.services.agent_service import AgentService

        AgentHandoverService.verify_agent_access(
            session, agent_id, user_id, is_superuser=is_superuser
        )

        config = session.get(AgentHandoverConfig, handover_id)
        if not config or config.source_agent_id != agent_id:
            raise HandoverNotFoundError()

        if data.handover_prompt is not None:
            config.handover_prompt = data.handover_prompt
        if data.enabled is not None:
            config.enabled = data.enabled
        if data.auto_feedback is not None:
            config.auto_feedback = data.auto_feedback

        config.updated_at = datetime.now(UTC)
        session.add(config)
        session.commit()
        session.refresh(config)

        await AgentService.sync_agent_handover_config(session, agent_id)

        return AgentHandoverService._config_to_public(session, config)

    @staticmethod
    async def delete_config(
        session: Session,
        agent_id: uuid.UUID,
        handover_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        is_superuser: bool = False,
    ) -> None:
        """Delete a handover configuration."""
        from app.services.agent_service import AgentService

        AgentHandoverService.verify_agent_access(
            session, agent_id, user_id, is_superuser=is_superuser
        )

        config = session.get(AgentHandoverConfig, handover_id)
        if not config or config.source_agent_id != agent_id:
            raise HandoverNotFoundError()

        session.delete(config)
        session.commit()

        await AgentService.sync_agent_handover_config(session, agent_id)

    @staticmethod
    def generate_handover_prompt(
        session: Session,
        agent_id: uuid.UUID,
        target_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        is_superuser: bool = False,
    ) -> dict:
        """Generate handover prompt using AI."""
        from app.services.ai_functions_service import AIFunctionsService

        source_agent = AgentHandoverService.verify_agent_access(
            session, agent_id, user_id, is_superuser=is_superuser
        )

        target_agent = session.get(Agent, target_agent_id)
        if not target_agent:
            raise AgentNotFoundError("Target agent not found")
        if not is_superuser and target_agent.owner_id != user_id:
            raise PermissionDeniedError("Not enough permissions to access target agent")

        if agent_id == target_agent_id:
            raise HandoverError("Cannot create handover to the same agent")

        return AIFunctionsService.generate_handover_prompt(
            source_agent_name=source_agent.name,
            source_entrypoint=source_agent.entrypoint_prompt,
            source_workflow=source_agent.workflow_prompt,
            target_agent_name=target_agent.name,
            target_entrypoint=target_agent.entrypoint_prompt,
            target_workflow=target_agent.workflow_prompt,
        )
