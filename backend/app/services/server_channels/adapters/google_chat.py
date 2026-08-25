"""Google Chat channel adapter.

Trust chain, stated plainly because everything downstream rests on it: the
bearer JWT on the webhook proves *Google Chat* sent the event — issuer
``chat@system.gserviceaccount.com``, RS256 against Google's published JWKS,
audience pinned to this channel's GCP project number. The sender email inside
the payload is then trusted *transitively from Google*, the same tier the
email integration extends to IMAP. Verification is the first thing that
happens and it fails closed.

Outbound uses the Chat REST API with a ``chat.bot`` access token minted from
the channel's service-account JSON via the JWT-bearer grant. That is done with
PyJWT (already a dependency) rather than pulling in ``google-auth`` for one
signed assertion. Tokens are cached per channel until shortly before expiry.

Every outbound call passes the shared egress guard.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, ClassVar

import httpx
import jwt as pyjwt
from fastapi import Request

from app.core.security import (
    GoogleCertsUnavailable,
    decrypt_field,
    verify_google_signed_jwt,
)
from app.models import ServerChannel
from app.services.common.egress_guard import assert_url_allowed
from app.services.server_channels.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    ChannelConfigError,
    ChannelInboundMessage,
    ChannelSendError,
    ChannelVerificationError,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[GoogleChat]"

# Chat signs interaction events with this service account. Distinct issuer AND
# distinct key set from the accounts.google.com set used for user ID tokens —
# the JWKS URL must be the /jwk/ form; the /metadata/x509/ sibling serves a
# {kid: PEM} map that Authlib cannot decode.
_CHAT_ISSUER = "chat@system.gserviceaccount.com"
_CHAT_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/"
    "chat@system.gserviceaccount.com"
)
_CHAT_API_BASE = "https://chat.googleapis.com/v1"
_CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Chat's hard per-message limit is 4096 characters.
_MAX_MESSAGE_CHARS = 4096

# Outbound retry policy: 3 attempts, short exponential backoff. Best-effort by
# design — a persistent queue is a listed future enhancement.
_SEND_ATTEMPTS = 3
_SEND_BACKOFF_SECONDS = (0.5, 1.5)

# Per-process access-token cache: channel_id -> (token, absolute_expiry).
# Refreshed early by the skew so a token never expires mid-flight.
_bot_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_SKEW_SECONDS = 120


class GoogleChatAdapter(ChannelAdapter):
    """Google Chat app (HTTPS endpoint) transport."""

    channel_type: ClassVar[str] = "google_chat"
    display_name: ClassVar[str] = "Google Chat"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_progress_updates=True,
            # spaces.messages.patch exists, but MVP posts new messages instead
            # of editing — declared False so the pipeline never calls the
            # no-op inherited update_message and silently loses an update.
            supports_message_edit=False,
            # Chat supports a small formatting subset, not full markdown.
            supports_markdown=True,
            max_message_chars=_MAX_MESSAGE_CHARS,
            supports_sync_reply=True,
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> None:
        """Require a numeric ``project_number`` — it is the JWT audience.

        Validated at create/update because a wrong value here does not fail
        loudly at configuration time, it silently rejects every inbound event
        later, which is a miserable thing to debug.
        """
        project_number = str((config or {}).get("project_number") or "").strip()
        if not project_number:
            raise ChannelConfigError(
                "Google Chat requires 'project_number' — the GCP project number "
                "of the Chat app, used as the webhook JWT audience."
            )
        if not project_number.isdigit():
            raise ChannelConfigError(
                "'project_number' must be the numeric GCP project number "
                "(not the project ID)."
            )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def verify_inbound(
        self, request: Request, channel: ServerChannel, body: bytes
    ) -> ChannelInboundMessage:
        """Verify the bearer JWT, then parse the event. Nothing before this."""
        token = self._extract_bearer_token(request)
        if not token:
            raise ChannelVerificationError("Missing Authorization bearer token")

        project_number = str((channel.config or {}).get("project_number") or "").strip()
        if not project_number:
            # Misconfiguration, not an attack — but still fail closed: without
            # an audience we cannot tell this channel's events from any other
            # Chat app's.
            raise ChannelVerificationError(
                f"Channel {channel.id} has no project_number configured"
            )

        try:
            claims = await verify_google_signed_jwt(
                token,
                audience=project_number,
                issuers=[_CHAT_ISSUER],
                certs_url=_CHAT_JWKS_URL,
            )
        except GoogleCertsUnavailable as exc:
            # A JWKS outage is not a forgery, but we cannot verify, so we deny.
            # Logged distinctly so ops can tell "Google is down" from "someone
            # is probing us".
            logger.error(
                "%s JWKS fetch failed for channel %s: %s", _LOG_PREFIX, channel.id, exc
            )
            raise ChannelVerificationError("Unable to verify request signature") from exc

        if claims is None:
            raise ChannelVerificationError("Invalid Chat request signature")

        try:
            event = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise ChannelVerificationError("Malformed event payload") from exc
        if not isinstance(event, dict):
            raise ChannelVerificationError("Malformed event payload")

        return self._parse_event(event)

    @staticmethod
    def _extract_bearer_token(request: Request) -> str | None:
        header = request.headers.get("authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return value.strip() or None

    def _parse_event(self, event: dict[str, Any]) -> ChannelInboundMessage:
        """Normalize a verified Chat interaction event.

        Only ``MESSAGE`` from a HUMAN sender with a verified email is routable.
        Everything else authentic — the bot's own posts, membership changes,
        card clicks — is ``ignored`` and acked, never an error: a non-2xx
        response makes Chat retry the event indefinitely.
        """
        event_type = event.get("type")

        if event_type == "ADDED_TO_SPACE":
            return ChannelInboundMessage(event_kind="added_to_space", raw=event)

        if event_type != "MESSAGE":
            return ChannelInboundMessage(event_kind="ignored", raw=event)

        message = event.get("message") or {}
        sender = message.get("sender") or {}

        # Bot senders (including our own posts echoed back) must never route.
        if (sender.get("type") or "").upper() != "HUMAN":
            return ChannelInboundMessage(event_kind="ignored", raw=event)

        # `argumentText` is the text with the bot @-mention already stripped by
        # Chat; `text` is the raw form including the mention.
        text = (message.get("argumentText") or message.get("text") or "").strip()
        if not text:
            return ChannelInboundMessage(event_kind="ignored", raw=event)

        sender_email = (sender.get("email") or "").strip() or None
        # `sender_email` stays None for a consumer-Gmail (non-Workspace) sender.
        # That is deliberately NOT a verification failure: the event is
        # authentic, a non-2xx would make Chat retry it forever, and the
        # resulting 403 storm would drown the throttled audit signal for
        # genuine probing. The pipeline sees the missing address and replies
        # with the standard access-denied text instead.
        thread = message.get("thread") or {}
        thread_key = thread.get("name") or None
        if not thread_key:
            # Chat assigns a thread to every message; absence means a payload
            # shape we don't understand, and the binding key is mandatory.
            return ChannelInboundMessage(event_kind="ignored", raw=event)

        return ChannelInboundMessage(
            event_kind="message",
            sender_email=sender_email,
            sender_display_name=(sender.get("displayName") or "").strip() or None,
            external_user_id=sender.get("name") or sender_email,
            thread_key=thread_key,
            text=text,
            external_message_id=message.get("name") or None,
            raw=event,
        )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_message(
        self, channel: ServerChannel, thread_key: str, text: str
    ) -> str | None:
        """Post ``text`` into ``thread_key``, chunking and retrying as needed.

        Returns the platform id of the LAST chunk sent.
        """
        if not text:
            return None

        space = self._space_from_thread_key(thread_key)
        if not space:
            raise ChannelSendError(f"Cannot derive space from thread_key {thread_key!r}")

        credentials = self._load_credentials(channel)
        access_token = await self._mint_access_token(channel, credentials)

        url = assert_url_allowed(f"{_CHAT_API_BASE}/{space}/messages")
        last_id: str | None = None

        async with httpx.AsyncClient(timeout=30.0) as client:
            for chunk in self._chunk(text):
                payload: dict[str, Any] = {
                    "text": chunk,
                    "thread": {"name": thread_key},
                }
                created = await self._post_with_retries(
                    client=client,
                    url=url,
                    payload=payload,
                    access_token=access_token,
                    channel=channel,
                )
                last_id = created.get("name") or last_id

        return last_id

    async def _post_with_retries(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        access_token: str,
        channel: ServerChannel,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_SEND_ATTEMPTS):
            try:
                response = await client.post(
                    url,
                    params={
                        "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
                    },
                    json=payload,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return response.json() or {}
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                # 4xx other than 429 will not become correct by retrying.
                if status != 429 and 400 <= status < 500:
                    break
            except httpx.HTTPError as exc:
                last_exc = exc

            if attempt < len(_SEND_BACKOFF_SECONDS):
                await self._sleep(_SEND_BACKOFF_SECONDS[attempt])

        raise ChannelSendError(
            f"Chat send failed for channel {channel.id} after "
            f"{_SEND_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def _chunk(self, text: str) -> list[str]:
        """Split at the message limit, preferring a newline boundary."""
        limit = _MAX_MESSAGE_CHARS
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            window = remaining[:limit]
            split_at = window.rfind("\n")
            # Only honour a newline if it isn't pathologically early, otherwise
            # a long unbroken line would produce a stream of tiny chunks.
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at].rstrip("\n"))
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _space_from_thread_key(thread_key: str) -> str | None:
        """``spaces/AAA/threads/BBB`` -> ``spaces/AAA``."""
        parts = (thread_key or "").split("/")
        if len(parts) >= 2 and parts[0] == "spaces" and parts[1]:
            return f"spaces/{parts[1]}"
        return None

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    @staticmethod
    def _load_credentials(channel: ServerChannel) -> dict[str, Any]:
        """Decrypt the stored service-account JSON.

        Never logged, never returned — the only consumer is the token mint.
        """
        if not channel.encrypted_secrets:
            raise ChannelSendError(
                f"Channel {channel.id} has no outbound credentials configured"
            )
        try:
            data = json.loads(decrypt_field(channel.encrypted_secrets))
        except Exception as exc:  # noqa: BLE001 — corrupt blob is unrecoverable
            raise ChannelSendError(
                f"Channel {channel.id} outbound credentials could not be decrypted"
            ) from exc
        if not isinstance(data, dict):
            raise ChannelSendError(
                f"Channel {channel.id} outbound credentials are not a JSON object"
            )
        return data

    async def _mint_access_token(
        self, channel: ServerChannel, credentials: dict[str, Any]
    ) -> str:
        """Mint (or reuse) a ``chat.bot`` access token via the JWT-bearer grant."""
        cache_key = str(channel.id)
        now = time.time()
        cached = _bot_token_cache.get(cache_key)
        if cached is not None and cached[1] - _TOKEN_SKEW_SECONDS > now:
            return cached[0]

        client_email = credentials.get("client_email")
        private_key = credentials.get("private_key")
        if not client_email or not private_key:
            raise ChannelSendError(
                f"Channel {channel.id} service account JSON is missing "
                "client_email/private_key"
            )
        token_uri = credentials.get("token_uri") or _DEFAULT_TOKEN_URI

        issued = int(now)
        assertion = pyjwt.encode(
            {
                "iss": client_email,
                "scope": _CHAT_BOT_SCOPE,
                "aud": token_uri,
                "iat": issued,
                "exp": issued + 3600,
            },
            private_key,
            algorithm="RS256",
            headers=(
                {"kid": credentials["private_key_id"]}
                if credentials.get("private_key_id")
                else None
            ),
        )

        url = assert_url_allowed(token_uri)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            response.raise_for_status()
            data = response.json()

        access_token = data.get("access_token")
        if not access_token:
            raise ChannelSendError("Google token endpoint returned no access_token")
        expires_in = int(data.get("expires_in") or 3600)
        _bot_token_cache[cache_key] = (access_token, now + expires_in)
        return access_token

    @staticmethod
    def invalidate_token_cache(channel_id: Any) -> None:
        """Drop a cached token — called when a channel's secrets are rotated."""
        _bot_token_cache.pop(str(channel_id), None)

    # ------------------------------------------------------------------
    # Setup / sync response
    # ------------------------------------------------------------------

    def get_setup_instructions(
        self, channel: ServerChannel, webhook_url: str | None
    ) -> tuple[dict[str, str], list[str]]:
        """Return ``(details, steps)`` for the admin setup panel.

        ``details`` deliberately does NOT repeat ``webhook_url``: it already
        travels in ``ChannelSetupInstructions.webhook_url``, its own field, and
        the same value in a dedicated field and a free-form map means every
        consumer has to filter one of them out. ``webhook_url`` stays a
        parameter because the contract offers it to adapters that want to build
        a copyable example from it.
        """
        project_number = str((channel.config or {}).get("project_number") or "")
        details = {
            "Audience (GCP project number)": project_number or "(not set)",
            "Bot scope": _CHAT_BOT_SCOPE,
            "Connection type": "HTTPS endpoint",
        }
        steps = [
            "In Google Cloud Console, enable the Google Chat API for your project.",
            "Open Chat API > Configuration and give the app a name, avatar and "
            "description.",
            "Under Functionality, enable 'Receive 1:1 messages' and "
            "'Join spaces and group conversations'.",
            "Under Connection settings choose 'HTTPS endpoint URL' and paste the "
            "webhook URL above.",
            "Under Visibility, make the app available to the people or groups who "
            "should be able to reach your agents.",
            "Save, then create a service account with the Chat Bot role, download "
            "its JSON key, and paste it into this channel's service-account field.",
            "Use 'Test outbound' here to confirm the credential works, then message "
            "the bot from Google Chat.",
        ]
        return details, steps

    def build_sync_response(self, text: str | None) -> dict[str, Any]:
        """Chat renders a JSON ``{"text": ...}`` response as an in-thread reply."""
        if not text:
            return {}
        return {"text": text[:_MAX_MESSAGE_CHARS]}


__all__ = ["GoogleChatAdapter"]
