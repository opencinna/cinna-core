"""Email-channel test helpers — the polled transport's IMAP-shaped setup.

Extends ``tests/utils/server_channel.py``'s generic admin-CRUD helper
(``create_server_channel``) with what the email transport specifically needs:

* ``build_raw_email`` — RFC 5322 bytes for ``tests/stubs/email_stubs.py
  ::StubIMAPConnector``, which the email transport's ``poll()`` parses through
  the retained ``EmailPollingService`` MIME mechanics (nothing here reimplements
  IMAP or MIME).
* ``create_email_channel`` — composes ``create_server_channel`` with the
  email-shaped ``config`` (plan §2.1: ``incoming_server_id``,
  ``outgoing_server_id``, ``incoming_mailbox``, ``from_address``).
* ``poll_channel`` — a documented Rule-1 exemption, exactly like
  ``flush_pending_bindings`` in ``server_channel.py``: the poll scheduler
  (``channel_poll_scheduler.py``) has no HTTP surface and is ``TESTING``-gated
  per project convention, so tests call ``ChannelPollService
  .poll_enabled_channels`` directly instead of racing a background thread.
"""
from __future__ import annotations

import asyncio
import email.message
from email import encoders
from email.mime.base import MIMEBase
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.utils.server_channel import create_server_channel

#: Patch target for the IMAP connector the email transport imports at module
#: scope (``from app.services.email.imap_connector import imap_connector`` in
#: ``adapters/email.py``) — patch the bound name at the *importing* module,
#: not the definition site, exactly like ``_SEND_TARGET`` / ``_STREAM_TARGET``
#: in the sibling webhook/outbound test files.
IMAP_CONNECTOR_TARGET = "app.services.server_channels.adapters.email.imap_connector"

#: Patch target for the SMTP connector ``EmailChannelAdapter.send_rejection_
#: notice`` calls directly (``from app.services.email.smtp_connector import
#: smtp_connector`` in ``adapters/email.py``) — same "patch the bound name at
#: the importing module" rule as ``IMAP_CONNECTOR_TARGET`` above. Not used by
#: the ordinary agent-reply path, which goes through ``OutgoingEmailQueue``
#: and is read back as a row rather than a send.
SMTP_CONNECTOR_TARGET = "app.services.server_channels.adapters.email.smtp_connector"


def build_raw_email(
    *,
    message_id: str,
    sender: str,
    to: str,
    subject: str = "Test subject",
    body: str = "Hello there",
    in_reply_to: str | None = None,
    references: str | None = None,
    sender_display_name: str | None = None,
) -> bytes:
    """Build RFC 5322 bytes for a ``StubIMAPConnector`` fixture list.

    ``message_id`` / ``in_reply_to`` / ``references`` are written verbatim —
    pass them already angle-bracketed (``"<id@host>"``), the way a real
    server emits them. ``EmailChannelAdapter._normalize_message_id`` tolerates
    either spelling on the way in, but a test fixture should still look like
    real mail rather than depend on that tolerance.
    """
    msg = MIMEText(body, "plain", "utf-8")
    from_value = f'"{sender_display_name}" <{sender}>' if sender_display_name else sender
    msg["From"] = from_value
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return msg.as_bytes()


