"""Server channels API — one public webhook, the rest superuser-only.

The public route at the top of this file is the platform's only
unauthenticated ingress that can create an agent session. Its defences, in the
order they execute:

    rate limit → body-size cap → token resolve (404) → adapter verification (403)

Each is a distinct, visible step here rather than something the pipeline does
somewhere further down, because the cost of one of them silently moving after
`verify` is a public endpoint that parses attacker-controlled JSON.

Everything below the webhook requires ``get_current_active_superuser``. There
is no role-based partial access to channel administration: a channel holds an
outbound service-account credential and decides who may talk to agents, so it
is superuser-or-nothing.
"""
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session

from app.api.deps import SessionDep, get_current_active_superuser
from app.core.config import settings
from app.models import (
    AutoInstallBundleAdd,
    AutoInstallBundlePublic,
    ChannelDebugEventPublic,
    ChannelDebugEventsPublic,
    ChannelGrantPublic,
    ChannelGrantsUpdate,
    ChannelRecentSender,
    ChannelSetupInstructions,
    ChannelTestOutboundRequest,
    ChannelTestOutboundResult,
    ChannelTypePublic,
    Message,
    SecurityEventCreate,
    ServerChannelCreate,
    ServerChannelPublic,
    ServerChannelUpdate,
    User,
)
from app.models.events import security_event as security_event_constants
from app.services.common.rate_limiter import RateLimiter
from app.services.events.security_event_service import SecurityEventService
from app.services.server_channels.adapters.base import (
    ChannelConfigError,
    ChannelError,
    ChannelVerificationError,
    UnknownChannelTypeError,
)
from app.services.server_channels.adapters.registry import CHANNEL_ADAPTERS
from app.services.server_channels.channel_debug_buffer import (
    CAPTURING_SINCE,
    DEBUG_TEST_SEND,
    ChannelDebugBuffer,
)
from app.services.server_channels.channel_inbound_service import (
    ChannelInboundService,
    ChannelNotFound,
)
from app.services.server_channels.server_channel_service import (
    ChannelNotFoundError,
    DuplicateChannelNameError,
    InvalidChannelPolicyError,
    ServerChannelService,
)

router = APIRouter(tags=["server-channels"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]

# Keyed by webhook token so a burst against one channel cannot starve another.
# Process-local, like every other limiter here — a backstop against probing and
# runaway redelivery, not a billing control.
_webhook_rate_limiter = RateLimiter()


# ===========================================================================
# Public webhook — NO authentication dependency by design
# ===========================================================================


@router.post("/channels/{webhook_token}/inbound")
async def channel_inbound(
    *,
    session: SessionDep,
    request: Request,
    webhook_token: str,
) -> Any:
    """Receive an inbound channel event.

    Returns the adapter's sync-response body (rendered in-thread by channels
    that support it) or ``{}``. Non-2xx is reserved for the two cases where we
    genuinely want the platform to stop and retry or give up — an unknown token
    and a failed signature — because most channels retry a non-2xx forever.
    """
    # --- 1. Rate limit, before any work at all ---
    retry_after = _webhook_rate_limiter.check(
        webhook_token, settings.SERVER_CHANNEL_WEBHOOK_RATE_LIMIT_PER_MIN
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )

    # --- 2. Body-size cap, before the body is parsed by anything ---
    max_bytes = settings.SERVER_CHANNEL_WEBHOOK_MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Payload too large",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Bad request"
            )
    body = await request.body()
    # Re-check the real length: Content-Length can lie or be absent (chunked).
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Payload too large",
        )

    # --- 3 + 4. Token resolve (404) then adapter verification (403) ---
    # Both live inside the pipeline so that ordering cannot drift apart from
    # the logic that depends on it; the route only maps them to status codes.
    try:
        return await ChannelInboundService.handle_inbound(
            db=session,
            webhook_token=webhook_token,
            request=request,
            body=body,
        )
    except ChannelNotFound:
        # Unknown token AND disabled channel land here identically — no detail,
        # so the response is not an oracle for "this token exists".
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    except ChannelVerificationError:
        # Reason deliberately withheld: a specific message would let a caller
        # probe which part of the signature check it failed.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden"
        )
    except UnknownChannelTypeError:
        # A stored channel whose adapter is no longer registered. Answer
        # exactly like an unknown token — a 500 here would be an oracle for
        # "this token resolves to a real row".
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


