"""
/rebuild-env command - rebuild the active environment for the current agent.

Performs the same operation as clicking the "Rebuild" button on the environment
panel. Rebuilds the Docker image with updated core files while preserving
workspace data. Fails if any session connected to this environment is actively
streaming.
"""
import logging

from sqlmodel import select

from app.models import AgentEnvironment
from app.models.session import Session
from app.services.command_service import CommandHandler, CommandContext, CommandResult
from app.services.active_streaming_manager import active_streaming_manager
from app.core.db import create_session as create_db_session

logger = logging.getLogger(__name__)


class RebuildEnvCommandHandler(CommandHandler):
    """Handler for /rebuild-env — rebuild the agent's active environment."""

    @property
    def name(self) -> str:
        return "/rebuild-env"

    @property
    def description(self) -> str:
        return "Rebuild the active environment for this agent"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        from app.services.environment_service import EnvironmentService, AgentEnvironmentError

        # Phase 1: Validate state and check for active streaming
        with create_db_session() as db:
            environment = db.get(AgentEnvironment, context.environment_id)
            if not environment:
                return CommandResult(content="Environment not found.", is_error=True)

            # Extra safety for chat context where user can't see the environment panel
            if environment.status not in ("running", "stopped", "error", "suspended"):
                return CommandResult(
                    content=f"Cannot rebuild environment — current status is **{environment.status}**. "
                            f"Environment must be running, stopped, suspended, or in error state to rebuild.",
                    is_error=True,
                )

            # Check for active streaming on any session connected to this environment
            session_ids = set(
                db.exec(
                    select(Session.id).where(Session.environment_id == context.environment_id)
                ).all()
            )

            if session_ids and await active_streaming_manager.is_any_session_streaming(session_ids):
                return CommandResult(
                    content="Cannot rebuild environment — an active streaming session is in progress. "
                            "Please wait for the current response to complete or interrupt it first.",
                    is_error=True,
                )

        # Phase 2: Perform the rebuild
        try:
            with create_db_session() as db:
                await EnvironmentService.rebuild_environment(session=db, env_id=context.environment_id)
            return CommandResult(
                content="Environment rebuild initiated. The environment will be back online shortly."
            )
        except AgentEnvironmentError as e:
            return CommandResult(content=f"Failed to rebuild environment: {e.message}", is_error=True)
        except Exception as e:
            logger.error(f"Failed to rebuild environment {context.environment_id}: {e}", exc_info=True)
            return CommandResult(
                content=f"Failed to rebuild environment: {str(e)}",
                is_error=True,
            )
