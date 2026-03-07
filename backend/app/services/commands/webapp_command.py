"""
/webapp command - return the shareable webapp link for the current agent.

Looks up the first active, non-expired webapp share for the agent and returns
its public URL. If no share exists or the webapp feature is disabled, returns
an informational message.
"""
import logging

from app.models import Agent
from app.services.command_service import CommandHandler, CommandContext, CommandResult
from app.services.agent_webapp_share_service import AgentWebappShareService
from app.core.db import create_session

logger = logging.getLogger(__name__)


class WebappCommandHandler(CommandHandler):
    """Handler for /webapp — returns the first available webapp share link."""

    @property
    def name(self) -> str:
        return "/webapp"

    @property
    def description(self) -> str:
        return "Get the shareable webapp link for this agent"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        with create_session() as db:
            agent = db.get(Agent, context.agent_id)
            if not agent:
                return CommandResult(content="Agent not found.", is_error=True)

            if not agent.webapp_enabled:
                return CommandResult(content="No Web App available for this agent.")

            result = AgentWebappShareService.get_first_active_share_info(
                db, context.agent_id,
            )
            if not result:
                return CommandResult(content="No Web App available for this agent.")

            share_url, security_code = result
            lines = [f"**Web App:** [{share_url}]({share_url})"]
            if security_code:
                lines.append(f"**Access Code:** `{security_code}`")
            return CommandResult(content="\n".join(lines))
