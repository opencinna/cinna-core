"""
Email Processing Service - Routes stored emails to clones and triggers agent responses.

After emails are polled and stored (Phase 4), this service:
1. Routes each email to the correct clone via EmailRoutingService
2. Matches or creates sessions based on email threading
3. Injects the email body as a user message
4. Triggers the agent response via the standard streaming flow
"""
import logging
import uuid
from datetime import UTC, datetime

from sqlmodel import Session as DBSession, select

from app.core.db import create_session
from app.models.email_message import EmailMessage
from app.models.session import Session, SessionCreate
from app.models.input_task import InputTask, InputTaskStatus
from app.models.agent_email_integration import EmailProcessAs
from app.services.email.routing_service import EmailRoutingService, EmailAccessDenied
from app.services.email.integration_service import EmailIntegrationService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


class EmailProcessingService:

    @staticmethod
    async def process_incoming_email(
        db_session: DBSession,
        email_message_id: uuid.UUID,
    ) -> bool:
        """
        Process a single incoming email message.

        1. Load email from DB
        2. Route to clone via EmailRoutingService
        3. If clone not ready, mark pending_clone_creation
        4. If clone ready, match/create session and inject message

        Returns True if processed successfully, False otherwise.
        """
        email_msg = db_session.get(EmailMessage, email_message_id)
        if not email_msg:
            logger.error(f"Email message {email_message_id} not found")
            return False

        if email_msg.processed:
            logger.debug(f"Email {email_message_id} already processed, skipping")
            return True

        try:
            # Check if integration is configured for task mode
            integration = EmailIntegrationService.get_email_integration(
                db_session, email_msg.agent_id
            )
            process_as = (
                integration.process_as
                if integration and hasattr(integration, 'process_as')
                else EmailProcessAs.NEW_SESSION
            )

            # Task mode: create InputTask instead of session
            if process_as == EmailProcessAs.NEW_TASK:
                await EmailProcessingService._process_email_to_task(
                    db_session, email_msg
                )
                return True

            # Session mode: route email to target agent
            target_agent_id, is_ready, session_mode = await EmailRoutingService.route_email(
                session=db_session,
                agent_id=email_msg.agent_id,
                sender_email=email_msg.sender,
            )

            # In owner mode, the "clone_agent_id" field points to the parent agent itself
            email_msg.clone_agent_id = target_agent_id
            email_msg.updated_at = datetime.now(UTC)
            db_session.add(email_msg)
            db_session.commit()

            if not is_ready:
                # Target environment not ready yet - mark for retry
                email_msg.pending_clone_creation = True
                email_msg.updated_at = datetime.now(UTC)
                db_session.add(email_msg)
                db_session.commit()
                logger.info(
                    f"Email {email_message_id}: target agent {target_agent_id} not ready, "
                    "marked pending_clone_creation"
                )
                return False

            # Target is ready - process the email
            await EmailProcessingService._process_email_to_session(
                db_session, email_msg, target_agent_id, session_mode
            )
            return True

        except EmailAccessDenied as e:
            EmailProcessingService._handle_processing_error(
                db_session, email_msg, f"Access denied: {e}"
            )
            return False
        except Exception as e:
            EmailProcessingService._handle_processing_error(
                db_session, email_msg, str(e)
            )
            logger.error(
                f"Failed to process email {email_message_id}: {e}", exc_info=True
            )
            return False

    @staticmethod
    async def process_pending_emails(db_session: DBSession) -> int:
        """
        Process emails waiting for clone readiness.

        Called periodically by the scheduler to retry emails with
        pending_clone_creation=True.

        Returns the number of emails successfully processed.
        """
        stmt = select(EmailMessage).where(
            EmailMessage.processed == False,  # noqa: E712
            EmailMessage.pending_clone_creation == True,  # noqa: E712
            EmailMessage.processing_error.is_(None),  # type: ignore
        )
        pending_emails = db_session.exec(stmt).all()

        if not pending_emails:
            return 0

        logger.info(f"Processing {len(pending_emails)} pending emails")
        processed_count = 0

        for email_msg in pending_emails:
            try:
                # Check if integration switched to task mode
                integration = EmailIntegrationService.get_email_integration(
                    db_session, email_msg.agent_id
                )
                process_as = (
                    integration.process_as
                    if integration and hasattr(integration, 'process_as')
                    else EmailProcessAs.NEW_SESSION
                )

                if process_as == EmailProcessAs.NEW_TASK:
                    await EmailProcessingService._process_email_to_task(
                        db_session, email_msg
                    )
                    processed_count += 1
                    continue

                if not email_msg.clone_agent_id:
                    # Re-route if target agent wasn't set
                    target_agent_id, is_ready, session_mode = await EmailRoutingService.route_email(
                        session=db_session,
                        agent_id=email_msg.agent_id,
                        sender_email=email_msg.sender,
                    )
                    email_msg.clone_agent_id = target_agent_id
                    db_session.add(email_msg)
                    db_session.commit()
                else:
                    target_agent_id = email_msg.clone_agent_id
                    # Determine session_mode from integration config
                    from app.models.agent_email_integration import AgentSessionMode
                    session_mode = (
                        integration.agent_session_mode
                        if integration
                        else AgentSessionMode.CLONE
                    )
                    is_ready = EmailRoutingService._is_clone_ready(
                        db_session, target_agent_id
                    )

                if not is_ready:
                    logger.debug(
                        f"Email {email_msg.id}: target agent {target_agent_id} still not ready"
                    )
                    continue

                await EmailProcessingService._process_email_to_session(
                    db_session, email_msg, target_agent_id, session_mode
                )
                processed_count += 1

            except EmailAccessDenied as e:
                EmailProcessingService._handle_processing_error(
                    db_session, email_msg, f"Access denied: {e}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to process pending email {email_msg.id}: {e}",
                    exc_info=True,
                )
                continue

        if processed_count > 0:
            logger.info(f"Processed {processed_count} pending emails")
        return processed_count

    @staticmethod
    async def _process_email_to_session(
        db_session: DBSession,
        email_msg: EmailMessage,
        target_agent_id: uuid.UUID,
        session_mode: str,
    ) -> None:
        """
        Create/find session and inject email as a user message.

        Supports two session modes:
        - "clone": sessions belong to the clone agent (sender's user space)
        - "owner": sessions belong to the parent agent (owner's user space),
                   sender_email stored on session for reply routing

        1. Determine email thread ID for session matching
        2. Find existing session or create new one
        3. Send message via SessionService.send_session_message
        4. Mark email as processed
        """
        from app.models.agent import Agent
        from app.models.agent_email_integration import AgentSessionMode

        # Determine thread ID: use In-Reply-To or Message-ID for threading
        thread_id = email_msg.in_reply_to or email_msg.email_message_id

        # Resolve the user under whom the session runs
        target_agent = db_session.get(Agent, target_agent_id)
        if not target_agent:
            raise ValueError(f"Target agent {target_agent_id} not found")
        session_user_id = target_agent.owner_id

        # In owner mode, store sender_email on the session for reply routing
        sender_email_for_session = (
            email_msg.sender if session_mode == AgentSessionMode.OWNER else None
        )

        # Try to find existing session by thread
        session_id = None
        if thread_id:
            existing_session = SessionService.get_session_by_email_thread(
                db_session, target_agent_id, thread_id
            )
            if existing_session:
                session_id = existing_session.id
                logger.debug(
                    f"Email {email_msg.id}: matched existing session {session_id} "
                    f"via thread {thread_id}"
                )

        # Format email content for the agent
        content = EmailProcessingService._format_email_as_message(email_msg)

        # Use send_session_message which handles session creation, message creation,
        # and streaming initiation
        def get_fresh_db_session():
            return create_session()

        if session_id:
            # Send to existing session
            result = await SessionService.send_session_message(
                session_id=session_id,
                user_id=session_user_id,
                content=content,
                get_fresh_db_session=get_fresh_db_session,
                initiate_streaming=True,
            )
        else:
            # Create new session on the target agent with email metadata
            with get_fresh_db_session() as fresh_db:
                session_data = SessionCreate(
                    agent_id=target_agent_id,
                    mode="conversation",
                )
                new_session = SessionService.create_session(
                    db_session=fresh_db,
                    user_id=session_user_id,
                    data=session_data,
                    email_thread_id=thread_id,
                    integration_type="email",
                    sender_email=sender_email_for_session,
                )
                if not new_session:
                    raise ValueError(
                        f"Failed to create session on agent {target_agent_id}"
                    )
                session_id = new_session.id

            # Now send the message to the newly created session
            result = await SessionService.send_session_message(
                session_id=session_id,
                user_id=session_user_id,
                content=content,
                get_fresh_db_session=get_fresh_db_session,
                initiate_streaming=True,
            )

        if result.get("action") == "error":
            raise ValueError(f"Failed to send message: {result.get('message')}")

        # Mark email as processed
        email_msg.processed = True
        email_msg.pending_clone_creation = False
        email_msg.session_id = session_id
        email_msg.updated_at = datetime.now(UTC)
        db_session.add(email_msg)
        db_session.commit()

        logger.info(
            f"Email {email_msg.id}: processed -> session {session_id} "
            f"on agent {target_agent_id} (mode={session_mode})"
        )

    @staticmethod
    async def _process_email_to_task(
        db_session: DBSession,
        email_msg: EmailMessage,
    ) -> None:
        """
        Create an InputTask from an incoming email instead of a session.

        Used when integration.process_as == "new_task". The task is created
        for the agent owner to review, refine, and execute manually.

        The task retains source_agent_id for SMTP config lookup when sending replies.
        """
        from app.models.agent import Agent

        # Get the parent agent and its owner
        agent = db_session.get(Agent, email_msg.agent_id)
        if not agent:
            raise ValueError(f"Agent {email_msg.agent_id} not found")

        # Build task content from email
        content = EmailProcessingService._format_email_as_message(email_msg)

        # Create InputTask
        task = InputTask(
            owner_id=agent.owner_id,
            original_message=content,
            current_description=content,
            selected_agent_id=email_msg.agent_id,  # Pre-select the email agent
            source_email_message_id=email_msg.id,
            source_agent_id=email_msg.agent_id,  # Preserved for SMTP lookup
            status=InputTaskStatus.NEW,
            refinement_history=[],
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        # Mark email as processed and link to task
        email_msg.processed = True
        email_msg.pending_clone_creation = False
        email_msg.input_task_id = task.id
        email_msg.updated_at = datetime.now(UTC)
        db_session.add(email_msg)
        db_session.commit()

        # Emit TASK_CREATED event for activity tracking
        from app.utils import create_task_with_error_logging
        from app.services.event_service import event_service
        from app.models.event import EventType

        create_task_with_error_logging(
            event_service.emit_event(
                event_type=EventType.TASK_CREATED,
                model_id=task.id,
                user_id=agent.owner_id,
                meta={
                    "source_email_message_id": str(email_msg.id),
                    "source_agent_id": str(email_msg.agent_id),
                }
            ),
            task_name=f"emit_task_created_{task.id}"
        )

        logger.info(
            f"Email {email_msg.id}: created task {task.id} for agent {email_msg.agent_id} "
            f"owner {agent.owner_id} (process_as=new_task)"
        )

    @staticmethod
    def _format_email_as_message(email_msg: EmailMessage) -> str:
        """Format an email into a message string for the agent.

        Note: The authoritative sender/subject metadata is in the session context
        (system prompt and GET /session/context endpoint), not in this message text.
        This formatting is for readability only.
        """
        parts = ["--- Forwarded email content ---"]

        if email_msg.subject:
            parts.append(f"Subject: {email_msg.subject}")
        parts.append(f"From: {email_msg.sender}")

        parts.append("")  # blank line separator

        parts.append(email_msg.body or "")

        # Add attachment info if present
        if email_msg.attachments_metadata:
            parts.append("")
            parts.append("Attachments:")
            for att in email_msg.attachments_metadata:
                name = att.get("filename", "unknown")
                size = att.get("size", 0)
                parts.append(f"  - {name} ({size} bytes)")

        parts.append("--- End of forwarded email content ---")

        return "\n".join(parts)

    @staticmethod
    def _handle_processing_error(
        db_session: DBSession,
        email_msg: EmailMessage,
        error: str,
    ) -> None:
        """Record a processing error on an email message."""
        email_msg.processing_error = error
        email_msg.updated_at = datetime.now(UTC)
        db_session.add(email_msg)
        db_session.commit()
        logger.warning(f"Email {email_msg.id}: processing error: {error}")
