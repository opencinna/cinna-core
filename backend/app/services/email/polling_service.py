"""
Email Polling Service - Polls IMAP mailboxes for enabled agents and stores incoming emails.

Connects to configured IMAP servers, fetches unread emails, parses them,
and stores them in the email_message table for later processing.
"""
import email
import imaplib
import logging
import ssl
import uuid
from datetime import UTC, datetime
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from sqlmodel import Session, select

from app.models.email.agent_email_integration import AgentEmailIntegration
from app.models.email.email_message import EmailMessage
from app.models.email.mail_server_config import MailServerConfig, EncryptionType
from app.services.email.imap_connector import imap_connector
from app.services.email.mail_server_service import MailServerService

logger = logging.getLogger(__name__)


class EmailPollingService:

    @staticmethod
    def poll_agent_mailbox(
        session: Session,
        agent_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """
        Poll a single agent's IMAP mailbox for new emails.

        Returns list of stored EmailMessage IDs for processing.
        """
        # Get integration config
        stmt = select(AgentEmailIntegration).where(
            AgentEmailIntegration.agent_id == agent_id,
            AgentEmailIntegration.enabled == True,  # noqa: E712
        )
        integration = session.exec(stmt).first()
        if not integration:
            return []

        if not integration.incoming_server_id:
            logger.warning(f"Agent {agent_id}: no incoming server configured")
            return []

        # Get IMAP server with decrypted credentials
        result = MailServerService.get_mail_server_with_credentials(
            session, integration.incoming_server_id
        )
        if not result:
            logger.warning(f"Agent {agent_id}: incoming server {integration.incoming_server_id} not found")
            return []

        server, password = result

        logger.debug(f"Agent {agent_id}: connecting to IMAP server {server.host}:{server.port}")
        try:
            conn = imap_connector.connect(server, password)
            logger.debug(f"Agent {agent_id}: IMAP connection established")
        except Exception as e:
            logger.error(f"Agent {agent_id}: IMAP connection failed: {e}")
            return []

        try:
            logger.debug(f"Agent {agent_id}: fetching unread emails")
            raw_emails = EmailPollingService._fetch_unread_emails(conn)
            if not raw_emails:
                logger.debug(f"Agent {agent_id}: no unread emails found")
                return []

            logger.debug(f"Agent {agent_id}: found {len(raw_emails)} unread email(s)")
            stored_ids: list[uuid.UUID] = []
            for idx, (msg_id, raw_data) in enumerate(raw_emails, 1):
                try:
                    logger.debug(
                        f"Agent {agent_id}: parsing email {idx}/{len(raw_emails)} "
                        f"(IMAP id={msg_id}, size={len(raw_data)} bytes)"
                    )
                    parsed = EmailPollingService._parse_email(raw_data)
                    if not parsed:
                        logger.debug(f"Agent {agent_id}: email {msg_id} could not be parsed, skipping")
                        continue

                    logger.debug(
                        f"Agent {agent_id}: parsed email - "
                        f"from={parsed['sender']}, subject='{parsed['subject'][:80]}', "
                        f"message_id={parsed['message_id']}, "
                        f"body_len={len(parsed['body'])}, "
                        f"attachments={len(parsed['attachments_metadata']) if parsed['attachments_metadata'] else 0}"
                    )

                    # Check if this email is addressed to the agent's incoming mailbox
                    if not EmailPollingService._is_addressed_to_agent(
                        parsed.get("recipients", []),
                        integration.incoming_mailbox or "",
                    ):
                        logger.debug(
                            f"Agent {agent_id}: email from {parsed['sender']} not addressed to "
                            f"{integration.incoming_mailbox}, skipping "
                            f"(recipients: {parsed.get('recipients', [])})"
                        )
                        # Don't mark as read - it may belong to another consumer of this mailbox
                        continue

                    # Check for duplicate (by email Message-ID header)
                    if parsed["message_id"]:
                        existing = session.exec(
                            select(EmailMessage).where(
                                EmailMessage.agent_id == agent_id,
                                EmailMessage.email_message_id == parsed["message_id"],
                            )
                        ).first()
                        if existing:
                            logger.debug(
                                f"Agent {agent_id}: skipping duplicate email {parsed['message_id']}"
                            )
                            # Still mark as read on server
                            EmailPollingService._mark_email_read(conn, msg_id)
                            continue

                    email_msg = EmailPollingService._store_email_message(session, agent_id, parsed)
                    EmailPollingService._mark_email_read(conn, msg_id)
                    stored_ids.append(email_msg.id)
                    logger.debug(
                        f"Agent {agent_id}: stored email {email_msg.id} from {parsed['sender']}"
                    )
                except Exception as e:
                    logger.error(
                        f"Agent {agent_id}: failed to process email {msg_id}: {e}",
                        exc_info=True,
                    )
                    continue

            logger.info(f"Agent {agent_id}: polled {len(raw_emails)} emails, stored {len(stored_ids)} new")
            return stored_ids

        finally:
            try:
                conn.close()
                conn.logout()
                logger.debug(f"Agent {agent_id}: IMAP connection closed")
            except Exception:
                pass

    @staticmethod
    def _fetch_unread_emails(
        conn: imaplib.IMAP4,
        mailbox: str = "INBOX",
    ) -> list[tuple[bytes, bytes]]:
        """Fetch unread emails from IMAP. Returns list of (msg_id, raw_data)."""
        conn.select(mailbox, readonly=False)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            return []

        msg_ids = data[0].split()
        results = []
        for msg_id in msg_ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status == "OK" and msg_data[0]:
                raw = msg_data[0][1]
                results.append((msg_id, raw))

        return results

    @staticmethod
    def _parse_email(raw_data: bytes) -> dict | None:
        """Parse raw email bytes into a structured dict."""
        try:
            msg = email.message_from_bytes(raw_data)
        except Exception as e:
            logger.error(f"Failed to parse raw email bytes: {e}")
            return None

        # Log raw headers for debugging
        logger.debug(f"  Headers: From={msg.get('From')}, To={msg.get('To')}, "
                      f"Subject={msg.get('Subject')}, Date={msg.get('Date')}")
        logger.debug(f"  Content-Type={msg.get_content_type()}, "
                      f"multipart={msg.is_multipart()}")

        # Decode subject
        raw_subject = msg.get("Subject", "")
        subject = EmailPollingService._decode_header_value(raw_subject)
        logger.debug(f"  Decoded subject: '{subject[:100]}'")

        # Parse sender
        from_header = msg.get("From", "")
        _, sender_email = parseaddr(from_header)
        if not sender_email:
            logger.warning(f"Email has no valid sender address (From: '{from_header}'), skipping")
            return None
        logger.debug(f"  Sender: {sender_email}")

        # Parse recipients (To, CC) for address matching
        to_header = msg.get("To", "")
        cc_header = msg.get("Cc", "")
        recipients = []
        for addr_header in [to_header, cc_header]:
            if addr_header:
                for _, addr in getaddresses([addr_header]):
                    if addr:
                        recipients.append(addr.strip().lower())
        logger.debug(f"  Recipients: {recipients}")

        # Parse date
        date_str = msg.get("Date")
        try:
            received_at = parsedate_to_datetime(date_str) if date_str else datetime.now(UTC)
        except Exception as e:
            logger.debug(f"  Failed to parse date '{date_str}': {e}, using utcnow")
            received_at = datetime.now(UTC)

        # Extract body
        logger.debug(f"  Extracting body...")
        body = EmailPollingService._extract_body(msg)
        logger.debug(f"  Body extracted: {len(body)} chars, "
                      f"preview='{body[:150].replace(chr(10), ' ')}...'")

        # Extract threading headers
        message_id = msg.get("Message-ID", "").strip()
        references = msg.get("References", "")
        in_reply_to = msg.get("In-Reply-To", "").strip()
        logger.debug(f"  Threading: Message-ID={message_id}, "
                      f"In-Reply-To={in_reply_to or 'none'}, "
                      f"References={'yes' if references else 'none'}")

        # Extract attachment metadata
        attachments = EmailPollingService._extract_attachment_metadata(msg)
        if attachments:
            for att in attachments:
                logger.debug(f"  Attachment: {att['filename']} ({att['content_type']}, {att['size']} bytes)")

        return {
            "message_id": message_id,
            "sender": sender_email.lower(),
            "recipients": recipients,
            "subject": subject[:1000] if subject else "",
            "body": body,
            "references": references if references else None,
            "in_reply_to": in_reply_to if in_reply_to else None,
            "received_at": received_at,
            "attachments_metadata": attachments if attachments else None,
        }

    @staticmethod
    def _extract_body(msg: email.message.Message) -> str:
        """Extract the text body from an email message."""
        if msg.is_multipart():
            # Prefer plain text, fall back to html
            text_part = None
            html_part = None
            part_count = 0
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))
                part_count += 1
                logger.debug(f"    MIME part {part_count}: type={content_type}, disposition={disposition}")
                if "attachment" in disposition:
                    continue
                if content_type == "text/plain" and text_part is None:
                    text_part = part
                elif content_type == "text/html" and html_part is None:
                    html_part = part

            chosen = text_part or html_part
            if chosen:
                chosen_type = chosen.get_content_type()
                charset = chosen.get_content_charset() or "utf-8"
                logger.debug(f"    Chose {chosen_type} part (charset={charset})")
                payload = chosen.get_payload(decode=True)
                if payload:
                    try:
                        return payload.decode(charset, errors="replace")
                    except Exception:
                        return payload.decode("utf-8", errors="replace")
            else:
                logger.debug(f"    No text/plain or text/html part found in {part_count} MIME parts")
        else:
            content_type = msg.get_content_type()
            charset = msg.get_content_charset() or "utf-8"
            logger.debug(f"    Single-part email: type={content_type}, charset={charset}")
            payload = msg.get_payload(decode=True)
            if payload:
                try:
                    return payload.decode(charset, errors="replace")
                except Exception:
                    return payload.decode("utf-8", errors="replace")
            else:
                logger.debug("    Payload is empty")

        return ""

    @staticmethod
    def _extract_attachment_metadata(
        msg: email.message.Message,
    ) -> list[dict] | None:
        """Extract metadata for email attachments (does not store content)."""
        attachments = []
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" not in disposition:
                continue

            filename = part.get_filename()
            if filename:
                filename = EmailPollingService._decode_header_value(filename)

            content_type = part.get_content_type()
            size = len(part.get_payload(decode=True) or b"")

            attachments.append({
                "filename": filename or "unknown",
                "content_type": content_type,
                "size": size,
            })

        return attachments if attachments else None

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """Decode an email header value (handles RFC 2047 encoding)."""
        if not value:
            return ""
        decoded_parts = decode_header(value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return "".join(result)

    @staticmethod
    def _store_email_message(
        session: Session,
        agent_id: uuid.UUID,
        parsed: dict,
    ) -> EmailMessage:
        """Store a parsed email in the database."""
        email_msg = EmailMessage(
            agent_id=agent_id,
            email_message_id=parsed["message_id"],
            sender=parsed["sender"],
            subject=parsed["subject"],
            body=parsed["body"],
            references=parsed["references"],
            in_reply_to=parsed["in_reply_to"],
            received_at=parsed["received_at"],
            attachments_metadata=parsed["attachments_metadata"],
        )
        session.add(email_msg)
        session.commit()
        session.refresh(email_msg)
        return email_msg

    @staticmethod
    def _mark_email_read(conn: imaplib.IMAP4, msg_id: bytes) -> None:
        """Mark an email as read (Seen) on the IMAP server."""
        try:
            conn.store(msg_id, "+FLAGS", "\\Seen")
        except Exception as e:
            logger.warning(f"Failed to mark email {msg_id} as read: {e}")

    @staticmethod
    def _is_addressed_to_agent(
        recipients: list[str],
        incoming_mailbox: str,
    ) -> bool:
        """
        Check if the agent's incoming_mailbox is among the email recipients (To/CC).

        This is a mandatory check to ensure emails are only processed by the agent
        they were actually sent to. An IMAP mailbox can contain emails addressed to
        groups, aliases, or other addresses that don't belong to this agent.
        """
        if not incoming_mailbox:
            # No mailbox configured - cannot verify, reject by default
            return False
        target = incoming_mailbox.strip().lower()
        return target in recipients

    @staticmethod
    def poll_all_enabled_agents(session: Session) -> list[uuid.UUID]:
        """
        Poll mailboxes for all agents with enabled email integration.

        Returns list of all stored EmailMessage IDs across all agents.
        """
        stmt = select(AgentEmailIntegration).where(
            AgentEmailIntegration.enabled == True,  # noqa: E712
        )
        integrations = session.exec(stmt).all()

        if not integrations:
            logger.debug("No enabled email integrations to poll")
            return []

        logger.info(f"Polling {len(integrations)} enabled email integrations")

        all_stored_ids: list[uuid.UUID] = []
        for integration in integrations:
            try:
                stored_ids = EmailPollingService.poll_agent_mailbox(
                    session, integration.agent_id
                )
                all_stored_ids.extend(stored_ids)
            except Exception as e:
                logger.error(
                    f"Failed to poll agent {integration.agent_id}: {e}",
                    exc_info=True,
                )
                continue

        return all_stored_ids
