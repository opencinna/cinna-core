"""User-facing channel settings — Settings → Channels.

Ordinary authentication, and everything here is scoped to ``current_user`` by
construction: no route takes a user id, so there is no parameter that could be
pointed at somebody else's settings.

**This router must never return ``ServerChannelPublic``.** That model is the
superuser projection and carries ``webhook_token`` — the unguessable path
segment that *is* the channel's inbound authentication — along with ``config``,
``email_whitelist`` and ``has_outbound_credentials``. The defence is the
separate ``UserChannelPublic`` model rather than a field exclusion, because a
model cannot leak a field it does not declare, whereas an exclusion list
silently stops covering whatever is added to the admin model next.

**A channel the caller may not use answers 404, identically to one that does
not exist.** A "you have not been granted this channel" response would let any
authenticated user enumerate the restricted channels on the server. The webhook
answers 404 to both an unknown token and a disabled channel for the same
reason.

The inheritance rules are **not** implemented here. Every value on the response
is already resolved by ``ChannelPolicyService``, and each inheritable field
carries a flag saying whether the value came from the user or the admin
default, so the UI renders "following the admin default (on)" without owning a
second copy of the rules.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.models import UserChannelPublic, UserChannelUpdate
from app.services.server_channels.user_channel_service import (
    ChannelNotAvailableError,
    InvalidUserChannelSettingError,
    UserChannelService,
)

router = APIRouter(prefix="/users/me/channels", tags=["user-channels"])


@router.get("", response_model=list[UserChannelPublic])
def list_my_channels(session: SessionDep, current_user: CurrentUser) -> Any:
    """Every channel available to me, with my resolved settings.

    Includes channels I have switched off for myself — otherwise there would be
    no row to switch back on. ``is_enabled`` reports my toggle; ``is_available``
    reports the whole conjunction.
    """
    return UserChannelService.list_for_user(session, current_user.id)


@router.put("/{channel_id}", response_model=UserChannelPublic)
def update_my_channel(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    channel_id: uuid.UUID,
    data: UserChannelUpdate,
) -> Any:
    """Save my settings for one channel. Creates the row if I have none.

    This is the **only** place a ``channel_user_setting`` row is created —
    every read path treats a missing row as "the channel default applies".

    An omitted field is left unchanged; an explicit ``null`` clears it, which
    for ``is_enabled`` and ``agent_scope`` means reverting that one field to the
    channel default. ``allow_identity_routing`` has no inherited state, so an
    explicit ``null`` for it is a 422 rather than a silent no-op.
    """
    try:
        channel = UserChannelService.get_accessible_channel(
            session, channel_id, current_user.id
        )
        return UserChannelService.upsert_settings(
            session, channel, current_user.id, data
        )
    except ChannelNotAvailableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        )
    except InvalidUserChannelSettingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


@router.delete("/{channel_id}", response_model=UserChannelPublic)
def reset_my_channel(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    channel_id: uuid.UUID,
) -> Any:
    """Discard my settings for one channel, reverting me to inheritance.

    Returns the channel as it now resolves — every value inherited — so the UI
    re-renders from one response instead of guessing what the defaults were.
    A no-op when I have no row, because that is already the end state.
    """
    try:
        channel = UserChannelService.get_accessible_channel(
            session, channel_id, current_user.id
        )
    except ChannelNotAvailableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        )
    return UserChannelService.delete_settings(session, channel, current_user.id)
