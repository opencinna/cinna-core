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
import io
import logging
import re
import uuid
from datetime import UTC, datetime
from email.generator import BytesGenerator
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import unquote

from sqlmodel import Session

from app.models.email.email_message import EmailMessage

logger = logging.getLogger(__name__)

# ``cid:`` references inside an HTML body. Whatever follows the scheme up to
# the first quote, whitespace or closing delimiter is the Content-ID the part
# is embedded under — that is how a signature logo is wired into the markup,
# and it is the only reliable signal that an "attachment" is really inline.
_CID_REFERENCE_RE = re.compile(r"cid:([^\"'\s>)\]]+)", re.IGNORECASE)

# A forwarded email arrives as this: a MIME part whose payload is a whole
# nested message rather than transfer-encoded bytes. It needs its own
# serialisation step — see :meth:`EmailPollingService._serialize_message_part`.
_RFC822_CONTENT_TYPE = "message/rfc822"
# Fallback name for a forwarded message whose part carries no ``filename``,
# which is common. Better than the generic ``unknown``: the extension is what
# tells a reader (and the agent) that the file opens as mail.
_FORWARDED_MESSAGE_FILENAME = "forwarded-message.eml"

# Hard ceiling on how many attachment parts one message may contribute, even as
# metadata. The count cap the pipeline enforces is a *policy* number the caller
# passes in; this is the structural one, and it exists for the same reason
# ``GoogleChatAdapter._parse_attachments`` slices ``attachment[]``: a mail with
# ten thousand parts must not become ten thousand dicts and a megabyte of JSON
# in ``email_message.attachments_metadata``. Far above any real message.
_MAX_PARSED_ATTACHMENT_PARTS = 1000


