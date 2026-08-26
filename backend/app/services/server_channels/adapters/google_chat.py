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

Three verbs are used: ``create`` for replies and notices, ``patch`` to rewrite
a progress notice in place — including the last rewrite, which puts the agent's
own answer into the notice's slot — and ``delete`` for the rare turn that ends
with nothing to say. The last two are app-auth-restricted to messages this app
posted, which is all the pipeline ever asks of them.

``delete`` is deliberately off the common path: Chat renders a deleted message
as a "Message deleted by its author" tombstone, so clearing the notice once the
reply was posted beneath it put one of those above every single answer.

Everything on the way out is translated from CommonMark to Chat's own markup
(``google_chat_format``). Chat's ``text`` field is not Markdown and
``spaces.messages.create`` has no ``markupSyntax`` parameter to make it so, so
untranslated agent output reaches the reader as literal asterisks.

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
    ChannelError,
    ChannelInboundMessage,
    ChannelReplaceResult,
    ChannelSendError,
    ChannelVerificationError,
)
from app.services.server_channels.adapters.google_chat_format import markdown_to_chat

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
            # ``spaces.messages.patch`` and ``spaces.messages.delete``, both
            # with app auth and both restricted to messages this app posted —
            # which is all the pipeline ever asks of them. Together they are
            # what ``supports_status_notice`` derives from, and what lets one
            # progress notice be rewritten in place and then removed instead of
            # three of them piling up above the answer.
            supports_message_edit=True,
            supports_message_delete=True,
            # Chat renders its OWN markup, not CommonMark, and
            # ``spaces.messages.create`` has no ``markupSyntax`` parameter to
            # ask for anything else. So this is True because the adapter
            # translates on the way out (``google_chat_format``), not because
            # the transport takes markdown.
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

        ``text`` arrives as CommonMark — that is what agents write and what
        every pipeline notice is authored in — and is translated to Chat's own
        markup here, at the edge, because that translation is a fact about this
        transport and nothing above it should have to know Chat's dialect.
        Chunking runs on the *translated* text: the limit applies to what is
        actually sent.

        Returns the platform id of the LAST chunk sent.
        """
        if not text:
            return None
        return await self._post_chunks(
            channel, thread_key, self._chunk(markdown_to_chat(text))
        )

    async def replace_message(
        self,
        channel: ServerChannel,
        thread_key: str,
        external_message_id: str,
        text: str,
    ) -> ChannelReplaceResult:
        """Deliver ``text`` **into** an existing message of ours.

        The status notice's endgame: the message that has been saying "working
        on your message…" is rewritten to hold the agent's actual answer, so
        the thread shows one bot message rather than a notice, a deletion
        tombstone, and a reply.

        Chunked, unlike :meth:`update_message` — this is a delivery and the
        text is an agent's, so it can be any length. The first chunk replaces
        the named message; the rest are posted after it in the ordinary way.

        Falls back to a plain send if the patch fails, which is the case that
        matters in practice: the notice may have been deleted by hand, or its
        id may have gone stale. Losing the slot is cosmetic; losing the answer
        is not. The fallback re-sends the **untranslated** ``text``, because
        ``send_message`` translates and running ``markdown_to_chat`` over
        already-translated markup would corrupt it (Chat's ``*bold*`` reads
        back as Markdown italics).

        That fallback is why the return is a ``ChannelReplaceResult`` rather
        than an id: it turns a failure into a success *before the caller can
        see it*, and the caller's next move — release the status notice id or
        keep it — depends on which of the two happened. ``replaced=False``
        says the named message is still standing and still says whatever it
        said. See :class:`ChannelReplaceResult`.

        **Once the patch lands, ``replaced=True`` is not negotiable — even if
        the remaining chunks fail to post.** Ownership of that message
        transferred at the patch: it holds the answer now, whatever happens
        next. Letting a failed remainder raise reported the *pre-patch*
        ``replaced=False`` to ``ChannelOutboundService._deliver`` (it is bound
        before the ``try``), which kept the notice id on the binding — and 45
        seconds later the pending-flush loop patched "working on your
        message…" over a delivered reply, the exact overwrite this result type
        exists to prevent. So a remainder failure is swallowed and logged, and
        the deliberate cost is that a **truncated** reply is reported as
        delivered. That is strictly better than a complete reply being patched
        away next turn, and the warning keeps it diagnosable.
        """
        if not text:
            return ChannelReplaceResult(message_id=None, replaced=False)
        if not external_message_id:
            return ChannelReplaceResult(
                message_id=await self.send_message(channel, thread_key, text),
                replaced=False,
            )

        chunks = self._chunk(markdown_to_chat(text))
        try:
            await self._patch_text(channel, external_message_id, chunks[0])
        except ChannelError:
            logger.warning(
                "%s Could not deliver into message %s — posting instead",
                _LOG_PREFIX,
                external_message_id,
                exc_info=True,
            )
            return ChannelReplaceResult(
                message_id=await self.send_message(channel, thread_key, text),
                replaced=False,
            )

        if len(chunks) == 1:
            return ChannelReplaceResult(
                message_id=external_message_id, replaced=True
            )
        try:
            rest = await self._post_chunks(channel, thread_key, chunks[1:])
        except ChannelError:
            # The patch already landed: the notice now holds chunk 0 of the
            # answer. Letting this raise reported ``replaced=False`` to
            # ``_deliver`` — which then KEPT the notice id on the binding while
            # the message already held the reply, so the next turn's "working
            # on your message…" was patched straight over it. See the
            # docstring's last paragraph.
            logger.warning(
                "%s Delivered into message %s but could not post the "
                "remaining %d chunk(s) — the reply is truncated",
                _LOG_PREFIX,
                external_message_id,
                len(chunks) - 1,
                exc_info=True,
            )
            rest = None
        return ChannelReplaceResult(
            message_id=rest or external_message_id, replaced=True
        )

    async def update_message(
        self,
        channel: ServerChannel,
        thread_key: str,
        external_message_id: str,
        text: str,
    ) -> None:
        """Rewrite a message this app posted, in place.

        ``external_message_id`` is a message resource name
        (``spaces/AAA/messages/BBB``) as returned by :meth:`send_message`. App
        auth can only patch the app's own messages, which is the only thing the
        pipeline asks for — the status notice it posted itself.

        Oversized text is **truncated rather than chunked**: a patch addresses
        exactly one message, and silently spilling the remainder into a second
        one would leave a message the caller does not know the id of. That is
        the difference from :meth:`replace_message`, which is a delivery and
        does chunk. Progress notices are one short line, so the cap here is a
        backstop, not a path anything travels.
        """
        if not external_message_id:
            return
        await self._patch_text(
            channel,
            external_message_id,
            markdown_to_chat(text)[:_MAX_MESSAGE_CHARS],
        )

    async def _post_chunks(
        self, channel: ServerChannel, thread_key: str, chunks: list[str]
    ) -> str | None:
        """Post pre-translated, pre-chunked text. Returns the last message id.

        Takes chunks rather than text so :meth:`replace_message` can post the
        *remainder* of an already-translated body without running the markdown
        translation a second time over its own output.
        """
        space = self._space_from_thread_key(thread_key)
        if not space:
            raise ChannelSendError(f"Cannot derive space from thread_key {thread_key!r}")

        url = assert_url_allowed(f"{_CHAT_API_BASE}/{space}/messages")
        last_id: str | None = None

        async with httpx.AsyncClient(timeout=30.0) as client:
            access_token = await self._mint_access_token(
                channel, self._load_credentials(channel)
            )
            for chunk in chunks:
                payload: dict[str, Any] = {
                    "text": chunk,
                    "thread": {"name": thread_key},
                }
                created = await self._request_with_retries(
                    client=client,
                    method="POST",
                    url=url,
                    params={
                        "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
                    },
                    payload=payload,
                    access_token=access_token,
                    channel=channel,
                )
                last_id = created.get("name") or last_id

        return last_id

    @staticmethod
    def _message_url(external_message_id: str) -> str:
        """Validate a message resource name and build its API URL.

        ``spaces/AAA/messages/BBB`` is the only shape Chat's ``patch`` and
        ``delete`` address, and the only shape ``send_message`` ever returns —
        so anything else is a caller holding an id that did not come from here.
        Refusing it loudly beats appending it to the API base and issuing a
        request that can only 404, which is a network round trip spent
        discovering something the string itself already said.
        """
        parts = (external_message_id or "").split("/")
        if len(parts) < 4 or parts[0] != "spaces" or parts[2] != "messages":
            raise ChannelSendError(
                f"{external_message_id!r} is not a Chat message resource name"
            )
        return assert_url_allowed(f"{_CHAT_API_BASE}/{external_message_id}")

    async def _patch_text(
        self, channel: ServerChannel, external_message_id: str, text: str
    ) -> None:
        """``spaces.messages.patch`` with ``updateMask=text``, already translated."""
        url = self._message_url(external_message_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            access_token = await self._mint_access_token(
                channel, self._load_credentials(channel)
            )
            await self._request_with_retries(
                client=client,
                method="PATCH",
                url=url,
                params={"updateMask": "text"},
                payload={"text": text},
                access_token=access_token,
                channel=channel,
            )

    async def delete_message(
        self,
        channel: ServerChannel,
        thread_key: str,
        external_message_id: str,
    ) -> None:
        """Remove a message this app posted. Already-gone is success."""
        if not external_message_id:
            return
        url = self._message_url(external_message_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            access_token = await self._mint_access_token(
                channel, self._load_credentials(channel)
            )
            await self._request_with_retries(
                client=client,
                method="DELETE",
                url=url,
                params=None,
                payload=None,
                access_token=access_token,
                channel=channel,
                # A notice someone deleted by hand, or one this app already
                # removed on a retried tick, is the outcome the caller wanted.
                # Raising would record a delivery failure on the binding for a
                # thread that is in exactly the right state.
                tolerate_missing=True,
            )

    async def _request_with_retries(
        self,
        *,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        payload: dict[str, Any] | None,
        access_token: str,
        channel: ServerChannel,
        tolerate_missing: bool = False,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_SEND_ATTEMPTS):
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                try:
                    return response.json() or {}
                except ValueError:
                    # DELETE answers with an empty body.
                    return {}
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if tolerate_missing and status == 404:
                    return {}
                # 4xx other than 429 will not become correct by retrying.
                if status != 429 and 400 <= status < 500:
                    break
            except httpx.HTTPError as exc:
                last_exc = exc

            if attempt < len(_SEND_BACKOFF_SECONDS):
                await self._sleep(_SEND_BACKOFF_SECONDS[attempt])

        raise ChannelSendError(
            f"Chat {method} failed for channel {channel.id} after "
            f"{_SEND_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def _chunk(self, text: str) -> list[str]:
        """Split at the message limit, preferring a newline boundary.

        Code fences are closed and re-opened across the split. A ```````
        block cut in half leaves the first chunk with an unterminated fence —
        Chat renders the rest of that message as prose — and the second chunk
        opening with the block's *closing* fence, which then swallows whatever
        follows it. The reserve below is what keeps re-opening the fence from
        pushing the chunk back over the limit.
        """
        limit = _MAX_MESSAGE_CHARS
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text
        # Room for a closing "\n```" on the chunk we cut and an opening
        # "```\n" on the next. Reserved for the whole split rather than per
        # chunk, because whether a given chunk needs one fence, both, or
        # neither is only known after it has been cut — and not reserved at all
        # for text that has no fences in it, so ordinary prose splits exactly
        # where it always did.
        reserve = 8 if "```" in text else 0
        open_fence = False
        # ``limit - reserve``, not ``limit``: the loop condition decides how
        # big the FINAL chunk may be, and that chunk gets the re-opening
        # "```\n" prepended like any other. Exiting at ``limit`` let a tail of
        # exactly ``limit`` characters become ``limit + 4`` — Chat answers 400,
        # ``_request_with_retries`` gives up immediately on a non-429 4xx, and
        # the earlier chunks are already posted, so the end of a long reply
        # vanishes AND the binding records a delivery failure for an answer the
        # reader mostly received.
        while len(remaining) > limit - reserve:
            window_size = limit - reserve
            window = remaining[:window_size]
            split_at = window.rfind("\n")
            # Only honour a newline if it isn't pathologically early, otherwise
            # a long unbroken line would produce a stream of tiny chunks.
            if split_at < window_size // 2:
                split_at = window_size
            piece = remaining[:split_at].rstrip("\n")
            remaining = remaining[split_at:].lstrip("\n")

            was_open = open_fence
            open_fence = self._fence_open_after(piece, was_open)
            if was_open:
                piece = f"```\n{piece}"
            if open_fence:
                piece = f"{piece}\n```"
            chunks.append(piece)
        if remaining:
            chunks.append(f"```\n{remaining}" if open_fence else remaining)
        return chunks

    @staticmethod
    def _fence_open_after(piece: str, open_before: bool) -> bool:
        """Whether a fenced block is still open at the end of ``piece``."""
        state = open_before
        for line in piece.split("\n"):
            if line.lstrip().startswith("```"):
                state = not state
        return state

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

    def build_sync_response(
        self, text: str | None, thread_key: str | None = None
    ) -> dict[str, Any]:
        """Render the webhook's own HTTP response as a message.

        ``thread`` is not decoration. A Chat app's synchronous response with no
        ``thread`` is posted as a **new top-level message in the space** — so
        without this the pipeline's first word to a sender ("working on it", or
        a denial) appeared outside the conversation it was answering, while
        every later message, posted through :meth:`send_message` with an
        explicit ``thread.name``, appeared inside it. One reply in the room and
        the rest in a thread, from the same exchange.

        ``thread_key`` is ``None`` only for the ``added_to_space`` welcome,
        which genuinely has no thread yet and is correct as a space-level post.
        """
        if not text:
            return {}
        body: dict[str, Any] = {
            "text": markdown_to_chat(text)[:_MAX_MESSAGE_CHARS]
        }
        if thread_key:
            body["thread"] = {"name": thread_key}
        return body


__all__ = ["GoogleChatAdapter"]
