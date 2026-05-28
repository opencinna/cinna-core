from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    NotificationSettingItem,
    NotificationSettingsPublic,
    UserNotificationSettingUpdate,
)
from app.services.notifications.notification_catalog import NotificationType
from app.services.notifications.notification_setting_service import (
    NotificationSettingService,
)

router = APIRouter(
    prefix="/notification-settings", tags=["notification-settings"]
)


@router.get("/", response_model=NotificationSettingsPublic)
def read_notification_settings(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List the notification catalog merged with the current user's effective state."""
    items = NotificationSettingService.list_for_user(session, current_user.id)
    return NotificationSettingsPublic(data=items)


@router.put("/{notification_type}", response_model=NotificationSettingItem)
def update_notification_setting(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    notification_type: str,
    setting_in: UserNotificationSettingUpdate,
) -> Any:
    """Upsert one notification preference for the current user."""
    try:
        nt = NotificationType(notification_type)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown notification type: {notification_type}",
        )

    return NotificationSettingService.set_email_enabled(
        session, current_user.id, nt, setting_in.email_enabled
    )
