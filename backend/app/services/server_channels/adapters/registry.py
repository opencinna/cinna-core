"""Adapter registry — dispatch by ``channel_type``.

A module-level dict, deliberately. Adapters are stateless singletons (all
per-channel state lives on the ``ServerChannel`` row passed into every call),
so one instance per type is correct and there is nothing to manage.

Adding a channel: write the adapter module, add one line here.
"""
from app.services.server_channels.adapters.base import (
    ChannelAdapter,
    UnknownChannelTypeError,
)
from app.services.server_channels.adapters.google_chat import GoogleChatAdapter

CHANNEL_ADAPTERS: dict[str, ChannelAdapter] = {
    GoogleChatAdapter.channel_type: GoogleChatAdapter(),
}


def get_adapter(channel_type: str) -> ChannelAdapter:
    """Return the adapter for ``channel_type``.

    Raises ``UnknownChannelTypeError`` rather than returning None — every
    caller needs an adapter to proceed, so a missing one is an error at the
    point of lookup, not a branch the callers each have to remember.
    """
    adapter = CHANNEL_ADAPTERS.get(channel_type)
    if adapter is None:
        raise UnknownChannelTypeError(
            f"Unknown channel type {channel_type!r}. "
            f"Available: {', '.join(sorted(CHANNEL_ADAPTERS)) or 'none'}"
        )
    return adapter


def available_channel_types() -> list[str]:
    """Registered channel types, for admin UI and create-time validation."""
    return sorted(CHANNEL_ADAPTERS)


__all__ = ["CHANNEL_ADAPTERS", "get_adapter", "available_channel_types"]
