"""
/session-recover command - recover a session from a lost SDK connection.

Performs the same recovery as the UI "Recover Session" button:
- Clears SDK session metadata (external_session_id, sdk_type, last_sdk_message_id)
- Sets recovery_pending flag so the next message includes conversation history
- If a failed user message is detected, resets it for automatic re-send
- Creates a "Session recovered" system message
- Triggers streaming for the resendable message if found

The handler implements its own recovery logic (rather than calling
mark_session_for_recovery) because the command's own user message is
already in the DB when the handler runs, which would break the
trailing-error detection pattern.
"""
import logging
from datetime import UTC, datetime

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.services.agents.command_service import CommandHandler, CommandContext, CommandResult
from app.core.db import create_session as create_db_session

logger = logging.getLogger(__name__)


class SessionRecoverCommandHandler(CommandHandler):
    """Handler for /session-recover — recover session from lost SDK connection."""

    @property
    def name(self) -> str:
        return "/session-recover"

    @property
    def description(self) -> str:
        return "Recover session from a lost SDK connection"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        from app.models import Session, SessionMessage
        from app.services.sessions.message_service import MessageService
        from app.services.sessions.session_service import SessionService

        with create_db_session() as db:
            session = db.get(Session, context.session_id)
            if not session:
                return CommandResult(content="Session not found.", is_error=True)

            # Clear SDK session metadata (forces fresh SDK session on next message)
            session.session_metadata.pop("external_session_id", None)
            session.session_metadata.pop("sdk_type", None)
            session.session_metadata.pop("last_sdk_message_id", None)
            session.session_metadata["recovery_pending"] = True
            flag_modified(session, "session_metadata")
            session.status = "active"
            session.updated_at = datetime.now(UTC)
            db.add(session)

            # Detect failed user message pattern, skipping the command message
            # itself (which is the most recent user message at this point).
            has_resendable = False
            messages = list(db.exec(
                select(SessionMessage)
                .where(SessionMessage.session_id == context.session_id)
                .order_by(SessionMessage.sequence_number.desc())
                .limit(20)
            ).all())

            # Skip the command message (most recent user message with /session-recover)
            start_idx = 0
            if (
                messages
                and messages[0].role == "user"
                and messages[0].content
                and messages[0].content.strip().lower().startswith("/session-recover")
            ):
                start_idx = 1

            # Walk backwards: skip system error messages
            i = start_idx
            while (
                i < len(messages)
                and messages[i].role == "system"
                and messages[i].status == "error"
            ):
                i += 1

            # If we skipped at least one error and the next is a user message, reset it
            if i > start_idx and i < len(messages) and messages[i].role == "user":
                messages[i].sent_to_agent_status = "pending"
                db.add(messages[i])
                has_resendable = True
                logger.info(
                    f"Recovery command: reset user message {messages[i].id} to pending"
                )

            db.commit()

            # Create "Session recovered" system message
            MessageService.create_message(
                session=db,
                session_id=context.session_id,
                role="system",
                content="Session recovered",
            )

        if has_resendable:
            await SessionService.initiate_stream(
                session_id=context.session_id,
                get_fresh_db_session=create_db_session,
            )
            return CommandResult(
                content="Session recovered. Resending last message."
            )

        return CommandResult(
            content="Session recovered. Send a new message to continue with conversation history."
        )
