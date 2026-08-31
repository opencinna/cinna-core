"""
Email Sending Service — the durable drain of ``outgoing_email_queue``.

This is the send half only. Enqueueing used to happen here, keyed on the
per-agent ``AgentEmailIntegration``; that integration is deleted and
``ChannelOutboundService`` becomes the producer, so **nothing enqueues for one
commit** and this drain is a no-op until the email channel transport lands.

SMTP configuration is resolved per queue entry through the channel the
conversation arrived on: ``entry.session_id`` → ``ChannelThreadBinding`` →
``ServerChannel.config["outgoing_server_id"] / ["from_address"]``. A queue row
whose session has no binding, or whose channel is not configured for outbound
mail, is recorded as a permanent failure rather than retried or crashed on.
"""
import logging
import uuid
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.email.outgoing_email_queue import OutgoingEmailQueue, OutgoingEmailStatus
from app.models.server_channels.channel_thread_binding import ChannelThreadBinding
from app.models.server_channels.server_channel import ServerChannel
from app.models.sessions.session import Session as ChatSession
from app.models.users.user import User
from app.services.email.mail_server_service import MailServerService
from app.services.email.smtp_connector import smtp_connector

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class EmailSendingService:

    @staticmethod
    def _resolve_responsible_user(
        db_session: Session, entry: OutgoingEmailQueue
    ) -> User | None:
        """Resolve the platform user responsible for an outgoing queue entry.

        This is the account whose confirmation status gates the send — the
        human the platform holds responsible for the mail, NOT the external
        recipient.

        Resolution is ``entry.agent_id`` → ``Agent.owner_id`` → ``User``: with
        the per-agent email integration gone, every outgoing entry belongs to
        exactly one agent, and that agent has exactly one owner.

        ``input_task_id`` is consulted first purely for legacy rows. The
        column still exists on the queue and older rows may carry a task id;
        nothing enqueues with one any more. Its FK is ``ON DELETE SET NULL``,
        so a task that has since been deleted leaves the id NULL and the agent
        owner answers — and a still-present task whose owner is gone falls
        through to the agent owner too.
        """
        if entry.input_task_id is not None:
            from app.models.tasks.input_task import InputTask

            task = db_session.get(InputTask, entry.input_task_id)
            if task and task.owner_id:
                return db_session.get(User, task.owner_id)
            # Fall through to agent owner if the task is gone (SET NULL FK).
        agent = db_session.get(Agent, entry.agent_id)
        if agent and agent.owner_id:
            return db_session.get(User, agent.owner_id)
        return None

    @staticmethod
    def _resolve_channel(
        db_session: Session, entry: OutgoingEmailQueue
    ) -> ServerChannel | None:
        """Resolve the ``ServerChannel`` an outgoing queue entry replies into.

        The path is ``entry.session_id`` → ``ChannelThreadBinding`` →
        ``ServerChannel``: the binding is the (channel, thread) → session map,
        so the session the reply belongs to names its own channel. Returns
        ``None`` when the entry has no session, no binding (deleted thread), or
        a channel that has since been removed — all of which the caller records
        as a permanent failure on the row.
        """
        if entry.session_id is None:
            return None
        binding = db_session.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.session_id == entry.session_id
            )
        ).first()
        if binding is None:
            return None
        return db_session.get(ServerChannel, binding.server_channel_id)

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
        not_sent_count = 0
        for entry in pending:
            try:
                # ``_send_single_email`` returns normally after recording a
                # terminal failure on the row, so its return value — not the
                # absence of an exception — is what says an email left the
                # building. Counting calls instead would report a full batch
                # of permanent failures as a batch of successful sends.
                if EmailSendingService._send_single_email(db_session, entry):
                    sent_count += 1
                else:
                    not_sent_count += 1
            except Exception as e:
                not_sent_count += 1
                logger.error(
                    f"Failed to send email {entry.id}: {e}", exc_info=True
                )
                # Error already recorded in _send_single_email
                continue

        if not_sent_count:
            logger.warning(
                f"Outgoing email batch: {sent_count} sent, "
                f"{not_sent_count} not sent (blocked, failed, or retrying)"
            )

        return sent_count

    @staticmethod
    def _send_single_email(
        db_session: Session,
        entry: OutgoingEmailQueue,
    ) -> bool:
        """Send a single email from the queue.

        Returns ``True`` only when the message was actually handed to SMTP.
        Every early return below has already recorded its outcome on the row
        (blocked, permanently failed, or awaiting a retry) and reports
        ``False`` so the caller does not count it as a send.
        """
        # Outbound-email gate (defense-in-depth): block the send if the
        # responsible platform user is not email-confirmed. Marks the entry
        # BLOCKED_UNCONFIRMED (terminal — never retried) so it cannot spam.
        from app.services.users.email_confirmation_service import (
            EmailConfirmationService,
        )
        responsible_user = EmailSendingService._resolve_responsible_user(
            db_session, entry
        )
        if not EmailConfirmationService.is_outbound_email_allowed(responsible_user):
            entry.status = OutgoingEmailStatus.BLOCKED_UNCONFIRMED
            entry.last_error = "sender email not confirmed"
            entry.updated_at = datetime.now(UTC)
            db_session.add(entry)
            db_session.commit()
            logger.warning(
                f"Email {entry.id}: blocked — responsible user email not confirmed"
            )
            return False

        # Resolve the SMTP configuration through the channel this conversation
        # arrived on. Every failure below is terminal and recorded on the row:
        # a misconfigured channel does not get better by retrying, and a queue
        # entry that cannot name a sender must never be silently dropped.
        channel = EmailSendingService._resolve_channel(db_session, entry)
        if channel is None:
            EmailSendingService._mark_failed(
                db_session, entry, "No channel bound to this session"
            )
            return False

        outgoing_server_id = channel.config.get("outgoing_server_id")
        from_address = channel.config.get("from_address")
        if not outgoing_server_id or not from_address:
            EmailSendingService._mark_failed(
                db_session,
                entry,
                f"Channel '{channel.name}' has no outgoing mail configuration",
            )
            return False

        try:
            server_uuid = uuid.UUID(str(outgoing_server_id))
        except (TypeError, ValueError):
            EmailSendingService._mark_failed(
                db_session,
                entry,
                f"Channel '{channel.name}' has a malformed outgoing_server_id",
            )
            return False

        # Get SMTP credentials
        result = MailServerService.get_mail_server_with_credentials(
            db_session, server_uuid
        )
        if not result:
            EmailSendingService._mark_failed(
                db_session, entry, "SMTP server not found"
            )
            return False

        server, password = result

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
            return False

        # Mark as sent
        entry.status = OutgoingEmailStatus.SENT
        entry.sent_at = datetime.now(UTC)
        entry.updated_at = datetime.now(UTC)
        db_session.add(entry)
        db_session.commit()

        logger.info(f"Email {entry.id}: sent to {entry.recipient}")
        return True

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

