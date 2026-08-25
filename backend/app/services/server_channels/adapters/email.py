"""Email channel transport — the first *polled* channel.

Trust chain, stated as plainly as Google Chat's is, because the two now feed
the same pipeline and they are **not the same tier**:

    Google Chat  sender_email comes out of a Google-signed JWT.
    Email        sender_email comes out of the ``From:`` header, and the
                 ``From:`` header is **spoofable**.

Anyone who can get a message into the polled mailbox can claim any address in
it. Nothing below ``poll`` re-checks the sender — the whitelist, user
resolution, auto-registration and identity routing all treat that address as
the sender's identity — so the whitelist on an email channel is only as strong
as the mail server's own anti-spoofing (SPF/DKIM/DMARC enforcement at
delivery time). That sentence belongs in the admin UI, and
:meth:`EmailChannelAdapter.get_setup_instructions` puts it there.

**Inbound** reuses the retained IMAP/MIME mechanics on
``EmailPollingService`` — this module is a driver over them, not a second IMAP
implementation.

**Outbound** does not send. It enqueues into ``outgoing_email_queue``, which
``sending_scheduler`` drains with retries. Server Channels' own outbound is
best-effort by design; email is the one transport that already had a durable
queue, so it keeps it (plan §2.4). Google Chat is deliberately left as it was.

**Config** (non-secret, in ``ServerChannel.config``)::

    {"incoming_server_id": "<mail_server_config uuid, type=imap>",
     "outgoing_server_id": "<mail_server_config uuid, type=smtp>",
     "incoming_mailbox": "support@corp.com",
     "from_address":     "support@corp.com"}

Credentials are **not** here and not in ``ServerChannel.encrypted_secrets``.
They stay in ``mail_server_config.encrypted_password``, backend-only,
superuser-owned, never near an agent — which is why this transport declares
``needs_outbound_credentials=False``.
"""
from __future__ import annotations

import functools
import imaplib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import anyio.to_thread
from sqlalchemy import func, update
from sqlmodel import Session as DBSession, select

