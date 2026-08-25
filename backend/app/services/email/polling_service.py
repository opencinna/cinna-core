"""
Email polling mechanics — transport-agnostic IMAP/MIME helpers.

Connects to configured IMAP servers, fetches unread mail, parses MIME into a
plain dict, stores it in ``email_message`` and marks it read on the server.

These are *mechanics only*. The per-agent driver that used to sit on top of
them (``poll_agent_mailbox`` / ``poll_all_enabled_agents``, keyed on the
deleted ``AgentEmailIntegration``) is gone; the email **channel transport**
under ``app/services/server_channels/adapters/`` becomes their only caller and
will absorb them. Until then this class is a bag of statics with no driver —
deliberately, for one commit.
"""
import email
import imaplib
import logging
import uuid
from datetime import UTC, datetime
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from sqlmodel import Session

from app.models.email.email_message import EmailMessage

logger = logging.getLogger(__name__)


class EmailPollingService:

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
        display_name, sender_email = parseaddr(from_header)
        if not sender_email:
            logger.warning(f"Email has no valid sender address (From: '{from_header}'), skipping")
            return None
        # RFC 2047 applies to the display part too ("=?utf-8?B?...?= <a@b>").
        display_name = EmailPollingService._decode_header_value(display_name).strip()
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
            # The From: display part, for ChannelInboundMessage.
            # sender_display_name. Non-authoritative in every sense — it is
            # the same spoofable header the address comes from.
            "sender_display_name": display_name or None,
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
    def _is_addressed_to_channel(
        recipients: list[str],
        incoming_mailbox: str,
    ) -> bool:
        """
        Check if the channel's incoming_mailbox is among the recipients (To/CC).

        Mandatory, and its reason is unchanged by the move from a per-agent
        integration to a channel: one IMAP account can carry mail addressed to
        groups, aliases, or other mailboxes entirely. Only the target moved —
        it is now ``ServerChannel.config["incoming_mailbox"]``, so several
        channels may share one account and each answers only its own mail.

        An empty target rejects everything, deliberately: with nothing to
        compare against there is no way to tell this channel's mail from
        anybody else's, and answering all of it is the worse failure.
        """
        if not incoming_mailbox:
            # No mailbox configured - cannot verify, reject by default
            return False
        target = incoming_mailbox.strip().lower()
        return target in recipients

    @staticmethod
    def format_email_as_message(email_msg: EmailMessage) -> str:
        """Format a stored email into the message text handed to an agent.

        Moved here verbatim from the deleted ``EmailProcessingService``: it is
        pure formatting over an ``EmailMessage`` row with no dependency on the
        removed per-agent integration, and the email channel transport needs
        exactly this to build ``ChannelInboundMessage.text``.

        Note: this is **not** merely a readability convenience any more. The
        session-context enrichment
        (``message_service._build_session_context``'s ``email_subject`` field,
        surfaced via the system prompt and ``GET /session/context``) only
        fires when a session's ``integration_type`` is literally ``"email"``.
        Every channel-routed email session is stamped ``"channel_email"``
        instead, so that enrichment never runs for mail arriving through this
        transport — the subject reaches the agent **only** through this
        formatted message text (the ``Subject:`` line above), not through the
        server-verified session context. See
        ``docs/application/email_integration/email_integration_tech.md`` for
        the full account of this gap.
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
