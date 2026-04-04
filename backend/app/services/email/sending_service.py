"""
Email Sending Service - Queues and sends agent responses as emails via SMTP.

After an agent responds in an email-initiated session, the response is queued
in the outgoing_email_queue table. This service processes the queue and sends
emails via the parent agent's SMTP configuration.
"""
import logging
import uuid
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.email.agent_email_integration import AgentEmailIntegration
from app.models.email.outgoing_email_queue import OutgoingEmailQueue, OutgoingEmailStatus
from app.models.sessions.session import Session as ChatSession, SessionMessage
from app.models.users.user import User
from app.services.email.mail_server_service import MailServerService
from app.services.email.smtp_connector import smtp_connector

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class EmailSendingService:

    @staticmethod
    def queue_outgoing_email(
        db_session: Session,
        session_id: uuid.UUID,
        message_id: uuid.UUID,
    ) -> OutgoingEmailQueue | None:
        """
        Queue an agent response for email sending.

        Looks up session -> clone -> parent agent -> SMTP config,
        determines recipient from clone owner's email, and creates
        an outgoing queue entry.

        Returns the queue entry, or None if not applicable.
        """
        # Get session
        chat_session = db_session.get(ChatSession, session_id)
        if not chat_session:
            logger.warning(f"Session {session_id} not found for email queueing")
            return None

        # Only process email-integration sessions
        if chat_session.integration_type != "email":
            return None

        # Get the agent message
        message = db_session.get(SessionMessage, message_id)
        if not message or message.role != "agent":
            return None

        # Get environment -> agent (clone or owner agent)
        from app.models.environments.environment import AgentEnvironment
        from app.models.email.agent_email_integration import AgentSessionMode
        env = db_session.get(AgentEnvironment, chat_session.environment_id)
        if not env:
            logger.warning(f"Environment {chat_session.environment_id} not found")
            return None

        session_agent = db_session.get(Agent, env.agent_id)
        if not session_agent:
            logger.warning(f"Session agent {env.agent_id} not found")
            return None

        # Determine if this is owner mode or clone mode
        is_owner_mode = session_agent.parent_agent_id is None and chat_session.sender_email is not None

        if is_owner_mode:
            # Owner mode: session is on the parent agent itself
            parent_agent_id = session_agent.id
            clone_agent_id_for_queue = None

            # Recipient is the original email sender (stored on session)
            recipient = chat_session.sender_email
            if not recipient:
                logger.warning(f"Owner-mode session {session_id} has no sender_email")
                return None
        else:
            # Clone mode: session is on a clone, parent owns the email integration
            if not session_agent.parent_agent_id:
                logger.warning(f"Agent {session_agent.id} has no parent agent")
                return None
            parent_agent_id = session_agent.parent_agent_id
            clone_agent_id_for_queue = session_agent.id

            # Recipient is the clone owner (= email sender's user account)
            recipient_user = db_session.get(User, session_agent.owner_id)
            if not recipient_user:
                logger.warning(f"Clone owner {session_agent.owner_id} not found")
                return None
            recipient = recipient_user.email

        # Get email integration config from parent
        stmt = select(AgentEmailIntegration).where(
            AgentEmailIntegration.agent_id == parent_agent_id,
        )
        integration = db_session.exec(stmt).first()
        if not integration or not integration.outgoing_server_id:
            logger.warning(
                f"Parent agent {parent_agent_id} has no outgoing email config"
            )
            return None

        # Build email subject and threading headers
        subject = EmailSendingService._build_reply_subject(chat_session)
        references = chat_session.email_thread_id or ""
        in_reply_to = chat_session.email_thread_id

        # Create queue entry
        queue_entry = OutgoingEmailQueue(
            agent_id=parent_agent_id,
            clone_agent_id=clone_agent_id_for_queue,
            session_id=session_id,
            message_id=message_id,
            recipient=recipient,
            subject=subject,
            body=message.content,
            references=references if references else None,
            in_reply_to=in_reply_to,
            status=OutgoingEmailStatus.PENDING,
        )
        db_session.add(queue_entry)
        db_session.commit()
        db_session.refresh(queue_entry)

        logger.info(
            f"Queued outgoing email: session={session_id}, "
            f"recipient={recipient}, subject={subject}"
        )
        return queue_entry

    @staticmethod
    def send_pending_emails(db_session: Session) -> int:
        """
        Process the outgoing email queue: send all pending emails.

        Returns the number of emails successfully sent.
        """
        stmt = select(OutgoingEmailQueue).where(
            OutgoingEmailQueue.status == OutgoingEmailStatus.PENDING,
            OutgoingEmailQueue.retry_count < MAX_RETRIES,
        )
        pending = db_session.exec(stmt).all()

        if not pending:
            return 0

        logger.info(f"Processing {len(pending)} pending outgoing emails")

        sent_count = 0
        for entry in pending:
            try:
                EmailSendingService._send_single_email(db_session, entry)
                sent_count += 1
            except Exception as e:
                logger.error(
                    f"Failed to send email {entry.id}: {e}", exc_info=True
                )
                # Error already recorded in _send_single_email
                continue

        return sent_count

    @staticmethod
    def _send_single_email(
        db_session: Session,
        entry: OutgoingEmailQueue,
    ) -> None:
        """Send a single email from the queue."""
        # Get parent agent's email integration for SMTP config
        stmt = select(AgentEmailIntegration).where(
            AgentEmailIntegration.agent_id == entry.agent_id,
        )
        integration = db_session.exec(stmt).first()
        if not integration or not integration.outgoing_server_id:
            EmailSendingService._mark_failed(
                db_session, entry, "No outgoing server configured"
            )
            return

        # Get SMTP credentials
        result = MailServerService.get_mail_server_with_credentials(
            db_session, integration.outgoing_server_id
        )
        if not result:
            EmailSendingService._mark_failed(
                db_session, entry, "SMTP server not found"
            )
            return

        server, password = result
        from_address = integration.outgoing_from_address

        # Build email message
        msg = EmailSendingService._build_email_message(
            from_address=from_address,
            to_address=entry.recipient,
            subject=entry.subject,
            body=entry.body,
            in_reply_to=entry.in_reply_to,
            references=entry.references,
        )

        # Send via SMTP
        try:
            smtp_connector.send(server, password, from_address, entry.recipient, msg)
        except Exception as e:
            entry.retry_count += 1
            entry.last_error = str(e)
            entry.updated_at = datetime.now(UTC)
            if entry.retry_count >= MAX_RETRIES:
                entry.status = OutgoingEmailStatus.FAILED
                logger.error(
                    f"Email {entry.id}: max retries reached, marking failed: {e}"
                )
            db_session.add(entry)
            db_session.commit()
            return

        # Mark as sent
        entry.status = OutgoingEmailStatus.SENT
        entry.sent_at = datetime.now(UTC)
        entry.updated_at = datetime.now(UTC)
        db_session.add(entry)
        db_session.commit()

        logger.info(f"Email {entry.id}: sent to {entry.recipient}")

    @staticmethod
    def _build_email_message(
        from_address: str,
        to_address: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> MIMEMultipart:
        """Build a MIME email message."""
        msg = MIMEMultipart("alternative")
        msg["From"] = from_address
        msg["To"] = to_address
        msg["Subject"] = subject

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

        # Plain text body
        msg.attach(MIMEText(body, "plain", "utf-8"))

        return msg

    @staticmethod
    def _build_reply_subject(chat_session: ChatSession) -> str:
        """Build a reply subject from the session title or thread."""
        title = chat_session.title or "Agent Response"
        if not title.lower().startswith("re:"):
            title = f"Re: {title}"
        return title

    @staticmethod
    def _mark_failed(
        db_session: Session,
        entry: OutgoingEmailQueue,
        error: str,
    ) -> None:
        """Mark a queue entry as permanently failed."""
        entry.status = OutgoingEmailStatus.FAILED
        entry.last_error = error
        entry.updated_at = datetime.now(UTC)
        db_session.add(entry)
        db_session.commit()
        logger.error(f"Email {entry.id}: permanently failed: {error}")

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """
        Event handler for STREAM_COMPLETED - queue email reply if session is email-initiated.

        Registered in main.py to listen for STREAM_COMPLETED events.
        When the agent finishes responding in an email-integration session,
        queues the agent's response for sending via SMTP.
        """
        try:
            from app.core.db import create_session

            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            was_interrupted = meta.get("was_interrupted", False)

            if not session_id or was_interrupted:
                return

            with create_session() as db_session:
                # Check if this is an email session
                chat_session = db_session.get(ChatSession, uuid.UUID(session_id))
                if not chat_session or chat_session.integration_type != "email":
                    return

                # Find the last agent message in this session
                stmt = (
                    select(SessionMessage)
                    .where(
                        SessionMessage.session_id == uuid.UUID(session_id),
                        SessionMessage.role == "agent",
                    )
                    .order_by(SessionMessage.sequence_number.desc())
                    .limit(1)
                )
                last_agent_msg = db_session.exec(stmt).first()
                if not last_agent_msg:
                    logger.debug(
                        f"No agent message found for session {session_id}, "
                        "skipping email queue"
                    )
                    return

                # Queue it for sending
                EmailSendingService.queue_outgoing_email(
                    db_session=db_session,
                    session_id=uuid.UUID(session_id),
                    message_id=last_agent_msg.id,
                )

        except Exception as e:
            logger.error(
                f"Failed to queue email for stream_completed event: {e}",
                exc_info=True,
            )
