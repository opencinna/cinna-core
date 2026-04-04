"""
/session-reset command - reset SDK session for a clean slate.

Unlike /session-recover, this command:
- Does NOT set recovery_pending (no conversation history injected)
- Does NOT detect or auto-resend failed messages
- Simply clears SDK metadata so the next message starts a fresh conversation
"""
import logging

from app.services.agents.command_service import CommandHandler, CommandContext, CommandResult
from app.core.db import create_session as create_db_session

logger = logging.getLogger(__name__)


class SessionResetCommandHandler(CommandHandler):
    """Handler for /session-reset — reset SDK session for a clean slate."""

    @property
    def name(self) -> str:
        return "/session-reset"

    @property
    def description(self) -> str:
        return "Reset SDK session for a clean slate"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        from app.models import Session
        from app.services.sessions.message_service import MessageService
        from app.services.sessions.session_service import SessionService

        with create_db_session() as db:
            session = db.get(Session, context.session_id)
            if not session:
                return CommandResult(content="Session not found.", is_error=True)

            # Clear SDK session metadata (forces fresh SDK session on next message)
            SessionService.clear_external_session(db, session)

            # Create "Session reset" system message
            MessageService.create_message(
                session=db,
                session_id=context.session_id,
                role="system",
                content="Session reset",
            )

        return CommandResult(
            content="Session reset. Next message will start a fresh conversation with the agent."
        )
