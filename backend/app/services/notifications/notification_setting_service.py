"""Per-user notification preference CRUD.

Preferences live in the ``user_notification_setting`` table keyed by
(user_id, notification_type). A missing row resolves to the catalog default,
so the table only stores explicit user overrides.
"""

from datetime import datetime, UTC
from uuid import UUID

from sqlmodel import Session as DBSession, select

from app.models.notifications.user_notification_setting import (
    NotificationSettingItem,
    UserNotificationSetting,
)
from app.services.notifications.notification_catalog import (
    NOTIFICATION_CATALOG,
    NotificationType,
)


class NotificationSettingService:
    @staticmethod
    def _get_row(
        db_session: DBSession,
        user_id: UUID,
        notification_type: NotificationType,
    ) -> UserNotificationSetting | None:
        return db_session.exec(
            select(UserNotificationSetting).where(
                UserNotificationSetting.user_id == user_id,
                UserNotificationSetting.notification_type == notification_type.value,
            )
        ).first()

    @staticmethod
    def is_email_enabled(
        db_session: DBSession,
        user_id: UUID,
        notification_type: NotificationType,
    ) -> bool:
        """Resolve the effective email preference for a (user, type) pair.

        Returns the stored row's value, or the catalog default when no row
        exists.
        """
        row = NotificationSettingService._get_row(
            db_session, user_id, notification_type
        )
        if row is not None:
            return row.email_enabled
        return NOTIFICATION_CATALOG[notification_type].default_email_enabled

    @staticmethod
    def list_for_user(
        db_session: DBSession, user_id: UUID
    ) -> list[NotificationSettingItem]:
        """One item per catalog type, merged with the user's stored overrides."""
        rows = db_session.exec(
            select(UserNotificationSetting).where(
                UserNotificationSetting.user_id == user_id
            )
        ).all()
        stored = {row.notification_type: row.email_enabled for row in rows}

        items: list[NotificationSettingItem] = []
        for notification_type, meta in NOTIFICATION_CATALOG.items():
            items.append(
                NotificationSettingItem(
                    notification_type=notification_type.value,
                    label=meta.label,
                    description=meta.description,
                    email_enabled=stored.get(
                        notification_type.value, meta.default_email_enabled
                    ),
                )
            )
        return items

    @staticmethod
    def set_email_enabled(
        db_session: DBSession,
        user_id: UUID,
        notification_type: NotificationType,
        enabled: bool,
    ) -> NotificationSettingItem:
        """Upsert one preference row and return the merged item."""
        row = NotificationSettingService._get_row(
            db_session, user_id, notification_type
        )
        if row is None:
            row = UserNotificationSetting(
                user_id=user_id,
                notification_type=notification_type.value,
                email_enabled=enabled,
            )
        else:
            row.email_enabled = enabled
            row.updated_at = datetime.now(UTC)

        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)

        meta = NOTIFICATION_CATALOG[notification_type]
        return NotificationSettingItem(
            notification_type=notification_type.value,
            label=meta.label,
            description=meta.description,
            email_enabled=row.email_enabled,
        )
