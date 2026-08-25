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
    "build_raw_email",
    "create_email_channel",
    "poll_channel",
]
