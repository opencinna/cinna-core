"""The poll driver — the inbound door for transports nobody pushes to.

A webhook channel is driven by the outside world: a request arrives, the route
resolves it and calls ``ChannelInboundService.handle_inbound``. A **polled**
channel has no such driver, so this is it. One tick asks every enabled polled
channel what arrived and feeds each message into the pipeline at
``process_inbound`` — the post-verification entry point, because a polled
transport already did its own authenticating inside ``poll`` (see
``PolledChannelTransport.poll``, which restates that promise and says how
strong it is for its transport).

Split from the scheduler on purpose: ``poll_enabled_channels(db)`` takes its
session and is directly callable, exactly like
``ChannelInboundService.flush_pending_bindings``. The APScheduler wrapper in
``channel_poll_scheduler`` adds nothing but a timer and a session — which is
what lets tests drive a poll without racing a background thread, and is why
the scheduler can stay ``TESTING``-gated with nothing lost.
"""
from __future__ import annotations

import logging

from sqlmodel import Session as DBSession, select

from app.models import ServerChannel
from app.services.server_channels.adapters.base import PolledChannelTransport
from app.services.server_channels.adapters.registry import (
    channel_types_with_inbound_mode,
    get_adapter,
)
from app.services.server_channels.channel_inbound_service import ChannelInboundService

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelPoll]"


class ChannelPollService:
    """Drives every enabled polled channel through one fetch-and-process pass."""

    @staticmethod
    async def poll_enabled_channels(db: DBSession) -> int:
        """Poll every enabled polled channel once. Returns messages processed.

        Enumeration comes from the registry
        (``channel_types_with_inbound_mode("polled")``) rather than a hardcoded
        type list, so registering a second polled transport is still one line
        in the registry and nothing here.

        **Disabled channels are never polled**, which is the same rule the
        webhook route enforces by resolving enabled rows only. A channel an
        admin switched off must stop receiving, not merely stop replying.

        Failure isolation is per channel and then per message: a mail server
        that is down, or one message the pipeline chokes on, must not stop the
        other channels or the rest of the batch. Both are logged with the
        channel id, which is the only handle an operator has for a transport
        with no request to inspect.
        """
        polled_types = channel_types_with_inbound_mode("polled")
        if not polled_types:
            return 0

        channels = db.exec(
            select(ServerChannel).where(
                ServerChannel.enabled.is_(True),
                ServerChannel.channel_type.in_(polled_types),
            )
        ).all()

        processed = 0
        for channel in channels:
            adapter = get_adapter(channel.channel_type)
            if not isinstance(adapter, PolledChannelTransport):
                # Unreachable: the registry refuses to import when a declared
                # mode and its base class disagree. Kept as a guard rather than
                # a cast because the alternative failure is an ``AttributeError``
                # from a background job, which surfaces as "the channel receives
                # nothing" with no other clue.
                logger.error(
                    "%s Channel type %r declares polled but has no poll()",
                    _LOG_PREFIX,
                    channel.channel_type,
                )
                continue

            try:
                messages = await adapter.poll(channel)
            except Exception:
                logger.exception(
                    "%s Poll failed for channel %s", _LOG_PREFIX, channel.id
                )
                continue

            for inbound in messages:
                try:
                    await ChannelInboundService.process_inbound(
                        db=db, channel=channel, adapter=adapter, inbound=inbound
                    )
                    processed += 1
                except Exception:
                    # The returned body is inert for a polled transport
                    # (``build_sync_response`` is ``{}``), so there is nothing
                    # to do with a success either — only the failure is worth
                    # a line, and the debug feed carries the per-message
                    # detail.
                    logger.exception(
                        "%s Processing failed for a message on channel %s",
                        _LOG_PREFIX,
                        channel.id,
                    )
                    db.rollback()

        return processed


__all__ = ["ChannelPollService"]
