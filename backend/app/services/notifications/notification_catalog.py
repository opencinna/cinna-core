"""Single source of truth for the system-notification types.

The catalog defines which notifications exist, their user-facing copy, defaults,
email template, subject builder, and dedup scope. Both the settings API and the
dispatch service read from it. Adding a new notification type requires only:

    1. a new ``NotificationType`` enum value,
    2. a matching ``NOTIFICATION_CATALOG`` entry,
    3. a built email template under ``email-templates/build/``.

No service or route changes are needed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from app.core.config import settings


class NotificationType(str, Enum):
    """Catalog keys for system notifications."""

    SESSION_ERROR = "session_error"


@dataclass(frozen=True)
class NotificationTypeMeta:
    """Per-type metadata driving copy, defaults, and rendering."""

    label: str
    description: str
    default_email_enabled: bool
    # Filename under email-templates/build/ (the runtime reads the built HTML).
    email_template: str
    # Builds the email subject from the render context.
    subject: Callable[[dict], str]
    # Which context key dedups repeats (e.g. "session_id"); None disables dedup.
    dedup_scope: str | None = None


NOTIFICATION_CATALOG: dict[NotificationType, NotificationTypeMeta] = {
    NotificationType.SESSION_ERROR: NotificationTypeMeta(
        label="Session errors",
        description="Email me when one of my agent sessions ends with an error.",
        default_email_enabled=True,
        email_template="session_error.html",
        subject=lambda ctx: (
            f"{settings.PROJECT_NAME} — Session error on agent "
            f"{ctx.get('agent_name', 'your agent')}"
        ),
        dedup_scope="session_id",
    ),
}
