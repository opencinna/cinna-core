"""
Webapp Service - Business logic for serving webapp content from agent environments.
"""
import uuid
import logging
from datetime import UTC, datetime

from sqlmodel import Session

from app.models import Agent, AgentEnvironment
from app.services.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

WEBAPP_SIZE_LIMIT_BYTES = 100 * 1024 * 1024  # 100MB


class WebappError(Exception):
    """Base exception for webapp service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class WebappNotFoundError(WebappError):
    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class WebappPermissionError(WebappError):
    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=403)


class WebappNotAvailableError(WebappError):
    def __init__(self, message: str = "Webapp feature is disabled for this agent"):
        super().__init__(message, status_code=400)


class WebappService:
    """Service for webapp content serving logic."""

    @staticmethod
    def resolve_agent_environment(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
        require_webapp_enabled: bool = True,
    ) -> tuple[Agent, AgentEnvironment]:
        """
        Resolve agent + active running environment with ownership check.

        Raises:
            WebappNotFoundError: Agent not found
            WebappPermissionError: User doesn't own agent
            WebappNotAvailableError: Webapp disabled or no active environment
            WebappError: Environment not running
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise WebappNotFoundError("Agent not found")
        if agent.owner_id != user_id and not is_superuser:
            raise WebappPermissionError("Not enough permissions")
        if require_webapp_enabled and not agent.webapp_enabled:
            raise WebappNotAvailableError("Webapp feature is disabled for this agent")
        if not agent.active_environment_id:
            raise WebappError("No active environment")

        environment = session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            raise WebappError("Active environment not found")
        if environment.status != "running":
            raise WebappError(
                f"Environment must be running (current: {environment.status})"
            )
        return agent, environment

    @staticmethod
    def update_last_activity(session: Session, environment: AgentEnvironment) -> None:
        """Update last_activity_at to keep env alive for webapp traffic."""
        environment.last_activity_at = datetime.now(UTC)
        session.add(environment)
        session.commit()

    @staticmethod
    async def get_public_status(
        session: Session,
        agent: Agent,
        environment: AgentEnvironment | None,
    ) -> dict:
        """
        Get environment status for public webapp polling.

        Returns dict with status/step/message for the loading page.
        """
        if not environment:
            return {"status": "error", "step": "waking_up", "message": "No active environment"}

        if environment.status == "running":
            try:
                lifecycle = EnvironmentService.get_lifecycle_manager()
                adapter = lifecycle.get_adapter(environment)
                status = await adapter.get_webapp_status()
                if status.get("total_size_bytes", 0) > WEBAPP_SIZE_LIMIT_BYTES:
                    return {"status": "error", "step": "loading_app", "message": "Webapp exceeds size limit (100MB). Contact the owner."}
                if status.get("has_index"):
                    return {"status": "running", "step": "ready"}
                return {"status": "error", "step": "loading_app", "message": "Web app not built yet. Ask the agent to build a dashboard first."}
            except Exception:
                return {"status": "running", "step": "loading_app"}
        elif environment.status in ("creating", "starting", "activating"):
            return {"status": "activating", "step": "waking_up"}
        elif environment.status == "suspended":
            try:
                await EnvironmentService.activate_environment(
                    session, agent.id, environment.id
                )
                return {"status": "activating", "step": "waking_up"}
            except Exception as e:
                logger.error(f"Auto-activation failed for env {environment.id}: {e}")
                return {"status": "error", "step": "waking_up", "message": str(e)}
        else:
            return {"status": "error", "step": "waking_up", "message": f"Environment status: {environment.status}"}