# ===========================================================================
# Admin — literal paths FIRST so they win FastAPI matching over /{channel_id}
# ===========================================================================


@router.get("/admin/server-channels/channel-types", response_model=list[ChannelTypePublic])
def list_channel_types(current_user: SuperUser) -> Any:
    """Registered adapters, for the admin type picker."""
    return [
        ChannelTypePublic(
            channel_type=adapter.channel_type,
            display_name=adapter.display_name or adapter.channel_type,
        )
        for adapter in sorted(
            CHANNEL_ADAPTERS.values(), key=lambda a: a.display_name or a.channel_type
        )
    ]


@router.get(
    "/admin/server-channels/auto-install-list",
    response_model=list[AutoInstallBundlePublic],
)
def list_auto_install_bundles(session: SessionDep, current_user: SuperUser) -> Any:
    """The server-wide auto-install list, with routability flags."""
    return ServerChannelService.list_auto_install_bundles(session)


@router.post(
    "/admin/server-channels/auto-install-list",
    response_model=list[AutoInstallBundlePublic],
)
async def add_auto_install_bundle(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: AutoInstallBundleAdd,
) -> Any:
    """Add a bundle to the auto-install list. Idempotent.

    Returns the whole list so the admin UI re-renders from one response —
    membership changes the routability badges on nothing else, but the caller
    would otherwise have to refetch immediately anyway.
    """
    try:
        ServerChannelService.add_auto_install_bundle(
            session, data.bundle_uuid, current_user
        )
    except ChannelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ChannelError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_UPDATED,
        {"action": "auto_install_list_add", "bundle_uuid": str(data.bundle_uuid)},
    )
    return ServerChannelService.list_auto_install_bundles(session)


@router.delete(
    "/admin/server-channels/auto-install-list/{bundle_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_auto_install_bundle(
    *,
    session: SessionDep,
    current_user: SuperUser,
    bundle_uuid: uuid.UUID,
) -> Response:
    """Remove a bundle from the auto-install list.

    Existing installs and bindings are untouched — only future Pass 2 routing
    is affected.
    """
    ServerChannelService.remove_auto_install_bundle(session, bundle_uuid)
    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_UPDATED,
        {"action": "auto_install_list_remove", "bundle_uuid": str(bundle_uuid)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Admin — channel CRUD
# ===========================================================================


@router.get("/admin/server-channels", response_model=list[ServerChannelPublic])
def list_channels(session: SessionDep, current_user: SuperUser) -> Any:
    """All configured channels. Secrets are never included."""
    return [
        ServerChannelService.to_public(c)
        for c in ServerChannelService.list_channels(session)
    ]


@router.post("/admin/server-channels", response_model=ServerChannelPublic)
async def create_channel(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: ServerChannelCreate,
) -> Any:
    """Create a channel and mint its webhook token."""
    try:
        channel = ServerChannelService.create_channel(session, data, current_user)
    except UnknownChannelTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ChannelConfigError, InvalidChannelPolicyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except DuplicateChannelNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_CREATED,
        {
            "server_channel_id": str(channel.id),
            "channel_type": channel.channel_type,
            "name": channel.name,
        },
    )
    return ServerChannelService.to_public(channel)