def _safe_content_type(part: email.message.Message) -> str:
    """``part``'s content type for a log line, never raising.

    Used only from inside an ``except`` handler, where the part is already
    known to be defective — asking it one more question must not replace the
    error being reported with a second one.
    """
    try:
        return part.get_content_type()
    except Exception:  # noqa: BLE001 — a log label is not worth an exception
        return "unknown"


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
    def _parse_email(
        raw_data: bytes,
        *,
        attachment_budget_bytes: int | None = None,
        max_attachments: int | None = None,
    ) -> dict | None:
        """Parse raw email bytes into a structured dict.

        ``attachment_budget_bytes`` is how much attachment **content** this
        parse may keep in memory — the caller's remaining per-poll-tick budget
        (see :meth:`_extract_attachments`). ``None`` means "keep everything",
        which is what a caller with no budget to enforce wants and what this
        method did before attachments carried their bytes at all.

        ``max_attachments`` is how many attachments the caller's pipeline will
        actually accept from one message; past it, parts are still reported but
        their bytes are not retained. ``None`` means "no cap", for a caller
        that has no downstream limit to mirror.
        """
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

        # Extract attachments — metadata AND, unless the caller's budget is
        # spent, the decoded bytes. The bytes live only as long as this dict.
        #
        # **Guarded, and the guard is not decoration.** Everything below the
        # ``message_from_bytes`` above runs unprotected, and this region is the
        # one the attachment feature added: it decodes sender-controlled MIME
        # payloads and sender-controlled headers. A raise here does not cost
        # one message — it propagates through ``poll()`` to
        # ``ChannelPollService.poll_all``'s per-channel handler, which abandons
        # the **whole tick**, and ``_fetch_and_accept`` has already marked
        # every message accepted earlier in that tick ``\Seen``. Those are
        # discarded with the list and never re-fetched, while the offending
        # mail stays unread and fails the next tick identically. The mailbox
        # wedges.
        #
        # The per-part guard inside :meth:`_extract_attachments` is the primary
        # defence and keeps one bad part from costing its siblings; this is the
        # backstop for the walk itself, on the same reasoning
        # :meth:`_html_inline_cids` already states one method down — a body we
        # cannot read hides no attachments either.
        try:
            attachments = EmailPollingService._extract_attachments(
                msg,
                budget_bytes=attachment_budget_bytes,
                max_attachments=max_attachments,
            )
        except Exception:  # noqa: BLE001 — see above; the message still arrives
            logger.warning(
                "Could not extract attachments from a message (Message-ID=%s); "
                "delivering it as if it had none",
                msg.get("Message-ID"),
                exc_info=True,
            )
            attachments = []
        if attachments:
            for att in attachments:
                logger.debug(f"  Attachment: {att['filename']} ({att['content_type']}, {att['size']} bytes)")

        # The durable half. Projected explicitly rather than by deleting one
        # key, so no future field of the in-memory shape — least of all
        # ``content`` — can reach the database by accident. This list is what
        # ``EmailMessage.attachments_metadata`` has always held, unchanged.
        attachments_metadata = [
            {
                "filename": att["filename"],
                "content_type": att["content_type"],
                "size": att["size"],
            }
            for att in attachments
        ]

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
            "attachments_metadata": attachments_metadata or None,
            # In-memory only, never persisted: the same attachments WITH their
            # decoded bytes, for the channel transport to hand the pipeline.
            "attachments": attachments,
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
    def _extract_attachments(
        msg: email.message.Message,
        *,
        budget_bytes: int | None = None,
        max_attachments: int | None = None,
    ) -> list[dict[str, Any]]:
        """Extract email attachments — metadata **and** their decoded bytes.

        Returns one dict per attachment part::

            {"filename": str, "content_type": str, "size": int,
             "content": bytes | None, "unavailable_reason": str}  # reason optional

        ``size`` is the real decoded length, always. ``content`` is those same
        bytes, kept rather than discarded — this is not a new parse pass: the
        predecessor of this method (``_extract_attachment_metadata``) already
        decoded every attachment part purely to take ``len()`` of the result
        and throw it away. Keeping the value it already computed is the whole
        change.

        **Inline parts are excluded**, on two rules:

        1. ``Content-Disposition`` does not say ``attachment`` (unchanged), and
        2. the part's ``Content-ID`` appears as a ``cid:`` reference in the
           message's HTML body — **even when its disposition says
           ``attachment``**. Mailers routinely mark embedded signature images
           that way, and without this rule every logo in the company lands in
           the agent's workspace.

        Both rules live in :meth:`_is_attachment_part`.

        The residual case — a signature image marked ``attachment`` with no
        ``Content-ID`` — is a documented limitation (plan §9). It is
        deliberately not chased with a size heuristic, which would silently
        drop real small files.

        **A forwarded email** (``message/rfc822``) is a *container*, and its
        own parts win. ``walk()`` descends into a nested message, so an
        `invoice.pdf` inside a forward has always arrived loose and still does
        — that is the whole "forward me this invoice" workflow and it is
        unchanged. What the container itself contributes is **nothing**: it is
        not a failed attachment, so it produces no entry, no skip and no
        sender-visible notice. The one exception is the fallback below.

        **The fallback.** When a forwarded message yields no deliverable inner
        part — everything filtered out as inline, everything empty, or a
        genuinely bodiless forward — the container is serialised to ``.eml``
        bytes (:meth:`_serialize_message_part`) and admitted as one attachment
        instead. That is what keeps the forward from being silently lost, and
        it is strictly either/or: the loose parts *or* the ``.eml``, never
        both, so nothing is charged twice against the aggregate cap, the tick
        budget or the sender's quota. ``message/rfc822`` is on
        ``UPLOAD_ALLOWED_MIME_TYPES`` for the fallback's sake — without the
        entry the ``.eml`` would merely fail differently, as
        ``type_not_allowed``.

        **``budget_bytes``** is the caller's remaining per-poll-tick memory
        budget across *all* messages in the tick. An attachment that would push
        past it keeps its metadata but loses its bytes, carrying
        ``unavailable_reason="poll_budget_exhausted"`` so the sender is told
        instead of left wondering — the message itself still arrives. That is a
        real, unrecoverable loss (the mail is marked ``\\Seen`` and is not
        re-fetched next tick), hence the WARNING. The alternative — holding
        everything — turns one mailbox with a backlog of large mail into an
        OOM.

        **``max_attachments``** mirrors the pipeline's per-message count cap
        (``CHANNEL_ATTACHMENT_MAX_PER_MESSAGE``) *here*, where the bytes are.
        Plan §4.3/§9 promise that attachments past the cap are skipped "without
        being fetched"; on Chat that is true because the cap precedes the
        download, but on this path there is no download — the cost is the
        decoded copy, and without this parameter a mail with two hundred 1MB
        parts would spend two hundred megabytes (the whole default tick budget)
        to deliver ten and skip a hundred and ninety, starving every later
        message in the tick. Past the cap a part is still **measured and still
        reported** — the pipeline names it to the sender as
        ``too_many_attachments``, and ``attachments_metadata`` stays an honest
        manifest — but its bytes are released the moment its length is known.
        Peak cost becomes one transient part rather than all of them.

        **Nothing in the per-part walk may raise.** Each part is read behind
        its own guard: a header the stdlib refuses to decode, or a payload it
        refuses to decode, costs that one attachment and not its siblings —
        and, above all, not the poll tick, whose earlier messages are already
        ``\\Seen`` by the time this runs. See the call site in
        :meth:`_parse_email`.
        """
        inline_cids = EmailPollingService._html_inline_cids(msg)
        attachments: list[dict[str, Any]] = []
        # The part each admitted entry came from, positionally parallel to
        # ``attachments``. One reader: the forwarded-message fallback, which
        # has to ask whether a container's own subtree delivered anything. Ids
        # are safe as keys here — every part stays referenced by ``msg`` for as
        # long as this method runs.
        source_ids: list[int] = []
        kept_bytes = 0
        # ``message/rfc822`` containers, in walk order, each paired with the
        # ids of the parts it contains. Their fate is decided *after* the walk,
        # because "did this forward deliver anything loose?" cannot be answered
        # at the moment the container is reached — ``walk()`` has not visited
        # its children yet.
        forwarded: list[tuple[email.message.Message, set[int]]] = []

        def admit(
            entry: dict[str, Any], source_id: int, *, keep_content: bool
        ) -> None:
            """Charge one entry against the caps and append it."""
            nonlocal kept_bytes
            size = entry["size"]
            if not keep_content:
                # Over the count cap: nothing was retained, so nothing is
                # charged. No ``unavailable_reason`` is set — the pipeline
                # slices these off ahead of every other check and names them
                # ``too_many_attachments``, which is the reason that belongs in
                # the sender's notice.
                pass
            elif budget_bytes is not None and kept_bytes + size > budget_bytes:
                entry["content"] = None
                entry["unavailable_reason"] = "poll_budget_exhausted"
                logger.warning(
                    "Per-tick attachment budget exhausted — dropping the "
                    "content of '%s' (%d bytes); the message still arrives, "
                    "but this attachment is lost and will not be re-fetched. "
                    "The check is per attachment, so a smaller one later in "
                    "this message may still fit",
                    entry["filename"],
                    size,
                )
            else:
                kept_bytes += size

            attachments.append(entry)
            source_ids.append(source_id)

        for part in msg.walk():
            if len(attachments) >= _MAX_PARSED_ATTACHMENT_PARTS:
                logger.warning(
                    "Message carries more than %d attachment parts; the rest "
                    "are ignored entirely",
                    _MAX_PARSED_ATTACHMENT_PARTS,
                )
                break

            # Past the pipeline's count cap the part is measured and reported
            # but its bytes are not retained — see the docstring.
            keep_content = (
                max_attachments is None or len(attachments) < max_attachments
            )

            # **The per-part guard.** Everything inside reads sender-controlled
            # MIME. One part the stdlib cannot decode must cost that part, not
            # its siblings and not the poll tick — the same trade
            # :meth:`_html_inline_cids` already makes one method down.
            try:
                if part.get_content_type() == _RFC822_CONTENT_TYPE:
                    # A container, not an attachment. Its children are next in
                    # the walk and will be emitted loose; it contributes an
                    # ``.eml`` only if they turn out to deliver nothing.
                    if EmailPollingService._is_attachment_part(
                        part, inline_cids=inline_cids
                    ):
                        forwarded.append(
                            (
                                part,
                                {
                                    id(sub)
                                    for sub in part.walk()
                                    if sub is not part
                                },
                            )
                        )
                    continue

                entry = EmailPollingService._attachment_entry(
                    part,
                    inline_cids=inline_cids,
                    keep_content=keep_content,
                )
            except Exception:  # noqa: BLE001 — one bad part is not a bad mail
                logger.warning(
                    "Could not read an attachment part (type=%s); skipping it "
                    "and keeping the rest of the message",
                    _safe_content_type(part),
                    exc_info=True,
                )
                continue

            if entry is None:
                continue

            admit(entry, id(part), keep_content=keep_content)

        # **The forwarded-message fallback**, deferred to here for the reason
        # given on ``forwarded`` above. Innermost first (``reversed``, since
        # ``walk()`` is pre-order), so a forward nested inside a forward has
        # already contributed its own ``.eml`` before the outer container asks
        # whether its subtree delivered anything.
        for container, subpart_ids in reversed(forwarded):
            if any(source_id in subpart_ids for source_id in source_ids):
                # Its parts arrived loose. The ``.eml`` would be the same bytes
                # a second time — two copies in the workspace, and both charged
                # to the aggregate cap, the tick budget and the sender's quota.
                continue

            if len(attachments) >= _MAX_PARSED_ATTACHMENT_PARTS:
                break

            keep_content = (
                max_attachments is None or len(attachments) < max_attachments
            )
            try:
                entry = EmailPollingService._forwarded_entry(
                    container, keep_content=keep_content
                )
            except Exception:  # noqa: BLE001 — same trade as the walk above
                logger.warning(
                    "Could not read a forwarded message part; skipping it and "
                    "keeping the rest of the message",
                    exc_info=True,
                )
                continue

            admit(entry, id(container), keep_content=keep_content)

        return attachments

    @staticmethod
    def _is_attachment_part(
        part: email.message.Message, *, inline_cids: set[str]
    ) -> bool:
        """Whether this part is one the sender meant as a *file*.

        Two rules, both long-standing: the ``Content-Disposition`` has to say
        ``attachment``, and a part whose ``Content-ID`` is embedded in the HTML
        body as a ``cid:`` reference is inline whatever its disposition claims.

        Split out from :meth:`_attachment_entry` because the forwarded-message
        container needs the same verdict without the decode that follows it.
        Reads sender-controlled headers, so every caller keeps it inside a
        per-part guard.
        """
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition:
            return False

        content_id = EmailPollingService._normalize_content_id(
            part.get("Content-ID")
        )
        if content_id and content_id in inline_cids:
            logger.debug("    Skipping inline part referenced as cid:%s", content_id)
            return False
        return True

    @staticmethod
    def _attachment_entry(
        part: email.message.Message,
        *,
        inline_cids: set[str],
        keep_content: bool,
    ) -> dict[str, Any] | None:
        """One MIME part as an attachment entry, or ``None`` if it is not one.

        Everything that reads sender-controlled MIME lives in here, and that is
        the point: it gives the caller exactly one expression to wrap in a
        per-part guard. ``Content-Disposition``, ``Content-ID``, the RFC 2047
        filename and the transfer-encoded payload are each a place the stdlib
        can raise on a defective message, and none of them is worth a whole
        mail — let alone a whole poll tick.

        ``message/rfc822`` never reaches this method: a forwarded message is a
        container the caller handles separately (:meth:`_forwarded_entry`).

        ``keep_content`` False means the part is over the caller's count cap:
        it is still decoded far enough to know its real ``size`` — so the
        durable manifest stays honest — and the bytes are then dropped on the
        floor instead of being carried for the rest of the tick.
        """
        if not EmailPollingService._is_attachment_part(
            part, inline_cids=inline_cids
        ):
            return None

        filename = part.get_filename()
        if filename:
            filename = EmailPollingService._decode_header_value(filename)

        # ``decode=True`` answers ``None`` for a part with nothing to decode,
        # and typeshed allows a ``str`` for a malformed one.
        decoded = part.get_payload(decode=True)
        payload = decoded if isinstance(decoded, bytes) else b""

        return {
            "filename": filename or "unknown",
            "content_type": part.get_content_type(),
            "size": len(payload),
            "content": payload if keep_content else None,
        }

    @staticmethod
    def _forwarded_entry(
        part: email.message.Message, *, keep_content: bool
    ) -> dict[str, Any]:
        """A forwarded message as one ``.eml`` entry — the fallback path only.

        Reached only when the forward delivered no loose part of its own, so
        emitting it here can never double what the sender already got.

        An empty serialisation (a container with no inner message, or one the
        generator refuses) is left as ``size=0``: the ref collapses to "no
        bytes" downstream and the sender is told ``no_content``. In *this*
        position that is honest rather than bogus — the forward reached the
        agent by no other route, so it really was lost.
        """
        payload = EmailPollingService._serialize_message_part(part)

        filename = part.get_filename()
        if filename:
            filename = EmailPollingService._decode_header_value(filename)

        return {
            # A forwarded part commonly carries no ``filename``; the extension
            # is what tells a reader (and the agent) that the file opens as
            # mail.
            "filename": filename or _FORWARDED_MESSAGE_FILENAME,
            "content_type": _RFC822_CONTENT_TYPE,
            "size": len(payload),
            "content": payload if keep_content else None,
        }

    @staticmethod
    def _serialize_message_part(part: email.message.Message) -> bytes:
        """Flatten a ``message/rfc822`` part into the bytes of an ``.eml``.

        A nested message part holds a *parsed* sub-``Message``, which is why
        ``get_payload(decode=True)`` answers ``None`` for it: there is no
        transfer encoding to undo. The bytes have to be regenerated, and the
        stdlib generator is the way to do it — reaching for the raw undecoded
        payload instead would hand back a ``Message`` object or a string whose
        encoding nobody has resolved.

        ``maxheaderlen=0`` keeps the forwarded message's headers exactly as
        they arrived; the default would refold long ones, which changes bytes
        the sender may care about (DKIM, above all). ``mangle_from_=False``
        keeps ``>From`` escaping out of a file that is not an mbox.

        Called only from the fallback in :meth:`_forwarded_entry` — a forward
        whose own parts arrived loose is never serialised, because the ``.eml``
        would be those same bytes a second time.

        Returns ``b""`` when there is nothing to serialise or the generator
        refuses a defective message. The caller treats that exactly as it
        treats any other empty part — ``size=0``, reported to the sender as
        ``no_content`` — rather than inventing a reason code for it. On this
        path that report is honest: nothing else carried the forward.
        """
        payload = part.get_payload()
        if isinstance(payload, email.message.Message):
            inner: email.message.Message | None = payload
        elif isinstance(payload, list):
            inner = next(
                (
                    item
                    for item in payload
                    if isinstance(item, email.message.Message)
                ),
                None,
            )
        else:
            inner = None
        if inner is None:
            return b""

        buffer = io.BytesIO()
        try:
            BytesGenerator(buffer, mangle_from_=False, maxheaderlen=0).flatten(inner)
        except Exception:  # noqa: BLE001 — a mail we cannot re-emit is not fatal
            logger.warning(
                "Could not serialize a forwarded message part; it will be "
                "reported to the sender as empty"
            )
            return b""
        return buffer.getvalue()

    @staticmethod
    def _html_inline_cids(msg: email.message.Message) -> set[str]:
        """Content-IDs the message's HTML body embeds as ``cid:`` references.

        Read from every non-attachment ``text/html`` part, because a mailer may
        split alternatives across several. Failure to decode one part is not
        fatal: a missed ``cid:`` costs a stray logo in the workspace, while an
        exception here would cost the whole mail.
        """
        cids: set[str] = set()
        for part in msg.walk():
            if part.get_content_type() != "text/html":
                continue
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            try:
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes) or not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
            except Exception:  # noqa: BLE001 — a body we cannot read hides no cids
                continue
            for match in _CID_REFERENCE_RE.findall(html):
                normalized = EmailPollingService._reference_content_id(match)
                if normalized:
                    cids.add(normalized)
        return cids

    @staticmethod
    def _normalize_content_id(value: str | None) -> str:
        """``<Logo.png@01D9>`` → ``Logo.png@01D9``. Header side only.

        Case is **preserved**: a Content-ID is case-sensitive (RFC 2392), and
        folding it would let two distinct ids collide — which on this path
        means dropping a real attachment, the one outcome plan §9 says not to
        risk. Percent-decoding is likewise not applied here: headers are not
        percent-encoded, so ``%41`` in a real Content-ID must stay ``%41``.
        Both belong to the *reference* side, and live in
        :meth:`_reference_content_id`.
        """
        if not value:
            return ""
        return str(value).strip().strip("<>").strip()

    @staticmethod
    def _reference_content_id(value: str) -> str:
        """``cid:Logo.png%4001D9`` → the Content-ID it names.

        A ``cid:`` URL *is* a URL, so this is where percent-decoding belongs
        (RFC 2392 defines the reference as an encoded ``msg-id``). The result
        is compared against :meth:`_normalize_content_id` of the part's header.
        """
        return EmailPollingService._normalize_content_id(unquote(value.strip()))

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
