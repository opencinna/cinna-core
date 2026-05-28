import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel
from sqlalchemy import Index, UniqueConstraint


class UserNotificationSetting(SQLModel, table=True):
    """Per-(user, notification_type) preference row.

    A missing row means "use the catalog default" for that notification type,
    so new notification types require no migration and existing users get
    sensible defaults automatically. Rows are created lazily (upsert) only when
    a user changes a preference.
    """

    __tablename__ = "user_notification_setting"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "notification_type",
            name="uq_user_notification_setting_user_type",
        ),
        Index("ix_user_notification_setting_user_id", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    # Catalog key, e.g. "session_error". Validated against NotificationType in
    # the service layer; stored as a plain string so adding new types needs no
    # schema change.
    notification_type: str = Field(max_length=64)
    # Whether the email channel is on for this type. Kept explicit (not a
    # generic "enabled") so adding future channels stays non-breaking.
    email_enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Request body for PUT /notification-settings/{notification_type}
class UserNotificationSettingUpdate(SQLModel):
    email_enabled: bool


# A catalog item merged with the user's effective state (one per type).
class NotificationSettingItem(SQLModel):
    notification_type: str
    label: str
    description: str
    email_enabled: bool


# Response for GET /notification-settings/
class NotificationSettingsPublic(SQLModel):
    data: list[NotificationSettingItem]