from app.models import ServerChannel
from app.models.email.email_message import EmailMessage
from app.models.email.mail_server_config import MailServerConfig, MailServerType
from app.models.email.outgoing_email_queue import OutgoingEmailQueue
from app.models.server_channels.channel_thread_binding import ChannelThreadBinding
from app.models.sessions.session import Session as ChatSession
from app.models.users.user import User
from app.services.email.imap_connector import imap_connector
from app.services.email.mail_server_service import MailServerService
from app.services.email.polling_service import EmailPollingService
from app.services.email.sending_service import EmailSendingService
from app.services.server_channels.adapters.base import (
    ChannelCapabilities,
    ChannelConfigError,
    ChannelInboundMessage,
    ChannelSendError,
    PolledChannelTransport,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[EmailChannel]"

#: Separator between the two Message-IDs in the transport-facing thread key.
#:
#: ``|`` is what plan §2.3 writes, and it is safe **given the normalization
#: below**. A bare Message-ID may legally contain ``|`` (it is RFC 5322
#: ``atext``), so splitting on the character alone would be ambiguous. Both
#: halves of the composite are angle-bracketed by
#: :func:`_normalize_message_id` before they are ever joined, so the split
#: happens on the three-character sequence ``>|<`` instead — and ``>`` and
#: ``<`` cannot appear *inside* a Message-ID, only as its delimiters. One
#: rule, no ambiguity.
_THREAD_KEY_SEPARATOR = "|"
_THREAD_KEY_SPLIT = ">|<"

#: Config keys, named once so the validator, the poller and the sender agree.
_CFG_INCOMING_SERVER = "incoming_server_id"
_CFG_OUTGOING_SERVER = "outgoing_server_id"
_CFG_INCOMING_MAILBOX = "incoming_mailbox"
_CFG_FROM_ADDRESS = "from_address"


# ======================================================================
# The transport-facing thread key
# ======================================================================


def _normalize_message_id(raw: str | None) -> str | None:
    """Return ``raw`` as ``<id>``, or ``None`` if there is nothing usable.

    Most servers emit the angle brackets; a few do not. Normalizing on the way
    *in* — before the value becomes a ``thread_key``, an
    ``external_message_id`` or half of a composite — is what lets everything
    downstream assume one spelling: the binding lookup is a string equality,
    and :func:`parse_reply_thread_key`'s split depends on the brackets being
    there.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if not value.startswith("<"):
        value = f"<{value}"
    if not value.endswith(">"):
        value = f"{value}>"
    return value


def build_reply_thread_key(root_message_id: str, last_message_id: str | None) -> str:
    """Join the thread root and the last inbound message into one key.

    The inverse of :func:`parse_reply_thread_key`, and the *only* place the
    composite is built. Called from
    ``channel_outbound_service._binding_thread_key`` — the single seam that
    derives a transport-facing thread key from a binding — because
    ``send_message(channel, thread_key, text)`` has nowhere else to carry the
    reply context that ``In-Reply-To`` and ``References`` need (settled
    decision §2.7).

    ``last_message_id`` is ``None`` on a binding that has not recorded one
    yet; the result is then the bare root, which parses back to
    ``(root, None)``.
    """
    if not last_message_id or last_message_id == root_message_id:
        return root_message_id
    return f"{root_message_id}{_THREAD_KEY_SEPARATOR}{last_message_id}"


def parse_reply_thread_key(thread_key: str) -> tuple[str, str | None]:
    """Split a transport-facing key into ``(root_message_id, last_message_id)``.

    Contract:

    * ``"<root>|<last>"`` → ``("<root>", "<last>")``
    * ``"<root>"``        → ``("<root>", None)``
    * anything the split does not recognise → ``(thread_key, None)``

    The third case is deliberate rather than an error. The root half is the
    **binding key** — it is what the stored ``ChannelThreadBinding.thread_key``
    equals and what the reply must be looked up by — so a key the parser
    cannot fully understand must still yield a usable root. The worst outcome
    is a reply that goes out without threading headers, which one mail client
    renders as a new conversation; refusing to send would lose the answer
    entirely.
    """
    key = thread_key or ""
    head, sep, tail = key.partition(_THREAD_KEY_SPLIT)
    if not sep:
        return key, None
    # ``partition`` consumed the ``>`` and the ``<`` that delimit the two ids.
    return f"{head}>", f"<{tail}"


class EmailChannelAdapter(PolledChannelTransport):
    """IMAP in on a timer, SMTP out through the durable queue."""

    channel_type: ClassVar[str] = "email"
    display_name: ClassVar[str] = "Email"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            # No progress notices. The pipeline calls ``notify_progress``
            # unconditionally, and a transport that answered it would mail the
            # sender "working on it", "still setting up" and "ready" as three
            # separate messages before the actual answer.
            supports_progress_updates=False,
            supports_message_edit=False,
            # Replies go out as ``text/plain`` (see
            # ``EmailSendingService._build_email_message``), so markdown would
            # reach the reader as literal asterisks.
            supports_markdown=False,
            # No hard per-message limit worth declaring: SMTP servers cap
            # message *size*, not characters, and the cap varies per server.
            max_message_chars=None,
            # The one that matters. A polled transport has no sync-reply
            # surface, so every denial in ``process_inbound`` reaches the
            # sender as nothing at all — decided behaviour (mailing declines
            # is a probing oracle and a spam amplifier), and never invisible
            # to the operator, who reads the debug feed instead.
            supports_sync_reply=False,
            inbound_mode="polled",
            needs_webhook_token=False,
            needs_outbound_credentials=False,
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> None:
        """Shape check only — see :meth:`validate_config_references` for the rest."""
        cfg = config or {}
        for key in (_CFG_INCOMING_SERVER, _CFG_OUTGOING_SERVER):
            raw = str(cfg.get(key) or "").strip()
            if not raw:
                raise ChannelConfigError(
                    f"Email channels require '{key}' — the id of a configured "
                    "mail server (Admin → Server Configuration → Mail servers)."
                )
            try:
                uuid.UUID(raw)
            except (TypeError, ValueError):
                raise ChannelConfigError(
                    f"'{key}' must be a mail server id."
                ) from None

        for key in (_CFG_INCOMING_MAILBOX, _CFG_FROM_ADDRESS):
            value = str(cfg.get(key) or "").strip()
            if not value:
                raise ChannelConfigError(
                    f"Email channels require '{key}' — an email address."
                )
            if "@" not in value:
                raise ChannelConfigError(f"'{key}' must be an email address.")

    def has_outbound_credentials(self, channel: ServerChannel) -> bool:
        """The referenced SMTP server *is* this transport's outbound credential.

        Nothing is ever stored in ``encrypted_secrets`` for an email channel
        (that is what ``needs_outbound_credentials=False`` declares), so the
        inherited reading of that column would report every working email
        channel as having no way to reply — a false alarm on the one admin
        screen that exists to tell an admin whether a channel is operational.

        Session-free on purpose, and sufficient because of
        :meth:`validate_config_references`: a channel cannot be created or
        updated with an ``outgoing_server_id`` that does not name an existing
        SMTP server, so the presence of the value already carries "an SMTP
        server was configured here". Re-verifying the row would need a database
        query per channel inside the admin list's per-row projection, to catch
        only one drift — a mail server deleted out from under a channel — which
        the same admin surface shows directly.
        """
        return bool(str((channel.config or {}).get(_CFG_OUTGOING_SERVER) or "").strip())

    def validate_config_references(
        self, db: DBSession, config: dict[str, Any]
    ) -> None:
        """Both referenced mail servers must exist and be of the right kind.

        A wrong id here does not fail loudly at configuration time — it
        silently polls nothing, or silently fails to reply, which is a
        miserable thing to debug. The same reasoning Google Chat's
        ``project_number`` check is written on.
        """
        cfg = config or {}
        for key, expected in (
            (_CFG_INCOMING_SERVER, MailServerType.IMAP),
            (_CFG_OUTGOING_SERVER, MailServerType.SMTP),
        ):
            try:
                server_id = uuid.UUID(str(cfg.get(key) or "").strip())
            except (TypeError, ValueError):
                # Unreachable through the services, which run the shape check
                # first — but this method is public on the adapter contract,
                # and a bad id must be an admin-readable refusal here too, not
                # a ValueError escaping as a 500.
                raise ChannelConfigError(
                    f"'{key}' must be a mail server id."
                ) from None
            server = db.get(MailServerConfig, server_id)
            if server is None:
                raise ChannelConfigError(
                    f"No mail server exists with the id given for '{key}'."
                )
            if server.server_type != expected:
                raise ChannelConfigError(
                    f"'{key}' must reference a {expected.value.upper()} server; "
                    f"{server.name!r} is {server.server_type.value.upper()}."
                )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def poll(self, channel: ServerChannel) -> list[ChannelInboundMessage]:
        """Fetch, authenticate and normalize everything new in the mailbox.

        **The authentication chokepoint for this channel, and the weakest one
        the platform has.** ``verify_inbound`` promises that nothing downstream
        re-checks the sender; this method makes the same promise for a pull
        transport, and it must be read together with *how strong* the promise
        is: the sender identity is the ``From:`` header, and **it is
        spoofable**. There is no signature over it, no platform-issued user id
        behind it, and no second gate anywhere below. An email channel's
        whitelist is therefore worth exactly what the receiving mail server's
        SPF/DKIM/DMARC enforcement is worth. Google Chat's equivalent is a
        Google-signed JWT; both now enter the same pipeline, and the admin who
        picks a whitelist has to be told which one they are configuring.

        Ordering inside one tick:

        1. fetch unread from the configured mailbox,
        2. drop anything not addressed to ``incoming_mailbox``
           (:meth:`EmailPollingService._is_addressed_to_channel`) — a shared
           IMAP account can carry mail for several channels, and mail
           addressed to somebody else is not this channel's to answer,
        3. mark **accepted** mail ``\\Seen``, so the next tick does not see it
           again,
        4. record every arrival durably, **before** anything classifies it
           (:meth:`_store_arrivals`), dropping a redelivery of mail an agent
           already answered,
        5. return the normalized messages.

        Step 3 covers accepted mail only. Marking the rest read as well would
        stop the re-fetch, but it would also consume another channel's mail
        out of a shared inbox before that channel's own poller ran — a silent
        cross-channel loss in exchange for saving a re-parse. The re-parse is
        the cheaper mistake.

        A transient failure (the mail server is down, credentials rejected) is
        logged and answered with an empty list: it is not an inbound event and
        must never be returned as one. The next tick retries.
        """
        cfg = channel.config or {}
        mailbox = str(cfg.get(_CFG_INCOMING_MAILBOX) or "").strip()
        raw_server_id = str(cfg.get(_CFG_INCOMING_SERVER) or "").strip()
        if not mailbox or not raw_server_id:
            logger.error(
                "%s Channel %s has no incoming mail configuration — not polling",
                _LOG_PREFIX,
                channel.id,
            )
            return []

        try:
            server_id = uuid.UUID(raw_server_id)
        except (TypeError, ValueError):
            logger.error(
                "%s Channel %s has a malformed %s — not polling",
                _LOG_PREFIX,
                channel.id,
                _CFG_INCOMING_SERVER,
            )
            return []

        # Opened here rather than taken as a parameter: ``poll(channel)`` is
        # the contract, and the credential read is the only thing this method
        # needs a session for. Imported inside the function so tests that
        # redirect ``app.core.db.create_session`` onto the test transaction
        # pick it up — the convention the rest of this pipeline follows.
        from app.core.db import create_session

        with create_session() as db:
            resolved = MailServerService.get_mail_server_with_credentials(
                db, server_id
            )
        if resolved is None:
            logger.error(
                "%s Channel %s references mail server %s, which no longer exists",
                _LOG_PREFIX,
                channel.id,
                server_id,
            )
            return []
        server, password = resolved

        # imaplib is blocking, and this coroutine runs on the main event loop.
        try:
            parsed_messages = await anyio.to_thread.run_sync(
                functools.partial(self._fetch_mailbox, server, password, mailbox)
            )
        except Exception as exc:  # noqa: BLE001 — a fetch failure is the scheduler's to retry
            logger.warning(
                "%s Poll failed for channel %s: %s", _LOG_PREFIX, channel.id, exc
            )
            return []

        # Store on arrival — BEFORE anything classifies, whitelists or denies.
        # Returns only the mail that is genuinely new; see the method.
        fresh = self._store_arrivals(channel, parsed_messages)

        return [self._to_inbound_message(parsed) for parsed in fresh]

    def _store_arrivals(
        self, channel: ServerChannel, parsed_messages: list[dict]
    ) -> list[dict]:
        """Persist each arrival and return the ones the pipeline should see.

        **Why on arrival, and not after routing.** The messages this record is
        most valuable for are exactly the ones routing never sees: a sender the
        whitelist denies, one the channel policy turns away, one with no
        platform account. Those declines are deliberately silent to the sender
        on a polled transport (a reply would be a probing oracle and a spam
        amplifier), so the operator is the only audience — and until now their
        only trace was ``ChannelDebugBuffer``, which is in-memory and
        process-local. On email, the one transport whose senders are external
        by definition, "silent to the sender and gone at the next restart" is
        not an audit story. The row is.

        **Duplicate suppression, and how far it goes.** A ``Message-ID`` is
        globally unique, so an arrival whose id is already on file is a
        redelivery — which happens for real: ``_mark_email_read`` logs and
        swallows an IMAP failure, so a mail can stay unread and come back on
        the next tick, and it comes back across a restart too, where the
        pipeline's in-memory ``_seen_recently`` cache no longer remembers it.

        The disposition depends on what the stored row says happened last time,
        and the split is deliberate:

        * **Already routed** (``agent_id`` stamped) — dropped here and never
          returned. This is the expensive redelivery: first contact would run
          classification again and can auto-install a bundle again. It is also
          the only case where dropping is safe, because "an agent already
          answered this" is a fact, not a guess.
        * **Not routed** (``agent_id`` NULL) — returned, and the row is left
          alone. The mail was denied, or its routing failed; both are states a
          retry may legitimately resolve, and the pipeline's own recovery paths
          are written on the assumption that a message it failed to process can
          come back. Suppressing those here would silently defeat them.

        Never fatal. A storage failure must not cost the platform an inbound
        message: the row is bookkeeping, the mail is the product.
        """
        if not parsed_messages:
            return []

        from app.core.db import create_session

        fresh: list[dict] = []
        with create_session() as db:
            for parsed in parsed_messages:
                message_id = _normalize_message_id(parsed.get("message_id"))
                try:
                    existing = (
                        db.exec(
                            select(EmailMessage).where(
                                EmailMessage.email_message_id == message_id
                            )
                        ).first()
                        if message_id
                        else None
                    )
                    if existing is not None:
                        if existing.agent_id is not None:
                            logger.info(
                                "%s Redelivery of already-routed mail %s on "
                                "channel %s — dropping",
                                _LOG_PREFIX,
                                message_id,
                                channel.id,
                            )
                            continue
                        fresh.append(parsed)
                        continue
                    db.add(_email_row(parsed))
                    db.commit()
                except Exception:  # noqa: BLE001 — bookkeeping never costs a message
                    logger.warning(
                        "%s Could not record arrival %s on channel %s",
                        _LOG_PREFIX,
                        message_id or "(no Message-ID)",
                        channel.id,
                        exc_info=True,
                    )
                    db.rollback()
                fresh.append(parsed)
        return fresh

    def record_routing_outcome(
        self,
        db: DBSession,
        channel: ServerChannel,
        *,
        thread_key: str,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> None:
        """Stamp the routing outcome onto this thread's stored arrivals.

        The second half of the two-step store :meth:`_store_arrivals` opens.
        Thread-wide rather than message-wide, and only over rows that are still
        unstamped: that is what heals an arrival which was stored before the
        thread had an agent at all (the first message of a thread is always
        one, and a parked message may be several).

        Restores a real reader as well as the audit trail:
        ``message_service._build_session_context`` looks the initiating
        ``EmailMessage`` up **by ``session_id``** to put the original subject
        into the agent's session context. With nothing stored, that lookup has
        been finding nothing for every channel-borne email.

        Session is a parameter, per the contract on the base method: the caller
        is mid-transaction and a connection opened here would write against a
        different snapshot.
        """
        root = _normalize_message_id(thread_key) or thread_key
        db.execute(
            update(EmailMessage)
            .where(
                EmailMessage.agent_id.is_(None),
                _stored_root_expr() == root,
            )
            .values(
                agent_id=agent_id,
                session_id=session_id,
                updated_at=datetime.now(UTC),
            )
        )
        db.commit()

    @staticmethod
    def _fetch_mailbox(
        server: MailServerConfig, password: str, mailbox: str
    ) -> list[dict]:
        """Blocking half of :meth:`poll`. Runs on a worker thread.

        Returns the parsed dicts for mail this channel accepts, oldest first,
        having marked exactly those ``\\Seen``.
        """
        conn: imaplib.IMAP4 | None = None
        accepted: list[dict] = []
        try:
            conn = imap_connector.connect(server, password)
            for msg_id, raw in EmailPollingService._fetch_unread_emails(conn):
                parsed = EmailPollingService._parse_email(raw)
                if parsed is None:
                    continue
                if not EmailPollingService._is_addressed_to_channel(
                    parsed["recipients"], mailbox
                ):
                    logger.debug(
                        "%s Skipping mail not addressed to %s (recipients=%s)",
                        _LOG_PREFIX,
                        mailbox,
                        parsed["recipients"],
                    )
                    continue
                EmailPollingService._mark_email_read(conn, msg_id)
                accepted.append(parsed)
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001 — best-effort socket release
                    try:
                        conn.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
        return accepted

    def _to_inbound_message(self, parsed: dict) -> ChannelInboundMessage:
        """Map one parsed email onto the transport-agnostic inbound shape.

        ``thread_key`` is the **root** Message-ID of the thread, never the
        latest: ``References[0]`` if the sender's client sent a chain, else
        ``In-Reply-To``, else this message's own Message-ID (which is what
        makes a first message the root of its own thread). Keying on the
        latest id instead would give every reply a fresh binding, and the
        symptom is not "threading is broken" — it is *the agent forgets the
        conversation*, which reads as a session bug and is hunted for in the
        wrong place.

        ``external_user_id`` is the sender address because no stronger id
        exists on this transport. ``external_message_id`` is this message's
        own id, which is the pipeline's redelivery dedup key and the value
        that later becomes ``binding.last_external_message_id`` — the
        ``In-Reply-To`` of the eventual answer.
        """
        own_id = _normalize_message_id(parsed.get("message_id"))
        root_id = (
            self._root_message_id(parsed.get("references"), parsed.get("in_reply_to"))
            or own_id
        )
        sender = (parsed.get("sender") or "").strip() or None

        if not root_id or not sender:
            # No binding key, or nothing to identify the sender by. The
            # pipeline's own guards would drop this anyway; saying so here
            # keeps the reason next to the parse.
            return ChannelInboundMessage(event_kind="ignored", raw={})

        return ChannelInboundMessage(
            event_kind="message",
            # Spoofable. See this module's docstring and ``poll``.
            sender_email=sender,
            sender_display_name=parsed.get("sender_display_name") or None,
            external_user_id=sender,
            thread_key=root_id,
            text=EmailPollingService.format_email_as_message(
                _email_row(parsed)
            ),
            external_message_id=own_id,
        )

    @staticmethod
    def _root_message_id(references: str | None, in_reply_to: str | None) -> str | None:
        """First id of the ``References`` chain, else ``In-Reply-To``, else None."""
        chain = (references or "").split()
        if chain:
            return _normalize_message_id(chain[0])
        return _normalize_message_id(in_reply_to)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_message(
        self, channel: ServerChannel, thread_key: str, text: str
    ) -> str | None:
        """Enqueue the reply. Does **not** send — ``sending_scheduler`` does.

        Returns ``None`` rather than a platform message id, and there is no
        better answer available: the id of an email is its ``Message-ID``,
        which the SMTP server assigns at delivery, minutes after this call
        returns. Nothing on the channel path consumes the return value (only
        ``supports_message_edit`` transports need one, and this is not one).

        ``thread_key`` arrives as the composite built by
        :func:`build_reply_thread_key`. The root half is the binding key; the
        last half becomes ``In-Reply-To`` and the tail of ``References``.

        Everything else the queue row needs — who to answer, which agent it
        belongs to, which session it continues — comes from the binding, which
        is the (channel, thread) → (user, agent, session) map and the only
        row that knows. The recipient is deliberately the **resolved platform
        account's** address rather than the ``From:`` header the message
        arrived with: the header is spoofable, so replying to it would let a
        forged sender redirect an agent's answer, while replying to the
        account address sends it to the person whose identity was actually
        claimed.

        Raises ``ChannelSendError`` for every case where the reply cannot be
        addressed — the caller (``ChannelOutboundService._deliver``) records
        that on the binding and in the debug feed, which is the operator's
        surface for an email channel that is answering nothing.
        """
        if not text:
            return None

        root_id, last_id = parse_reply_thread_key(thread_key)

        cfg = channel.config or {}
        if not str(cfg.get(_CFG_FROM_ADDRESS) or "").strip():
            raise ChannelSendError(
                f"Channel {channel.id} has no {_CFG_FROM_ADDRESS} configured"
            )
        if not str(cfg.get(_CFG_OUTGOING_SERVER) or "").strip():
            raise ChannelSendError(
                f"Channel {channel.id} has no {_CFG_OUTGOING_SERVER} configured"
            )

        from app.core.db import create_session

        with create_session() as db:
            binding = db.exec(
                select(ChannelThreadBinding).where(
                    ChannelThreadBinding.server_channel_id == channel.id,
                    ChannelThreadBinding.thread_key == root_id,
                )
            ).first()
            if binding is None:
                raise ChannelSendError(
                    f"No thread binding for {root_id!r} on channel {channel.id}"
                )
            if binding.session_id is None:
                # ``sending_service`` resolves the SMTP configuration through
                # session → binding → channel, so a row with no session can
                # never be addressed. Refusing here keeps the failure on the
                # binding, where an operator will find it, instead of parking
                # an unsendable row in the queue until it fails there.
                raise ChannelSendError(
                    f"Binding {binding.id} has no session — nothing to reply to"
                )

            recipient = db.get(User, binding.user_id)
            if recipient is None or not recipient.email:
                raise ChannelSendError(
                    f"Binding {binding.id} has no resolvable recipient address"
                )

            chat_session = db.get(ChatSession, binding.session_id)
            subject = (
                EmailSendingService._build_reply_subject(chat_session)
                if chat_session is not None
                else "Agent Response"
            )

            entry = OutgoingEmailQueue(
                # NOT NULL on the queue, and the binding is what supplies it:
                # the agent that answered. On an identity-routed thread that
                # is the identity owner's agent, which is correct — it is the
                # agent whose reply this is.
                agent_id=binding.agent_id,
                session_id=binding.session_id,
                recipient=recipient.email,
                subject=subject[:1000],
                body=text,
                in_reply_to=(last_id or root_id)[:512],
                references=_references_chain(root_id, last_id),
            )
            db.add(entry)
            db.commit()
            logger.info(
                "%s Queued reply for channel %s thread %s (entry %s)",
                _LOG_PREFIX,
                channel.id,
                root_id,
                entry.id,
            )
        return None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def get_setup_instructions(
        self, channel: ServerChannel, webhook_url: str | None
    ) -> tuple[dict[str, str], list[str]]:
        """Admin setup panel. ``webhook_url`` is always ``None`` here.

        The trust-tier warning is not decoration. This panel is where an admin
        decides what to put in ``email_whitelist``, and the whitelist on this
        transport gates on an address anybody can write into a header.
        """
        cfg = channel.config or {}
        details = {
            "Connection type": "Polled IMAP (no inbound URL)",
            "Polled mailbox": str(cfg.get(_CFG_INCOMING_MAILBOX) or "(not set)"),
            "Reply from": str(cfg.get(_CFG_FROM_ADDRESS) or "(not set)"),
            "Incoming mail server": str(cfg.get(_CFG_INCOMING_SERVER) or "(not set)"),
            "Outgoing mail server": str(cfg.get(_CFG_OUTGOING_SERVER) or "(not set)"),
            "Sender verification": (
                "From: header — SPOOFABLE. Unlike Google Chat, nothing signs "
                "the sender's address. This channel's whitelist is only as "
                "strong as your mail server's SPF/DKIM/DMARC enforcement."
            ),
        }
        steps = [
            "Add the IMAP and SMTP servers under Admin → Server Configuration "
            "→ Mail servers, and test both connections there first.",
            "Select them above as the incoming and outgoing servers for this "
            "channel.",
            "Set the polled mailbox to the address people will write to, and "
            "the reply-from address the answers should come from.",
            "This channel has no inbound URL — nothing is pushed to the "
            "platform. A scheduler polls the mailbox on a timer, so the first "
            "answer can take up to one poll interval.",
            "Set the email whitelist deliberately: the sender address comes "
            "from the From: header and can be forged by anyone who can deliver "
            "mail to this mailbox.",
            "Mail addressed to a different recipient in the same inbox is "
            "ignored, so one IMAP account can serve several channels.",
        ]
        return details, steps


def _references_chain(root_id: str, last_id: str | None) -> str:
    """The ``References`` header for a reply: the root, then the parent.

    Space-separated, oldest first — the RFC 5322 shape every mail client
    threads on. Collapsed to the root alone when the parent *is* the root
    (the first reply in a conversation).
    """
    if not last_id or last_id == root_id:
        return root_id
    return f"{root_id} {last_id}"


def _normalize_references(raw: str | None) -> str | None:
    """Normalize a ``References`` chain to space-separated ``<id>`` tokens.

    The header arrives folded across lines and with whatever bracket spelling
    the sending client chose. Every id in the stored row is written in the one
    spelling :func:`_normalize_message_id` defines, for the same reason that
    function exists: the thread root is later recovered from this column by a
    **SQL** expression (:func:`_stored_root_expr`), which cannot re-run
    Python's ``str.split()``. One spelling in the column, one split rule in the
    query.

    ``None`` when there is nothing usable, matching the nullable column.
    """
    tokens = [
        normalized
        for normalized in (_normalize_message_id(part) for part in (raw or "").split())
        if normalized
    ]
    return " ".join(tokens) or None


def _email_row(parsed: dict) -> EmailMessage:
    """Build an ``EmailMessage`` from a parsed mail. Unsaved; the caller decides.

    One builder, two callers, and they want the same row for different reasons:
    :meth:`EmailChannelAdapter._store_arrivals` persists it, and
    :meth:`EmailChannelAdapter._to_inbound_message` hands it to
    ``EmailPollingService.format_email_as_message`` (which takes a row, not a
    dict). Keeping the construction in one place is what makes "the audit row
    and the text the agent reads describe the same mail" true by construction
    rather than by review.

    All three id fields are normalized here rather than stored verbatim. The
    row is now the durable half of a two-step record whose other half is keyed
    on the *thread root*, and the root is recomputed from these columns in SQL;
    a column holding ``a@b`` where the thread key holds ``<a@b>`` would never
    match. Nothing reads these fields for their original bytes — the formatter
    uses subject/sender/body only — so normalizing costs nothing and buys the
    join.

    ``agent_id`` and ``session_id`` are deliberately absent: the row is written
    on arrival, before classification has produced either, and
    :meth:`EmailChannelAdapter.record_routing_outcome` stamps them afterwards
    if and when routing succeeds. See ``EmailMessage.agent_id``.
    """
    return EmailMessage(
        email_message_id=_normalize_message_id(parsed.get("message_id")) or "",
        sender=parsed.get("sender") or "",
        subject=parsed.get("subject") or "",
        body=parsed.get("body") or "",
        references=_normalize_references(parsed.get("references")),
        in_reply_to=_normalize_message_id(parsed.get("in_reply_to")),
        received_at=parsed["received_at"],
        attachments_metadata=parsed.get("attachments_metadata"),
    )


def _stored_root_expr():
    """SQL for a stored row's thread root — the exact mirror of the Python rule.

    :meth:`EmailChannelAdapter._to_inbound_message` derives the binding key as
    ``References[0]`` → ``In-Reply-To`` → the message's own id. This is that
    same fallback chain expressed over the stored columns, so a row can be
    found by the thread key it would have produced without storing the key
    twice (which would be a second column, a second migration, and a second
    thing to keep true).

    ``split_part`` is Postgres-specific and that is fine here — the platform is
    Postgres-only — but it is the reason :func:`_normalize_references` collapses
    the chain to single spaces first: this expression splits on one space, not
    on arbitrary whitespace.
    """
    return func.coalesce(
        func.nullif(
            func.split_part(func.coalesce(EmailMessage.references, ""), " ", 1), ""
        ),
        func.nullif(EmailMessage.in_reply_to, ""),
        EmailMessage.email_message_id,
    )



__all__ = [
    "EmailChannelAdapter",
    "build_reply_thread_key",
    "parse_reply_thread_key",
]
