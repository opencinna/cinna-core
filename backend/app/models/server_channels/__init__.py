"""Public surface of the server-channels models package.

``app.models`` re-exports everything below as well, which is how the rest of
the codebase imports these. This package-level surface exists so that a module
working inside the feature can import from its own package without going
through the 1300-line top-level ``__init__``.
"""
from .channel_thread_binding import (
    CHANNEL_BINDING_ACTIVE,
    CHANNEL_BINDING_FAILED,
    CHANNEL_BINDING_PENDING_INSTALL,
    CHANNEL_BINDING_STATUSES,
    ChannelThreadBinding,
)
from .channel_user_setting import (
    ChannelUserAgent,
    ChannelUserSetting,
    UserChannelPublic,
    UserChannelUpdate,
)
from .server_auto_install_bundle import (
    AutoInstallBundleAdd,
    AutoInstallBundlePublic,
    ServerAutoInstallBundle,
)
from .server_channel import (
    CHANNEL_AGENT_SCOPE_ALL,
    CHANNEL_AGENT_SCOPE_LIST,
    CHANNEL_AGENT_SCOPE_NONE,
    CHANNEL_AGENT_SCOPES,
    CHANNEL_VISIBILITIES,
    CHANNEL_VISIBILITY_PUBLIC,
    CHANNEL_VISIBILITY_RESTRICTED,
    ChannelDebugEventPublic,
    ChannelDebugEventsPublic,
    ChannelRecentSender,
    ChannelSetupInstructions,
    ChannelTestOutboundRequest,
    ChannelTestOutboundResult,
    ChannelTypePublic,
    ServerChannel,
    ServerChannelBase,
    ServerChannelCreate,
    ServerChannelPublic,
    ServerChannelUpdate,
)
from .server_channel_user_grant import (
    ChannelGrantPublic,
    ChannelGrantsUpdate,
    ServerChannelUserGrant,
)

__all__ = [
    # Channel
    "ServerChannel",
    "ServerChannelBase",
    "ServerChannelCreate",
    "ServerChannelUpdate",
    "ServerChannelPublic",
    "ChannelSetupInstructions",
    "ChannelTypePublic",
    "ChannelDebugEventPublic",
    "ChannelDebugEventsPublic",
    "ChannelRecentSender",
    "ChannelTestOutboundRequest",
    "ChannelTestOutboundResult",
    "CHANNEL_VISIBILITY_PUBLIC",
    "CHANNEL_VISIBILITY_RESTRICTED",
    "CHANNEL_VISIBILITIES",
    "CHANNEL_AGENT_SCOPE_ALL",
    "CHANNEL_AGENT_SCOPE_LIST",
    "CHANNEL_AGENT_SCOPE_NONE",
    "CHANNEL_AGENT_SCOPES",
    # Auto-install list
    "ServerAutoInstallBundle",
    "AutoInstallBundleAdd",
    "AutoInstallBundlePublic",
    # Thread bindings
    "ChannelThreadBinding",
    "CHANNEL_BINDING_PENDING_INSTALL",
    "CHANNEL_BINDING_ACTIVE",
    "CHANNEL_BINDING_FAILED",
    "CHANNEL_BINDING_STATUSES",
    # Grants
    "ServerChannelUserGrant",
    "ChannelGrantPublic",
    "ChannelGrantsUpdate",
    # Per-user settings
    "ChannelUserSetting",
    "ChannelUserAgent",
    "UserChannelPublic",
    "UserChannelUpdate",
]
