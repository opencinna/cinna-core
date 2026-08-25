"""App MCP channel — the first *authenticated* transport.

Trust chain, stated as plainly as the other two, because all three now feed
the same policy object and they are **not the same tier**:

    Google Chat  sender_email comes out of a Google-signed JWT.
    Email        sender_email comes out of the ``From:`` header — spoofable.
    App MCP      there is no sender_email. The caller holds a bearer token
                 this platform minted for a specific ``user_id``, verified
                 against ``app_mcp_token`` on every request.

That last line is why this adapter is almost empty. The other two transports
exist to turn an outside identity into a platform user; App MCP starts with
one. There is nothing to whitelist, nothing to auto-register, nothing to
sign-check, and no outbound credential — the answer to an MCP call is the
response to that same call.

So what is the ``ServerChannel`` row for? Everything *above* the transport,
which is the half App MCP never had: a server-wide kill switch, ``visibility``
plus a grant allowlist, per-user enablement, and an agent scope. Those are
enforced where App MCP is actually entered — ``app.mcp.app_token_verifier``
reads this channel on every token verification — not here.

**Singleton.** ``/mcp/app/mcp`` is one endpoint per deployment, so
``is_singleton=True`` and ``ServerChannelService`` refuses a second row. Two
rows would be two policies over one door with nothing to say which wins.

**No config, no secrets, no whitelist.** ``validate_config`` rejects anything
non-empty rather than ignoring it: a value nothing reads is worse than a
refusal, because it looks configured. ``email_whitelist`` stays NULL and is
inert — the whitelist check lives in the inbound pipeline's post-verification
step, which this transport never enters — but a NULL whitelist *rendered in the
admin form* reads as "this channel denies everyone", which is why the admin UI
hides the field for a transport declaring this shape rather than merely
ignoring it.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.core.config import settings
from app.models import ServerChannel
from app.services.server_channels.adapters.base import (
    AuthenticatedChannelTransport,
    ChannelCapabilities,
    ChannelConfigError,
)


class AppMCPChannelAdapter(AuthenticatedChannelTransport):
    """Policy front for the app-level MCP server. No transport of its own."""

    channel_type: ClassVar[str] = "app_mcp"
    display_name: ClassVar[str] = "App MCP Server"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            # No out-of-band progress notices. The MCP call is synchronous from
            # the caller's point of view; there is no second channel to push
            # "working on it" down, and the pipeline's progress hook is only
            # consulted for transports that entered through it anyway.
            supports_progress_updates=False,
            # Nothing to edit: a message that has already been returned as a
            # tool result is the caller's, not ours.
            supports_message_edit=False,
            # MCP clients render tool results as markdown, and agents answer in
            # it. This is the one capability App MCP has that email does not.
            supports_markdown=True,
            # No protocol-level per-message cap worth declaring.
            max_message_chars=None,
            # True, and it is the reason every other outbound field is False:
            # the reply *is* the response to the caller's own request. Note this
            # is the declaration only — the pipeline field is consulted by the
            # webhook path, which this transport does not use.
            supports_sync_reply=True,
            # The whole point of this adapter. See ChannelInboundMode.
            inbound_mode="authenticated",
            # No webhook, so no token: minting one would advertise a door that
            # can only ever 404. (The registry refuses to boot on the opposite
            # combination.)
            needs_webhook_token=False,
            # Nothing is stored in ``encrypted_secrets`` — see
            # ``has_outbound_credentials`` below for what that means for the
            # admin projection.
            needs_outbound_credentials=False,
            # One endpoint per deployment.
            is_singleton=True,
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> None:
        """This transport takes no configuration. Anything supplied is refused.

        Ignoring it would be the friendlier-looking choice and the worse one:
        the value would persist, round-trip through the admin form, and read
        for the rest of its life as a setting that does something.
        """
        if config:
            raise ChannelConfigError(
                "The App MCP channel takes no configuration — its endpoint is "
                "fixed and its callers authenticate with a platform token. "
                f"Unexpected key(s): {', '.join(sorted(map(str, config)))}."
            )

    def has_outbound_credentials(self, channel: ServerChannel) -> bool:
        """Always True: there is no outbound credential, and none is missing.

        The override is mandatory (the registry refuses to import an adapter
        that declares ``needs_outbound_credentials=False`` and inherits the
        default), and the *value* is the decision this method exists to make.

        ``False`` would be defensible as "nothing is stored", and it would be
        wrong in the only place the field is read: the admin Channels list
        renders a falsy value as a permanent amber "No credential" badge whose
        tooltip tells the admin to go and add one. There is nothing to add. A
        warning that can never be cleared is not a warning, and it trains an
        admin to ignore the same badge on the Google Chat channel next to it,
        where it means the replies really are not being delivered.

        ``True`` reads as "nothing this channel needs in order to answer is
        missing", which is exactly and permanently true: the answer rides the
        synchronous MCP response. Session-free and cannot raise, as the base
        contract requires — it is called per row inside the admin list's
        projection.
        """
        return True

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def get_setup_instructions(
        self, channel: ServerChannel, webhook_url: str | None
    ) -> tuple[dict[str, str], list[str]]:
        """Admin panel. ``webhook_url`` is always ``None`` here.

        The panel's job for this type is not "how to connect it" — it is
        already connected, and it existed before it was a channel. It is to say
        what the row now controls, and that switching it off is a real kill
        switch with a bounded delay.
        """
        base = (settings.MCP_SERVER_BASE_URL or "").rstrip("/")
        details = {
            "Connection type": "Authenticated platform callers (no inbound URL)",
            "Endpoint": f"{base}/app/mcp" if base else "(MCP base URL not set)",
            "Sender verification": (
                "Platform bearer token, minted by this server's own OAuth "
                "flow and bound to one user account. Stronger than either of "
                "the other transports — there is no external address to "
                "whitelist and nothing to spoof."
            ),
            "Revocation delay": (
                "Disabling this channel, or withdrawing a grant, takes effect "
                f"within {settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS}s — "
                "availability is cached per user to keep it off every MCP "
                "request."
            ),
        }
        steps = [
            "This channel is always reachable at the endpoint above; there is "
            "nothing to register with a provider and no URL to paste anywhere.",
            "Users connect their own MCP client to it from Settings → "
            "Channels, authorising with their platform account.",
            "Switch this channel off to close the App MCP server for everyone, "
            "or set it to restricted and grant it to named users.",
            "Per-user agent scope works exactly as it does on the other "
            "channels: each person chooses, in Settings → Channels, which of "
            "their agents may be reached here.",
        ]
        return details, steps


__all__ = ["AppMCPChannelAdapter"]
