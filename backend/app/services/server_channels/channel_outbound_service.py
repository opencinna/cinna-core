"""Outbound delivery: agent replies and progress notices back to the channel.

Two entry points:

* Event subscribers (``handle_stream_completed`` / ``handle_stream_error``)
  registered in ``app/main.py`` next to the email integration's. They fire for
  every stream on the instance, so the first thing each does is a cheap
  ``integration_type.startswith("channel_")`` gate — everything else is only
  reached for sessions this feature owns.
* ``notify_progress``, called by the inbound pipeline to narrate the slow
  parts (routing, installing, ready, failed). Silently does nothing when the
  transport can't do progress updates, so callers never branch on it.

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
from app.services.server_channels.adapters.base import ChannelError
from app.services.server_channels.adapters.registry import get_adapter
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_REPLIED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelOutbound]"


def _binding_thread_key(binding: ChannelThreadBinding) -> str | None:
    """``binding``'s thread key, or ``None``. **Total by construction.**

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
    """
    try:
        return str(binding.thread_key)
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read a thread key from the binding (instance expired "
            "and its row is gone?)",
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
                    return

                await ChannelOutboundService._deliver(
                    db=db, channel=channel, binding=binding, text=text
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

                await ChannelOutboundService._deliver(
                    db=db,
                    channel=channel,
                    binding=binding,
                    text=(
                        "Something went wrong while I was working on that. "
                        "Please try again."
                    ),
                )
        except Exception:
            logger.exception("%s handle_stream_error failed", _LOG_PREFIX)

    # ------------------------------------------------------------------
    # Progress notices
    # ------------------------------------------------------------------

    @staticmethod
    async def notify_progress(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
    ) -> None:
        """Send an out-of-band progress notice, if the transport supports it.

        No-op (not an error) when the adapter can't do progress updates, so
        the pipeline can call this unconditionally.
        """
        try:
            adapter = get_adapter(channel.channel_type)
        except ChannelError:
            return
        if not adapter.capabilities.supports_progress_updates:
            return
        await ChannelOutboundService._deliver(
            db=db, channel=channel, binding=binding, text=text
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
    ) -> bool:
        """Send through the adapter, recording failure on the binding.

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
        thread_key = _binding_thread_key(binding)
        if thread_key is None:
            # Nothing to address the message to. Sending anyway would post to a
            # null thread; the warning is already logged by the helper.
            return False
        try:
            adapter = get_adapter(channel.channel_type)
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
                summary="Agent reply delivered",
                thread_key=thread_key,
                text=text,
            )
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
