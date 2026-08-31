"""Outbound delivery: agent replies and progress notices back to the channel.

Two entry points:

* Event subscribers (``handle_stream_completed`` / ``handle_stream_error``)
  registered in ``app/main.py`` next to the email integration's. They fire for
  every stream on the instance, so the first thing each does is a cheap
  ``integration_type.startswith("channel_")`` gate — everything else is only
  reached for sessions this feature owns.
* The **status notice** helpers (``set_status`` / ``set_binding_status`` /
  ``clear_binding_status``), called by the inbound pipeline to narrate the slow
  parts — routing, installing, ready, failed. On a transport that can edit its
  own posts that narration is ONE message, rewritten in place, and the last
  rewrite is the agent's own reply (``_deliver(into_status_notice=True)``) — so
  the notice does not disappear, it *becomes* the answer. A transport that can
  post but not edit gets each state as its own message instead (``set_status``
  falls through to ``_send_notice`` and keeps no id), and one that has no
  progress surface at all gets silence — both handled inside the helpers, so
  callers never branch on it.

Delivery is best-effort: three attempts inside the adapter, then the failure
is recorded on the binding and logged. A persistent outbound queue (the email
integration's ``OutgoingEmailQueue`` pattern) is a listed future enhancement —
until then a user whose reply was lost can simply ask again.

Everything here runs as an asyncio task on the main event loop. HTTP is async
httpx; the DB work mirrors what every other event handler on that loop does.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session as DBSession, select

from app.models import (
    ChannelThreadBinding,
    ServerChannel,
    Session as ChatSession,
    SessionMessage,
)
from app.services.routing import routing_trace
from app.services.server_channels.adapters.email import build_reply_thread_key
from app.services.server_channels.adapters.registry import (
    get_adapter,
    get_transport,
)
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_REPLIED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelOutbound]"


def _binding_thread_key(
    binding: ChannelThreadBinding, channel: ServerChannel | None = None
) -> str | None:
    """The transport-facing thread key for ``binding``, or ``None``.

    **Total by construction**, and the single place a transport-facing thread
    key is derived from a binding.

    The binding-shaped sibling of
    ``channel_inbound_service._debug_channel_key``, and it exists rather than
    reusing it for one reason: that helper reads ``channel.id``, and there is
    no total reader for a *binding* attribute to reuse. The hazard is
    identical — ``binding.thread_key`` looks like a field read and is not.
    Every path into ``_deliver`` arrives after a ``db.commit()`` (the inbound
    pipeline commits between every progress notice; the event handlers commit
    while resolving the session), which expires the instance, so the read is a
    lazy reload and reloading a concurrently deleted binding raises
    ``ObjectDeletedError``.

    ``None`` means "this message cannot be addressed", and the caller declines
    to send rather than posting to a null thread — the same bargain
    ``_debug_channel_key`` strikes, for the same reason: a delivery aimed at
    nothing is worse than an honest, logged non-delivery.

    **``channel`` and the reply context (settled decision §2.7).** A polled
    transport's reply needs more than a thread id: an email answer carries
    ``In-Reply-To`` and ``References``, which name the *last* inbound message,
    not the thread root. ``send_message(channel, thread_key, text)`` has no
    room for them, so the polled key is a composite —
    ``"<root-message-id>|<last-message-id>"`` — built here and parsed by the
    transport. The **stored** ``binding.thread_key`` is untouched: it stays the
    bare root and remains the unique key everything binds by.

    ``binding.last_external_message_id`` is read **inside the same ``try``**,
    and that placement is the whole point rather than tidiness. It is the same
    expired-instance lazy reload ``thread_key`` is, so a read outside the guard
    would let a concurrently deleted binding raise out of a helper whose
    callers rely on it never raising — turning an honest declined delivery into
    a crash on the delivery path.

    ``channel`` is optional and defaults to "no reply context", which is
    exactly right for every webhook transport (Google Chat's key is already
    complete) and is what a caller that has no channel to hand gets. Only the
    transport shape decides: ``inbound_mode == "polled"``. The composite's
    format belongs to the polled transport that reads it back —
    ``adapters.email`` defines the separator, the builder and the parser in one
    place — so a second polled transport with a different reply shape needs
    its own branch here, not a different spelling of this one.
    """
    try:
        thread_key = str(binding.thread_key)
        # Same reload, same guard. See the docstring.
        last_external_message_id = binding.last_external_message_id
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read a thread key from the binding (instance expired "
            "and its row is gone?)",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None

    if channel is None:
        return thread_key

    try:
        # ``channel.channel_type`` is a lazy reload too, and this helper may
        # not raise. A channel we cannot classify degrades to the bare thread
        # key rather than to ``None``: the key is still the right address, and
        # only the threading headers are lost. (``_deliver``'s own
        # ``get_adapter`` call raises on the same row a moment later and is
        # handled there — this is not the place to answer for it.)
        transport = get_transport(channel.channel_type)
    except Exception:  # noqa: BLE001 — degrade, never raise
        logger.warning(
            "%s Could not resolve the transport for a delivery; sending with "
            "the bare thread key",
            _LOG_PREFIX,
            exc_info=True,
        )
        return thread_key

    if transport.inbound_mode != "polled":
        return thread_key
    return build_reply_thread_key(thread_key, last_external_message_id)


def _binding_status_message_id(binding: ChannelThreadBinding) -> str | None:
    """The binding's live status-notice id, or ``None``. Never raises.

    The same expired-instance lazy reload :func:`_binding_thread_key` exists
    for, and read through the same kind of guard for the same reason: every
    caller arrives after a ``db.commit()``, so this is a reload, and a
    concurrently deleted binding would raise ``ObjectDeletedError`` out of a
    helper whose callers treat it as a field read.

    ``None`` on failure is the right degradation and not merely the safe one:
    it means "no notice to patch or delete", which sends the caller down the
    post-a-fresh-one path — a visible extra message at worst, never a lost one.
    """
    try:
        return binding.status_message_id
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read the status notice id from the binding",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None


# Prefix used for the integration_type stamped on channel sessions.
CHANNEL_INTEGRATION_PREFIX = "channel_"


class ChannelOutboundService:
    """Sends agent output back out through the originating channel."""

    # ------------------------------------------------------------------
    # Event subscribers
    # ------------------------------------------------------------------

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """STREAM_COMPLETED — deliver the agent's final message to the thread."""
        try:
            from app.core.db import create_session

            meta = event_data.get("meta") or {}
            session_id = meta.get("session_id")
            if not session_id or meta.get("was_interrupted"):
                return

            with create_session() as db:
                resolved = ChannelOutboundService._resolve_channel_session(
                    db, session_id
                )
                if resolved is None:
                    return
                binding, channel = resolved

                text = ChannelOutboundService._last_agent_message(
                    db, uuid.UUID(str(session_id))
                )
                if not text:
                    logger.debug(
                        "%s No agent message for session %s — nothing to send",
                        _LOG_PREFIX,
                        session_id,
                    )
                    # Nothing to put in the notice's slot, and the turn is over
                    # — so this is one of the two edges where the notice really
                    # is deleted. A "working on your message" left standing
                    # over a stream that produced nothing would outlive the
                    # work it narrates and be rewritten by the NEXT turn,
                    # telling the person we are still busy with a message they
                    # sent minutes ago.
                    await ChannelOutboundService.clear_binding_status(
                        db=db, channel=channel, binding=binding
                    )
                    return

                # Into the notice's slot, not underneath it. See `_deliver`.
                await ChannelOutboundService._deliver(
                    db=db,
                    channel=channel,
                    binding=binding,
                    text=text,
                    into_status_notice=True,
                )
        except Exception:
            # An event handler must never raise into the bus.
            logger.exception("%s handle_stream_completed failed", _LOG_PREFIX)

    @staticmethod
    async def handle_stream_error(event_data: dict[str, Any]) -> None:
        """STREAM_ERROR — tell the thread the turn failed, briefly.

        The error text itself is deliberately not forwarded: it can carry
        internal detail, and the external caller can act on neither.
        """
        try:
            from app.core.db import create_session

            meta = event_data.get("meta") or {}
            session_id = meta.get("session_id")
            if not session_id:
                return

            with create_session() as db:
                resolved = ChannelOutboundService._resolve_channel_session(
                    db, session_id
                )
                if resolved is None:
                    return
                binding, channel = resolved

                # The failure notice takes the status notice's slot too: it is
                # this turn's answer, and the person should read one message,
                # not a spinner with an apology under it.
                await ChannelOutboundService._deliver(
                    db=db,
                    channel=channel,
                    binding=binding,
                    text=(
                        "Something went wrong while I was working on that. "
                        "Please try again."
                    ),
                    into_status_notice=True,
                )
        except Exception:
            logger.exception("%s handle_stream_error failed", _LOG_PREFIX)

    # ------------------------------------------------------------------
    # Status notice
    # ------------------------------------------------------------------
    #
    # One message per thread that narrates the slow work and then turns into
    # the answer. The alternative — and what this replaced — is a notice per
    # state: "finding an assistant", "setting up X", "ready, working on it",
    # each a permanent message sitting above the answer the person actually
    # asked for. Chat gives us ``patch`` over our own posts, so the whole
    # narration can be one message that mutates all the way through.
    #
    # Three verbs, and the difference between them is the whole model:
    #
    #   set     post the notice, or rewrite it in place. The id is kept.
    #   settle  rewrite it one last time and let go of the id — for a text
    #           that IS the answer ("no assistant matched", "setup failed").
    #           The message stays; nothing will rewrite it again.
    #   clear   delete it. NOT the normal end of a turn — an agent's reply
    #           *takes* the slot (``_deliver(into_status_notice=True)``)
    #           rather than being posted under a notice that is then removed,
    #           because Chat leaves a "Message deleted by its author"
    #           tombstone and that put one above every answer. This is for the
    #           two edges with genuinely nothing to say: a stream that
    #           produced no message, and a routing race whose loser's notice
    #           has no thread left to narrate.
    #
    # Every one of them is best-effort and none of them raise: a thread whose
    # notice could not be posted, patched or removed still gets its answer, and
    # that is the ordering of priorities. A failed patch degrades to a fresh
    # post rather than to a lost update.

    @staticmethod
    async def set_status(
        *,
        channel: ServerChannel,
        thread_key: str,
        message_id: str | None,
        text: str,
    ) -> str | None:
        """Post or rewrite a thread's status notice. Returns its id, or None.

        **Session-free on purpose.** Everything here is transport work — an
        HTTP call and an in-memory debug record — and taking a ``Session`` it
        never used invited callers to hold a pooled connection open across it.
        ``_route_new_thread`` did exactly that: its notice went out with the
        outer transaction still open, above the ``db.commit()`` whose own
        comment forbids it. The persistence half lives in
        :meth:`set_binding_status`, which does have a row to write to.

        Thread-keyed rather than binding-keyed because the first notice of a
        new thread is posted **before** the binding exists — routing has to run
        before anything can be bound, and routing is the slow part the notice
        is narrating. The caller holds the returned id as a plain value and
        writes it onto the binding once there is one, the same way it carries
        ``policy`` across that hop.

        ``None`` comes back when the transport has no progress surface at all,
        when the notice was delivered as an ordinary message (a transport that
        can post but not edit), and when the post simply **failed** — in all
        three there is nothing for a later ``clear`` to act on, which is
        exactly what a ``None`` id means to every caller.

        Those three are not equally benign, and the caller has to tell them
        apart itself: a ``None`` from a transport that declares
        ``supports_status_notice`` is a notice that should have been posted and
        was not, which on that transport is the sender's whole acknowledgement.
        ``_route_new_thread`` warns on exactly that combination.

        **Total, and that is load-bearing.** The band comment above says none
        of these three verbs raise, and this line is what makes that true of
        this one. ``channel.channel_type`` is not a field read: every caller
        arrives after a ``db.commit()`` that expired the instance, so it is a
        lazy reload — ``ObjectDeletedError`` (row deleted concurrently),
        ``OperationalError`` (pool timeout, disconnect) and
        ``PendingRollbackError`` (a poisoned session, which is exactly the
        state the failure handlers that call this run in) are none of them
        ``ChannelError``. A raise here escapes ``set_binding_status`` and
        ``_settle_notice`` alike, and the callers it escapes into are the ones
        with no handler left: the inbound pipeline's ownership hand-off (the
        binding row keeps the id while the caller settles a stale local, so
        the flush loop patches "ready" over the sender's last word) and the
        outer failure handler itself (the notice is stranded on "Setting up…"
        forever). Same treatment, for the same hazard, as
        ``_debug_channel_key``, ``_binding_thread_key`` and
        ``_persist_status_message_id``.
        """
        try:
            adapter = get_adapter(channel.channel_type)
        except Exception:  # noqa: BLE001 — see the docstring: a lazy reload
            logger.warning(
                "%s Could not resolve the adapter for a status notice on "
                "thread %s — skipping it",
                _LOG_PREFIX,
                thread_key,
                exc_info=True,
            )
            return None
        capabilities = adapter.capabilities
        if not capabilities.supports_progress_updates:
            return None
        if not capabilities.supports_status_notice:
            # No edit/delete: fall back to a plain message and keep no id.
            await ChannelOutboundService._send_notice(channel, thread_key, text)
            return None

        if message_id:
            try:
                await adapter.update_message(channel, thread_key, message_id, text)
                ChannelOutboundService._record_notice(
                    channel, thread_key, text, "Status notice updated"
                )
                return message_id
            except Exception as exc:  # noqa: BLE001 — degrade to a fresh notice
                # The debug feed is where an operator diagnoses a channel that
                # has gone quiet, so a failed patch is recorded even though the
                # caller recovers from it — otherwise the only trace of a
                # channel whose every patch is failing is a doubled notice
                # nobody can explain.
                ChannelOutboundService._record_notice(
                    channel,
                    thread_key,
                    text,
                    f"Status notice update failed: "
                    f"{routing_trace.describe_exception(exc)} — reposting",
                    failed=True,
                )
                logger.warning(
                    "%s Could not rewrite the status notice on thread %s — "
                    "posting a new one",
                    _LOG_PREFIX,
                    thread_key,
                    exc_info=True,
                )

        return await ChannelOutboundService._send_notice(channel, thread_key, text)

    @staticmethod
    async def clear_status(
        *,
        channel: ServerChannel,
        thread_key: str,
        message_id: str | None,
    ) -> None:
        """Delete a thread's status notice. Never raises; already-gone is fine.

        **Not the normal end of a turn.** A reply takes the notice's slot
        instead — see ``_deliver(into_status_notice=True)`` — because a deleted
        Chat message leaves a "Message deleted by its author" tombstone, and
        clearing the notice after posting the reply beneath it put one of those
        above every answer. Reach for this only where the turn ends with
        nothing to put in the slot.

        Gated on ``supports_message_delete``, which is checked here rather than
        folded into ``supports_status_notice``: a transport that can edit but
        not delete runs the notice perfectly well and only loses this edge.
        """
        if not message_id:
            return
        try:
            adapter = get_adapter(channel.channel_type)
            if not adapter.capabilities.supports_message_delete:
                return
            await adapter.delete_message(channel, thread_key, message_id)
        except Exception:  # noqa: BLE001 — a stale notice is not worth a failure
            logger.warning(
                "%s Could not clear the status notice on thread %s",
                _LOG_PREFIX,
                thread_key,
                exc_info=True,
            )

    @staticmethod
    async def set_binding_status(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
        settle: bool = False,
    ) -> None:
        """``set_status`` for a bound thread, persisting the notice id.

        ``settle=True`` writes the final word: the notice is rewritten and then
        *released* — the id is dropped so nothing later deletes it. Use it for
        text that is itself the answer, and never for a state the conversation
        moves on from.
        """
        thread_key = _binding_thread_key(binding, channel)
        if thread_key is None:
            return
        message_id = await ChannelOutboundService.set_status(
            channel=channel,
            thread_key=thread_key,
            message_id=_binding_status_message_id(binding),
            text=text,
        )
        ChannelOutboundService._persist_status_message_id(
            db, binding, None if settle else message_id
        )

    @staticmethod
    async def clear_binding_status(
        *, db: DBSession, channel: ServerChannel, binding: ChannelThreadBinding
    ) -> None:
        """Delete a bound thread's status notice and forget it.

        Read :meth:`clear_status` before adding a call site: this is the edge
        case, not the happy path.
        """
        message_id = _binding_status_message_id(binding)
        if message_id is None:
            return
        thread_key = _binding_thread_key(binding, channel)
        if thread_key is not None:
            await ChannelOutboundService.clear_status(
                channel=channel, thread_key=thread_key, message_id=message_id
            )
        ChannelOutboundService._persist_status_message_id(db, binding, None)

    @staticmethod
    def adopt_status_notice(
        db: DBSession, binding: ChannelThreadBinding, message_id: str | None
    ) -> None:
        """Hand a notice posted before the binding existed over to the binding.

        The first notice of a new thread is posted while routing runs, which is
        strictly before there is anything to bind. The id travels as a plain
        local through ``_route_new_thread`` — the same way ``policy`` and
        ``origin`` do across that hop — and lands here once the binding is
        created, so every later turn can find the notice it has to rewrite.

        Never raises; see :meth:`_persist_status_message_id`.
        """
        ChannelOutboundService._persist_status_message_id(db, binding, message_id)

    @staticmethod
    async def _send_notice(
        channel: ServerChannel, thread_key: str, text: str
    ) -> str | None:
        """Post a notice, swallowing delivery failure. Returns its id."""
        try:
            adapter = get_adapter(channel.channel_type)
            message_id = await adapter.send_message(channel, thread_key, text)
        except Exception as exc:  # noqa: BLE001 — best effort, like every notice
            ChannelOutboundService._record_notice(
                channel,
                thread_key,
                text,
                f"Notice delivery failed: {routing_trace.describe_exception(exc)}",
                failed=True,
            )
            logger.warning(
                "%s Could not post a status notice to thread %s",
                _LOG_PREFIX,
                thread_key,
                exc_info=True,
            )
            return None
        ChannelOutboundService._record_notice(
            channel, thread_key, text, "Status notice delivered"
        )
        # The id goes straight into a varchar column and is later handed back
        # to the transport as a message name, so it has to actually be one.
        # An adapter that returns anything else is a bug, and the honest
        # degradation is "this thread has no notice" — the next state posts a
        # fresh one — rather than a write that fails at commit time.
        if not isinstance(message_id, str) or not message_id:
            return None
        return message_id[:255]

    @staticmethod
    def _record_notice(
        channel: ServerChannel,
        thread_key: str,
        text: str,
        summary: str,
        *,
        failed: bool = False,
    ) -> None:
        """File a notice in the debug feed. Never raises.

        The status notice replaced ``_reply`` on several pipeline paths, and
        ``_reply`` recorded every one of them. Without this the admin panel
        would go blind exactly where the feature got chattier — a thread whose
        notice is being rewritten five times would show nothing at all.

        ``channel.id`` is read through the total helper for the reason the rest
        of this file states at length: it is an argument expression, evaluated
        before ``ChannelDebugBuffer.record``'s own guard can cover it, and on
        these paths it is a lazy reload.
        """
        from app.services.server_channels.channel_inbound_service import (
            _debug_channel_key,
        )

        debug_channel_id = _debug_channel_key(channel)
        if debug_channel_id is None:
            return
        ChannelDebugBuffer.record(
            channel_id=debug_channel_id,
            direction="outbound",
            kind=DEBUG_SEND_FAILED if failed else DEBUG_REPLIED,
            summary=summary,
            thread_key=thread_key,
            text=text,
        )

    @staticmethod
    def _persist_status_message_id(
        db: DBSession, binding: ChannelThreadBinding, message_id: str | None
    ) -> None:
        """Write the notice id onto the binding. Never raises.

        Guarded for the same reason ``_record_error`` is: the binding instance
        is expired after every commit on these paths, so both the read and the
        write are lazy reloads, and a concurrently deleted binding raises
        ``ObjectDeletedError`` out of what is only bookkeeping. A lost id costs
        one extra round trip on the next turn (the patch misses, a fresh notice
        is posted); a raise here would abort a delivery that already happened.
        """
        try:
            if binding.status_message_id == message_id:
                return
            binding.status_message_id = message_id
            db.add(binding)
            db.commit()
        except Exception:  # noqa: BLE001 — see the docstring
            logger.warning(
                "%s Could not persist the status notice id", _LOG_PREFIX, exc_info=True
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s Rollback after a failed status-id write also failed",
                    _LOG_PREFIX,
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_channel_session(
        db: DBSession, session_id: Any
    ) -> tuple[ChannelThreadBinding, ServerChannel] | None:
        """Cheap gate + binding/channel lookup for a stream event.

        Returns None for every session this feature does not own — which is
        almost all of them, so the ``integration_type`` check comes first and
        costs one already-loaded column.
        """
        try:
            session_uuid = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return None

        chat_session = db.get(ChatSession, session_uuid)
        if chat_session is None:
            return None
        integration_type = chat_session.integration_type or ""
        if not integration_type.startswith(CHANNEL_INTEGRATION_PREFIX):
            return None

        binding = db.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.session_id == session_uuid
            )
        ).first()
        if binding is None:
            logger.warning(
                "%s Session %s is a channel session with no binding",
                _LOG_PREFIX,
                session_id,
            )
            return None

        channel = db.get(ServerChannel, binding.server_channel_id)
        if channel is None or not channel.enabled:
            return None
        return binding, channel

    @staticmethod
    def _last_agent_message(db: DBSession, session_id: uuid.UUID) -> str | None:
        row = db.exec(
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "agent",
            )
            .order_by(SessionMessage.sequence_number.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return (row.content or "").strip() or None

    @staticmethod
    async def _deliver(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
        into_status_notice: bool = False,
    ) -> bool:
        """Send through the adapter, recording failure on the binding.

        ``into_status_notice`` delivers **into** the thread's open status
        notice — the message that has been narrating this turn is rewritten to
        hold the answer — instead of posting a new one below it.

        That is the fix for what the delete-based version looked like in
        practice: Chat renders a deleted message as "Message deleted by its
        author", so clearing the notice after posting the reply put a tombstone
        above every single answer. Reusing the slot means the notice was never
        a separate message from the reader's point of view; it was the reply,
        arriving in stages.

        The id is released **when, and only when, the notice was really
        taken over**. It has to be: the message now holds the agent's answer,
        and the next turn's "working on your message…" would otherwise be
        patched straight over it.

        There are two ways not to take it over, and they used to be one.
        Delivery raising is the obvious one — the id stays, so the notice is
        still there to be rewritten or retried rather than orphaned
        mid-sentence. The other is invisible from here unless the adapter says
        so: ``replace_message`` degrades to an ordinary post when the patch
        fails, so a stale id (message hand-deleted, scope lost, id from before
        a redeploy) produced a *successful* delivery, a debug line claiming the
        reply had gone into the notice, and a released id — leaving "💬 Working
        on your message…" standing above the answer with nothing owning it and
        nothing able to rewrite it again. ``ChannelReplaceResult.replaced``
        makes that case visible, and the answer is to **keep** the id: the next
        turn patches that same message back to "working…" and self-heals, so a
        permanently broken patch costs one stale notice rather than one per
        turn. Deleting the orphan instead is not an option — that is the
        tombstone this whole mechanism exists to avoid.

        §11a Rule 2, and the worse twin of ``ChannelInboundService._reply``:
        this is the **agent-reply** path, so it carries the traffic the notice
        path does not. Five expressions were exposed inside the one ``except``
        — ``channel.id`` twice, ``binding.thread_key`` twice, and the
        ``f"...{exc}"`` summary, plus ``str(exc)`` in the ``_record_error``
        call and a lazily-interpolated ``exc`` in the log. Python evaluates
        every one of them *before* entering the callee, so neither
        ``ChannelDebugBuffer.record``'s never-raises guard nor
        ``_record_error``'s reached any of them. A raise in any one replaced
        the delivery exception, skipped the remaining statements, and left the
        failure invisible in the debug buffer **and** on the binding row — the
        two places an operator looks. Confirmed by firing poison objects, with
        ``logging`` disabled so the result is production behaviour and not
        pytest's re-raising capture handler.

        The success branch was exposed too, and had no ``try`` over it at all:
        ``channel_id=channel.id`` on a delivery that had already *succeeded*
        raised out of ``_deliver`` and turned a delivered reply into an error
        for the caller.

        Both reads are hoisted through total helpers and resolved once. The
        exception is rendered twice on purpose, by audience:
        ``describe_exception`` for the debug buffer, which is a superuser read
        surface an adapter's credential-echoing HTTP error must not reach, and
        ``_log_detail`` for the application log and the binding column, where
        the adapter's actual complaint is the whole diagnosis. Neither can
        raise; ``f"{exc}"`` and ``str(exc)`` both can.
        """
        from app.services.server_channels.channel_inbound_service import (
            _debug_channel_key,
            _log_detail,
        )

        # Imported inside the function, not at module scope:
        # ``channel_inbound_service`` imports *this* module at import time, so
        # the reverse edge would be circular. Resolved here rather than in the
        # ``except`` for the same reason the reads are hoisted — an
        # ``ImportError`` raised inside the handler would destroy the exception
        # just as surely as an attribute reload.
        debug_channel_id = _debug_channel_key(channel)
        thread_key = _binding_thread_key(binding, channel)
        if thread_key is None:
            # Nothing to address the message to. Sending anyway would post to a
            # null thread; the warning is already logged by the helper.
            return False
        # Hoisted with the rest, and for the same reason: this is a lazy reload
        # on an expired instance, and it is about to be read inside a `try`
        # whose `except` may not raise anything of its own.
        notice_id = (
            _binding_status_message_id(binding) if into_status_notice else None
        )
        # Whether the notice was actually taken over. Bound before the ``try``
        # so the failure path below can read it without a NameError, and only
        # ever set from the adapter's own report.
        replaced = False
        try:
            adapter = get_adapter(channel.channel_type)
            if notice_id:
                outcome = await adapter.replace_message(
                    channel, thread_key, notice_id, text
                )
                # A frozen-dataclass attribute, so this read cannot raise
                # (§11a Rule 2) — unlike everything else being hoisted here.
                replaced = outcome.replaced
            else:
                await adapter.send_message(channel, thread_key, text)
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            failure = routing_trace.describe_exception(exc)
            detail = _log_detail(exc)
            logger.warning(
                "%s Delivery failed for channel=%s thread=%s: %s",
                _LOG_PREFIX,
                # The hoisted values: this argument list is evaluated eagerly
                # too, so an inline ``channel.id`` here would destroy the
                # original exception just as surely as the one below. And
                # ``detail``, not ``exc``: ``logging`` interpolates lazily and
                # swallows its own formatting errors in production while
                # pytest's ``LogCaptureHandler`` re-raises them, so a raw
                # ``exc`` here is a guard whose correctness depends on which
                # handler is installed.
                debug_channel_id or "unknown",
                thread_key,
                detail,
            )
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="outbound",
                    kind=DEBUG_SEND_FAILED,
                    summary=f"Delivery failed: {failure}",
                    thread_key=thread_key,
                    text=text,
                )
            ChannelOutboundService._record_error(db, binding, detail)
            return False
        if debug_channel_id is not None:
            ChannelDebugBuffer.record(
                channel_id=debug_channel_id,
                direction="outbound",
                kind=DEBUG_REPLIED,
                summary=(
                    "Agent reply delivered into the status notice"
                    if replaced
                    # The notice could not be patched, so the reply was posted
                    # under it instead. Said out loud because the debug feed is
                    # where an operator diagnoses a channel whose every patch
                    # is failing, and the old wording asserted the opposite.
                    else "Agent reply delivered (status notice patch failed)"
                    if notice_id
                    else "Agent reply delivered"
                ),
                thread_key=thread_key,
                text=text,
            )
        if notice_id and replaced:
            # See the docstring: the slot now holds the answer, so nothing may
            # rewrite it again. Deliberately NOT released when the adapter fell
            # back to a plain post — that notice is still standing, and keeping
            # the id is what lets the next turn rewrite it instead of stranding
            # a new orphan every turn.
            ChannelOutboundService._persist_status_message_id(db, binding, None)
        return True

    @staticmethod
    def _record_error(
        db: DBSession, binding: ChannelThreadBinding, error: str
    ) -> None:
        """Record a delivery failure — but never over a diagnosis. Never raises.

        A binding that already failed carries WHY it failed, which is far more
        useful than "and we also couldn't tell them about it". The delivery
        failure is still logged by the caller.

        **It could raise, and it is called from inside an ``except``** — so a
        raise here replaced the delivery exception exactly like an unguarded
        argument expression would, and hoisting ``_deliver``'s arguments alone
        would not have stopped it. Two paths, both confirmed by firing:

        1. ``binding.status`` and ``binding.last_error`` were read *above* the
           ``try``. They are the same expired-instance lazy reload
           :func:`_binding_thread_key` exists for, and this call site is
           reached only after a delivery has already failed — which is
           precisely when a concurrently-torn-down binding is plausible.
        2. ``db.rollback()`` sat unguarded inside the handler. A session
           rolled back into an unusable state raises again from the very call
           meant to clean it up.

        **What this does not fix, and must not be read as fixing:** when the
        ``commit`` genuinely fails, the rollback discards ``last_error`` and
        the binding row keeps no record of the delivery failure. Guarding the
        write makes the failure *reportable*; it cannot make it *durable*.
        Durability needs the persistent outbound queue named in the module
        docstring, which is a listed future enhancement — until then the
        application log is the only surviving copy, which is why the caller
        logs before it calls this.
        """
        from app.models import CHANNEL_BINDING_FAILED

        try:
            if binding.status == CHANNEL_BINDING_FAILED and binding.last_error:
                return
            binding.last_error = error[:2000]
            db.add(binding)
            db.commit()
        except Exception:
            logger.exception("%s Could not record delivery error", _LOG_PREFIX)
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "%s Rollback after a failed error-record also failed",
                    _LOG_PREFIX,
                )


__all__ = ["ChannelOutboundService", "CHANNEL_INTEGRATION_PREFIX"]