def build_raw_email_with_attachments(
    *,
    message_id: str,
    sender: str,
    to: str,
    subject: str = "Test subject",
    body: str = "Hello there",
    html_body: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    """Build a multipart RFC 5322 message carrying attachment part(s).

    ``build_raw_email`` above stays a plain ``text/plain`` builder for every
    test that has nothing to do with attachments; this is the multipart
    sibling, added for the channel-message-attachments feature.

    ``attachments`` is a list of dicts, each::

        {"filename": str, "content": bytes,
         "mime_type": str = "application/octet-stream",
         "content_id": str | None,   # sets Content-ID; pair with an
                                      # ``html_body`` "cid:" reference to
                                      # build the inline-exclusion case
         "disposition": str = "attachment"}

    Real mailers mark an inline signature image ``Content-Disposition:
    attachment`` too (only the ``cid:`` reference in the HTML body tells
    ``EmailPollingService._extract_attachments`` it is inline) — the default
    disposition here is ``"attachment"`` for exactly that reason; passing
    ``disposition="inline"`` is not needed to build that case.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    if html_body is not None:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    for att in attachments or []:
        mime_type = att.get("mime_type") or "application/octet-stream"
        maintype, _, subtype = mime_type.partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(att["content"])
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            att.get("disposition", "attachment"),
            filename=att["filename"],
        )
        if att.get("content_id"):
            part.add_header("Content-ID", f"<{att['content_id']}>")
        msg.attach(part)

    return msg.as_bytes()


def build_forwarded_message(
    *,
    subject: str = "Original message",
    sender: str = "original@sender.example",
    body: str = "original body text",
    attachments: list[dict[str, Any]] | None = None,
    nested_forward: email.message.Message | None = None,
) -> email.message.Message:
    """Build the CONTENTS of one forwarded message — the payload that becomes
    a ``message/rfc822`` part when wrapped in :func:`build_raw_email_with_forward`
    or nested inside another call to this same function.

    Added for the channel-message-attachments plan §5.6 "loose parts win"
    behaviour: a forward's own inner parts are what a real forward carries,
    and this builds exactly that inner message, independent of the outer
    envelope that carries it.

    ``attachments`` gives the forward a loose, real attachment (the
    "invoice.pdf" case — delivered directly, never charged to the container).
    ``nested_forward``, when given, embeds ANOTHER call to this function as a
    ``message/rfc822`` part of THIS one — a forward of a forward — for the
    "nested forward" case, where the innermost ``.eml`` fallback is what the
    whole chain ultimately delivers.

    Plain ``text/plain`` (no ``Content-Disposition`` on anything) when
    neither is given — the "text-only forward, nothing deliverable inside"
    case that falls back to the ``.eml`` itself.
    """
    if attachments or nested_forward is not None:
        msg: email.message.Message = MIMEMultipart("mixed")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for att in attachments or []:
            mime_type = att.get("mime_type") or "application/octet-stream"
            maintype, _, subtype = mime_type.partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(att["content"])
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=att["filename"]
            )
            msg.attach(part)
        if nested_forward is not None:
            nested_part = MIMEMessage(nested_forward)
            # ``EmailPollingService._extract_attachments`` only ever considers
            # a ``message/rfc822`` part a forward candidate when
            # ``_is_attachment_part`` says so — which requires
            # ``Content-Disposition: attachment`` on the PART ITSELF (the
            # envelope carrying the nested message, not the nested message's
            # own headers). Real mail clients set exactly this when forwarding
            # as an attachment; without it the container is walked (its own
            # parts still arrive loose) but is never a *fallback* candidate.
            nested_part.add_header(
                "Content-Disposition", "attachment", filename="forwarded-message.eml"
            )
            msg.attach(nested_part)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = formatdate(localtime=True)
    return msg


def build_raw_email_with_forward(
    *,
    message_id: str,
    sender: str,
    to: str,
    subject: str = "Fwd: something",
    body: str = "See forwarded message below",
    forwarded: email.message.Message,
) -> bytes:
    """An outer email whose only meaningful content is a forwarded
    ``message/rfc822`` part, built via :func:`build_forwarded_message`.

    The shape ``EmailPollingService._extract_attachments`` treats specially
    (plan §5.6): the forward's own loose parts win over the container, and
    only a forward with nothing deliverable inside falls back to serialising
    the container itself as one ``.eml`` attachment.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    # Same fix as the nested-forward attach in `build_forwarded_message`:
    # ``EmailPollingService._extract_attachments`` only treats a
    # ``message/rfc822`` part as a forward candidate when
    # ``_is_attachment_part`` says so, which requires ``Content-Disposition:
    # attachment`` on the envelope part itself — real mail clients set this
    # when forwarding as an attachment.
    forward_part = MIMEMessage(forwarded)
    forward_part.add_header(
        "Content-Disposition", "attachment", filename="forwarded-message.eml"
    )
    msg.attach(forward_part)
    return msg.as_bytes()


def create_email_channel(
    client: TestClient,
    superuser_headers: dict[str, str],
    *,
    incoming_server_id: str,
    outgoing_server_id: str,
    incoming_mailbox: str,
    from_address: str | None = None,
    email_whitelist: str | None = "*",
    auto_register_users: bool = False,
    enabled: bool = True,
    name: str | None = None,
    expected_status: int = 200,
) -> dict:
    """POST /admin/server-channels for ``channel_type="email"``.

    Thin composition over ``create_server_channel`` — the same helper every
    other channel type in this domain shares — with the email-shaped
    ``config`` (plan §2.1) built for the caller.
    """
    config: dict[str, Any] = {
        "incoming_server_id": incoming_server_id,
        "outgoing_server_id": outgoing_server_id,
        "incoming_mailbox": incoming_mailbox,
        "from_address": from_address or incoming_mailbox,
    }
    return create_server_channel(
        client,
        superuser_headers,
        channel_type="email",
        name=name,
        enabled=enabled,
        auto_register_users=auto_register_users,
        email_whitelist=email_whitelist,
        config=config,
        expected_status=expected_status,
    )


def poll_channel(db: Session) -> int:
    """Directly invoke ``ChannelPollService.poll_enabled_channels``.

    EXEMPTION — same shape as ``tests/utils/server_channel.py
    ::flush_pending_bindings``: ``channel_poll_scheduler`` is the production
    caller and it is ``TESTING``-gated (a poller running under test would open
    real IMAP connections from an arbitrary worker thread and push messages
    into the pipeline underneath unrelated suites), so tests call the service
    entry point directly. Any ``imap_connector`` stub (patch
    ``IMAP_CONNECTOR_TARGET``) or ``agent_env_connector`` stub the caller
    needs active for a resulting ingest must be patched *around* this call,
    exactly as for ``flush_pending_bindings``.
    """
    from app.services.server_channels.channel_poll_service import ChannelPollService

    return asyncio.run(ChannelPollService.poll_enabled_channels(db))


__all__ = [
    "IMAP_CONNECTOR_TARGET",
    "SMTP_CONNECTOR_TARGET",
    "build_forwarded_message",
    "build_raw_email",
    "build_raw_email_with_attachments",
    "build_raw_email_with_forward",
    "create_email_channel",
    "poll_channel",
]