@router.put("/admin/server-channels/{channel_id}", response_model=ServerChannelPublic)
async def update_channel(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID,
    data: ServerChannelUpdate,
) -> Any:
    """Patch a channel. Omitting ``secrets`` keeps the stored credential."""
    channel = _get_channel_or_404(session, channel_id)
    try:
        channel = ServerChannelService.update_channel(session, channel, data)
    except UnknownChannelTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ChannelConfigError, InvalidChannelPolicyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except DuplicateChannelNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_UPDATED,
        {
            "server_channel_id": str(channel.id),
            # Field NAMES only — never values, which include the secret.
            "fields": sorted(data.model_dump(exclude_unset=True).keys()),
        },
    )
    if data.regenerate_webhook_token:
        await _audit(
            session,
            current_user,
            security_event_constants.SERVER_CHANNEL_TOKEN_REGENERATED,
            {"server_channel_id": str(channel.id)},
        )
    return ServerChannelService.to_public(channel)


@router.delete(
    "/admin/server-channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_channel(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID,
) -> Response:
    """Delete a channel. Thread bindings cascade away with it."""
    channel = _get_channel_or_404(session, channel_id)
    details = {
        "server_channel_id": str(channel.id),
        "channel_type": channel.channel_type,
        "name": channel.name,
    }
    ServerChannelService.delete_channel(session, channel)
    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_DELETED,
        details,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/admin/server-channels/{channel_id}/setup-instructions",
    response_model=ChannelSetupInstructions,
)
def get_setup_instructions(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID,
) -> Any:
    """Adapter-shaped setup guidance, including the public webhook URL."""
    channel = _get_channel_or_404(session, channel_id)
    try:
        return ServerChannelService.get_setup_instructions(channel)
    except UnknownChannelTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/admin/server-channels/{channel_id}/test-outbound",
    response_model=ChannelTestOutboundResult,
)
async def test_outbound(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID,
    data: ChannelTestOutboundRequest,
) -> Any:
    """Send a test message to prove the outbound credential works.

    Mirrors the mail-server "test connection" action. Failures come back as a
    200 with ``success=false`` and an admin-readable reason — this is a
    diagnostic, so the error text is the whole point of the call.
    """
    from app.services.server_channels.adapters.registry import get_adapter

    channel = _get_channel_or_404(session, channel_id)
    text = data.text or "Test message from Cinna — your channel is configured correctly."
    try:
        adapter = get_adapter(channel.channel_type)
        # An email is resolved to a thread we have already observed; the
        # provider only ever receives a native thread identity.
        thread_key = (data.thread_key or "").strip() or (
            ServerChannelService.resolve_test_thread_key(
                session, channel, data.email or ""
            )
        )
        message_id = await adapter.send_message(channel, thread_key, text)
    except ChannelError as exc:
        return ChannelTestOutboundResult(success=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — diagnostics surface everything
        return ChannelTestOutboundResult(success=False, error=str(exc))
    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_TEST_SEND,
        {
            "server_channel_id": str(channel.id),
            # The resolved destination, not just what was typed — an email
            # target names a person, and that is the point of auditing this.
            "thread_key": thread_key,
            "target_email": data.email or "",
            # Deliberately NOT the message body: the audit answers who sent
            # something where, and SecurityEvent rows are broadly readable.
            "targeted_by": "email" if data.email else "thread_key",
        },
    )
    ChannelDebugBuffer.record(
        channel_id=channel.id,
        direction="outbound",
        kind=DEBUG_TEST_SEND,
        summary=f"Admin test message sent by {current_user.email}",
        sender_email=data.email,
        thread_key=thread_key,
        text=text,
    )
    return ChannelTestOutboundResult(success=True, external_message_id=message_id)


@router.get(
    "/admin/server-channels/{channel_id}/recent-senders",
    response_model=list[ChannelRecentSender],
)
def list_recent_senders(
    *, session: SessionDep, current_user: SuperUser, channel_id: uuid.UUID
) -> Any:
    """People this channel has seen, for the test-send target picker."""
    channel = _get_channel_or_404(session, channel_id)
    return ServerChannelService.list_recent_senders(session, channel)


