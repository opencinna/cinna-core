"""Adapter registry — dispatch by ``channel_type``.

A module-level dict, deliberately. Adapters are stateless singletons (all
per-channel state lives on the ``ServerChannel`` row passed into every call),
so one instance per type is correct and there is nothing to manage.

Adding a channel: write the adapter module, add one line here.

Since the transport split this module is also where "what shape is this
transport?" is answered. The answer always comes from the adapter's declared
``ChannelCapabilities`` — never from which ABC it subclasses — so a caller that
needs to branch on transport shape asks here instead of reaching through
``adapter.capabilities`` and inventing its own default.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.server_channels.adapters.app_mcp import AppMCPChannelAdapter
from app.services.server_channels.adapters.base import (
    AuthenticatedChannelTransport,
    ChannelAdapter,
    ChannelInboundMode,
    PolledChannelTransport,
    UnknownChannelTypeError,
)
from app.services.server_channels.adapters.email import EmailChannelAdapter
from app.services.server_channels.adapters.google_chat import GoogleChatAdapter

CHANNEL_ADAPTERS: dict[str, ChannelAdapter] = {
    GoogleChatAdapter.channel_type: GoogleChatAdapter(),
    EmailChannelAdapter.channel_type: EmailChannelAdapter(),
    AppMCPChannelAdapter.channel_type: AppMCPChannelAdapter(),
}


@dataclass(frozen=True)
class RegisteredTransport:
    """One registered transport, flattened to the facts callers dispatch on.

    ``ChannelCapabilities`` stays the single source of truth; this is the read
    of it that ``ServerChannelService`` and the pollers share. Flattened rather
    than carrying the capabilities object because these four are the questions
    that decide whether a channel gets a webhook token, where its outbound
    credential lives, which driver feeds it, and how many of it may exist — and
    having one place answer them is what stops each caller growing its own
    default.
    """

    channel_type: str
    adapter: ChannelAdapter
    #: Where this transport's authentication chokepoint lives.
    inbound_mode: ChannelInboundMode
    #: Whether a webhook token must be minted for channels of this type.
    needs_webhook_token: bool
    #: Whether the outbound credential lives in ``encrypted_secrets``.
    needs_outbound_credentials: bool
    #: Whether at most one channel row of this type may exist.
    is_singleton: bool


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


def get_transport(channel_type: str) -> RegisteredTransport:
    """Return the adapter for ``channel_type`` together with its transport shape.

    Same "raise rather than return None" contract as :func:`get_adapter`, and
    for the same reason: a caller asking what shape a channel's transport is
    cannot proceed on "no answer".
    """
    adapter = get_adapter(channel_type)
    capabilities = adapter.capabilities
    return RegisteredTransport(
        channel_type=channel_type,
        adapter=adapter,
        inbound_mode=capabilities.inbound_mode,
        needs_webhook_token=capabilities.needs_webhook_token,
        needs_outbound_credentials=capabilities.needs_outbound_credentials,
        is_singleton=capabilities.is_singleton,
    )


def available_channel_types() -> list[str]:
    """Registered channel types, for admin UI and create-time validation."""
    return sorted(CHANNEL_ADAPTERS)


def channel_types_with_inbound_mode(mode: ChannelInboundMode) -> list[str]:
    """Registered channel types whose transport declares ``mode``.

    The enumeration seam a driver needs to find its own channels — a poller
    asks for ``"polled"`` and then selects the enabled rows of those types.
    Returns an empty list when nothing is registered for the mode, which is a
    real answer ("this build has no polled transport") rather than the missing
    answer :func:`get_adapter` refuses to give.
    """
    return sorted(
        channel_type
        for channel_type, adapter in CHANNEL_ADAPTERS.items()
        if adapter.capabilities.inbound_mode == mode
    )


def singleton_channel_types() -> list[str]:
    """Registered channel types that may only ever have one row.

    The enumeration seam for "materialize the rows this build owns" — the
    counterpart to :func:`channel_types_with_inbound_mode` for a driver finding
    its own channels. Returns an empty list when this build declares no
    singleton, which is a real answer rather than a missing one.
    """
    return sorted(
        channel_type
        for channel_type, adapter in CHANNEL_ADAPTERS.items()
        if adapter.capabilities.is_singleton
    )


def _assert_declared_modes_agree() -> None:
    """Fail at import if an adapter's base class and its declaration disagree.

    This is *not* the isinstance-based inference the split exists to avoid —
    dispatch reads ``capabilities.inbound_mode`` everywhere, and this check
    never chooses a behaviour. It only refuses to boot on a self-contradiction,
    which is the one thing the class can legitimately be read for.

    Both directions are wrong, and both are checked:

    * subclass without the declaration — registered as a webhook channel, so
      the poller never picks it up while its inherited ``verify_inbound``
      refuses everything that arrives: a channel that silently receives
      nothing.
    * declaration without the subclass — the worse one, because everything
      *looks* live. ``verify_inbound`` is abstract, so a plain ``ChannelAdapter``
      has a real, working one; the channel gets a minted token and a webhook
      that genuinely accepts traffic, while the admin is told it has none and
      the poller crashes with ``AttributeError`` reaching for ``poll()``.

    Both non-webhook modes are checked the same way and for the same reason.
    An ``authenticated`` transport has no ``poll()`` for the second failure to
    crash on, which makes it *quieter*, not safer: without the subclass it
    keeps a live, working ``verify_inbound`` while its whole premise is that no
    unauthenticated request can reach it.

    The token rule rides along for the same reason: a transport that declares
    a non-``webhook`` mode and ``needs_webhook_token=True`` has asked for a
    token on a door it says it does not have. So does the outbound-credential
    rule: a transport that declares its credential lives outside
    ``encrypted_secrets`` and then inherits the default
    ``has_outbound_credentials`` has said where its credential is *not* without
    ever saying where it is, and the admin projection answers "none configured"
    for every channel of that type.

    Cheap to catch at import; miserable to diagnose in production.
    """
    for channel_type, adapter in CHANNEL_ADAPTERS.items():
        capabilities = adapter.capabilities
        mode = capabilities.inbound_mode
        for base, declared_mode in (
            (PolledChannelTransport, "polled"),
            (AuthenticatedChannelTransport, "authenticated"),
        ):
            if isinstance(adapter, base) != (mode == declared_mode):
                raise RuntimeError(
                    f"Channel adapter {channel_type!r} declares "
                    f"inbound_mode={mode!r} but "
                    f"{'subclasses' if mode != declared_mode else 'does not subclass'} "
                    f"{base.__name__}. The two must agree: the declaration is "
                    "what the registry, the channel service and the drivers "
                    "dispatch on, and the base class is what supplies (or "
                    "refuses) the matching methods."
                )
        if not capabilities.needs_outbound_credentials and (
            type(adapter).has_outbound_credentials
            is ChannelAdapter.has_outbound_credentials
        ):
            raise RuntimeError(
                f"Channel adapter {channel_type!r} declares "
                "needs_outbound_credentials=False but does not override "
                "has_outbound_credentials(). The declaration says the outbound "
                "credential lives somewhere other than encrypted_secrets; the "
                "method is what says where. Inheriting the default makes the "
                "admin projection report a confident 'no outbound credential' "
                "for every channel of this type, operational or not."
            )
        if mode != "webhook" and capabilities.needs_webhook_token:
            raise RuntimeError(
                f"Channel adapter {channel_type!r} declares "
                f"inbound_mode={mode!r} but needs_webhook_token=True. A "
                "transport with no webhook must not have a token minted for "
                "it: the token is dead weight and the URL it produces "
                "misleads whoever pastes it into the platform."
            )


_assert_declared_modes_agree()


__all__ = [
    "CHANNEL_ADAPTERS",
    "RegisteredTransport",
    "get_adapter",
    "get_transport",
    "available_channel_types",
    "channel_types_with_inbound_mode",
    "singleton_channel_types",
]
