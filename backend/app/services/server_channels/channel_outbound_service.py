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
from app.services.server_channels.adapters.base import ChannelError
from app.services.server_channels.adapters.registry import get_adapter
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_REPLIED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelOutbound]"

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
        """Send through the adapter, recording failure on the binding."""
        try:
            adapter = get_adapter(channel.channel_type)
            await adapter.send_message(channel, binding.thread_key, text)
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            logger.warning(
                "%s Delivery failed for channel=%s thread=%s: %s",
                _LOG_PREFIX,
                channel.id,
                binding.thread_key,
                exc,
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="outbound",
                kind=DEBUG_SEND_FAILED,
                summary=f"Delivery failed: {exc}",
                thread_key=binding.thread_key,
                text=text,
            )
            ChannelOutboundService._record_error(db, binding, str(exc))
            return False
        ChannelDebugBuffer.record(
            channel_id=channel.id,
            direction="outbound",
            kind=DEBUG_REPLIED,
            summary="Agent reply delivered",
            thread_key=binding.thread_key,
            text=text,
        )
        return True

    @staticmethod
    def _record_error(
        db: DBSession, binding: ChannelThreadBinding, error: str
    ) -> None:
        """Record a delivery failure — but never over a diagnosis.

        A binding that already failed carries WHY it failed, which is far more
        useful than "and we also couldn't tell them about it". The delivery
        failure is still logged by the caller.
        """
        from app.models import CHANNEL_BINDING_FAILED

        if binding.status == CHANNEL_BINDING_FAILED and binding.last_error:
            return
        try:
            binding.last_error = error[:2000]
            db.add(binding)
            db.commit()
        except Exception:
            logger.exception("%s Could not record delivery error", _LOG_PREFIX)
            db.rollback()


__all__ = ["ChannelOutboundService", "CHANNEL_INTEGRATION_PREFIX"]