@router.get(
    "/admin/server-channels/{channel_id}/debug-events",
    response_model=ChannelDebugEventsPublic,
)
def list_debug_events(
    *, session: SessionDep, current_user: SuperUser, channel_id: uuid.UUID
) -> Any:
    """Recent traffic on this channel, newest first.

    In-memory and process-local (see ``channel_debug_buffer``): this is a live
    debugging view, not the audit trail. ``SecurityEvent`` remains the durable
    record of denials and verification failures.
    """
    channel = _get_channel_or_404(session, channel_id)
    events = ChannelDebugBuffer.list_events(channel.id)
    return ChannelDebugEventsPublic(
        events=[
            ChannelDebugEventPublic(
                id=e.id,
                at=e.at,
                direction=e.direction,
                kind=e.kind,
                summary=e.summary,
                sender_email=e.sender_email,
                sender_display_name=e.sender_display_name,
                thread_key=e.thread_key,
                text=e.text,
                detail=e.detail,
                repeat=e.repeat,
            )
            for e in events
        ],
        buffer_size=settings.SERVER_CHANNEL_DEBUG_BUFFER_SIZE,
        capturing_since=CAPTURING_SINCE,
    )


@router.delete("/admin/server-channels/{channel_id}/debug-events")
def clear_debug_events(
    *, session: SessionDep, current_user: SuperUser, channel_id: uuid.UUID
) -> Message:
    """Drop this channel's captured events."""
    channel = _get_channel_or_404(session, channel_id)
    ChannelDebugBuffer.clear(channel.id)
    return Message(message="Debug events cleared")


# ===========================================================================
# Admin — per-channel user grants
# ===========================================================================


@router.get(
    "/admin/server-channels/{channel_id}/grants",
    response_model=list[ChannelGrantPublic],
)
def list_channel_grants(
    *, session: SessionDep, current_user: SuperUser, channel_id: uuid.UUID
) -> Any:
    """Who may use this channel when its visibility is ``restricted``.

    Returned for public channels too: the rows are the admin's saved allowlist,
    inert while the channel is public, and hiding them would lose the list on a
    visibility round-trip.
    """
    channel = _get_channel_or_404(session, channel_id)
    return ServerChannelService.list_grants(session, channel)


@router.put(
    "/admin/server-channels/{channel_id}/grants",
    response_model=list[ChannelGrantPublic],
)
async def replace_channel_grants(
    *,
    session: SessionDep,
    current_user: SuperUser,
    channel_id: uuid.UUID,
    data: ChannelGrantsUpdate,
) -> Any:
    """Replace the grant list with exactly the supplied users.

    Returns the resulting list so the picker re-renders from one response,
    including the names and ``granted_by`` attribution the service resolves.
    """
    channel = _get_channel_or_404(session, channel_id)
    grants = ServerChannelService.replace_grants(
        session, channel, data.user_ids, current_user
    )
    await _audit(
        session,
        current_user,
        security_event_constants.SERVER_CHANNEL_UPDATED,
        {
            "action": "grants_replaced",
            "server_channel_id": str(channel_id),
            # The resulting membership, not the request: ids the service
            # dropped as unresolvable were never granted, and an audit that
            # records the request would claim otherwise.
            "user_ids": sorted(str(g.user_id) for g in grants),
        },
    )
    return grants


# ===========================================================================
# Helpers
# ===========================================================================


def _get_channel_or_404(session: Session, channel_id: uuid.UUID):
    try:
        return ServerChannelService.get_channel(session, channel_id)
    except ChannelNotFoundError:
        raise HTTPException(status_code=404, detail="Channel not found")


async def _audit(
    session: Session,
    user: User,
    event_type: str,
    details: dict[str, Any],
) -> None:
    """Record an admin action. Never blocks the response on audit failure."""
    try:
        await SecurityEventService.create_event(
            session=session,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=event_type, severity="low", details=details
            ),
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception(
            "Failed to write security event %s", event_type
        )
