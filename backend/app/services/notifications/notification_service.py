"""Generic system-notification dispatch.

``SystemNotificationService.notify`` is the single entry point for every system
notification. It guards on platform email config + per-user preference + a
generic in-memory throttle, then renders the catalog template and offloads the
blocking SMTP send off the event loop. The whole body is failure-isolated: a
notification failure never propagates into the activity/event pipeline.
"""

import logging
import time
from collections import deque
from threading import Lock
from uuid import UUID

import anyio
from sqlmodel import Session as DBSession

from app.core.config import settings
from app.models.users.user import User
from app.utils import (
    EmailData,
    create_task_with_error_logging,
    render_email_template,
    send_email,
)
from app.services.notifications.notification_catalog import (
    NOTIFICATION_CATALOG,
    NotificationType,
)
from app.services.notifications.notification_setting_service import (
    NotificationSettingService,
)

logger = logging.getLogger(__name__)

# ── Throttle configuration (best-effort, in-memory, process-local) ──────────
# Dedup suppresses repeated notifications for the same (type, dedup-value)
# within DEDUP_TTL_SECONDS. The per-user rate cap limits total notifications
# per user within RATE_WINDOW_SECONDS. State resets on restart (documented as
# acceptable — at worst one extra email shortly after a deploy).
DEDUP_TTL_SECONDS = 30 * 60  # 30 minutes
RATE_WINDOW_SECONDS = 15 * 60  # 15 minutes
MAX_PER_WINDOW = 5  # max notifications per user per window

# Maps a dedup key -> last-sent monotonic timestamp.
_dedup_seen: dict[tuple[str, str], float] = {}
# Maps a user id -> deque of recent send monotonic timestamps within the window.
_user_window: dict[UUID, deque[float]] = {}
_throttle_lock = Lock()

# Tracks whether the "emails disabled" warning has already been logged, to
# avoid log spam on every dispatch attempt.
_disabled_warned = False
_disabled_lock = Lock()

# Defensive cap on error text included in email bodies.
_MAX_ERROR_TEXT_CHARS = 500


class SystemNotificationService:
    @staticmethod
    async def notify(
        db_session: DBSession,
        *,
        user_id: UUID,
        notification_type: NotificationType,
        context: dict,
    ) -> None:
        """Dispatch a single system notification.

        Guards (in order): platform emails enabled, recipient active, preference
        enabled, throttle ok. Renders the catalog template and offloads the
        blocking SMTP send. Never raises.
        """
        try:
            meta = NOTIFICATION_CATALOG.get(notification_type)
            if meta is None:
                logger.warning(
                    f"Unknown notification type {notification_type!r}; skipping"
                )
                return

            if not settings.emails_enabled:
                SystemNotificationService._log_disabled_once()
                return

            user = db_session.get(User, user_id)
            if not user or not user.is_active or not user.email:
                return

            if not NotificationSettingService.is_email_enabled(
                db_session, user_id, notification_type
            ):
                return

            if not SystemNotificationService._should_send(
                notification_type, context, user_id
            ):
                return

            # Defensively truncate any error text before rendering.
            render_context = SystemNotificationService._sanitize_context(context)

            subject = meta.subject(render_context)
            html = render_email_template(
                template_name=meta.email_template, context=render_context
            )
            email_data = EmailData(html_content=html, subject=subject)
            recipient = user.email

            create_task_with_error_logging(
                SystemNotificationService._async_send(recipient, email_data),
                task_name=f"notification_{notification_type.value}",
            )
            SystemNotificationService._mark_sent(
                notification_type, context, user_id
            )
        except Exception as e:
            logger.error(
                f"Failed to dispatch {notification_type} notification "
                f"for user {user_id}: {e}",
                exc_info=True,
            )

    @staticmethod
    async def _async_send(recipient: str, email_data: EmailData) -> None:
        """Run the blocking SMTP send in a worker thread off the event loop."""
        try:
            await anyio.to_thread.run_sync(
                lambda: send_email(
                    email_to=recipient,
                    subject=email_data.subject,
                    html_content=email_data.html_content,
                )
            )
        except Exception as e:
            logger.error(
                f"Failed to send notification email to {recipient}: {e}",
                exc_info=True,
            )

    @staticmethod
    def _sanitize_context(context: dict) -> dict:
        """Copy context and defensively truncate the error text field."""
        sanitized = dict(context)
        error_text = sanitized.get("error_text")
        if isinstance(error_text, str) and len(error_text) > _MAX_ERROR_TEXT_CHARS:
            sanitized["error_text"] = (
                error_text[: _MAX_ERROR_TEXT_CHARS - 3] + "..."
            )
        return sanitized

    @staticmethod
    def _should_send(
        notification_type: NotificationType, context: dict, user_id: UUID
    ) -> bool:
        """Throttle gate: per-(type, dedup) dedup + per-user rate cap.

        This only checks; it does not mutate throttle state (that happens in
        ``_mark_sent`` after dispatch is scheduled).
        """
        meta = NOTIFICATION_CATALOG[notification_type]
        now = time.monotonic()

        with _throttle_lock:
            SystemNotificationService._prune_locked(now)

            # Dedup check.
            if meta.dedup_scope:
                dedup_value = context.get(meta.dedup_scope)
                if dedup_value is not None:
                    key = (notification_type.value, str(dedup_value))
                    last = _dedup_seen.get(key)
                    if last is not None and (now - last) < DEDUP_TTL_SECONDS:
                        return False

            # Per-user rate cap. The window is already pruned by _prune_locked.
            window = _user_window.get(user_id)
            if window and len(window) >= MAX_PER_WINDOW:
                return False

        return True

    @staticmethod
    def _mark_sent(
        notification_type: NotificationType, context: dict, user_id: UUID
    ) -> None:
        """Record a dispatched notification in the throttle state."""
        meta = NOTIFICATION_CATALOG[notification_type]
        now = time.monotonic()

        with _throttle_lock:
            SystemNotificationService._prune_locked(now)

            if meta.dedup_scope:
                dedup_value = context.get(meta.dedup_scope)
                if dedup_value is not None:
                    _dedup_seen[(notification_type.value, str(dedup_value))] = now

            window = _user_window.setdefault(user_id, deque())
            while window and (now - window[0]) >= RATE_WINDOW_SECONDS:
                window.popleft()
            window.append(now)

    @staticmethod
    def _prune_locked(now: float) -> None:
        """Drop expired throttle state. Caller must hold ``_throttle_lock``.

        Removes dedup entries past their TTL and per-user windows whose recent
        timestamps have all aged out, bounding memory for the process lifetime.
        """
        expired_dedup = [
            key for key, last in _dedup_seen.items()
            if (now - last) >= DEDUP_TTL_SECONDS
        ]
        for key in expired_dedup:
            del _dedup_seen[key]

        empty_windows = []
        for uid, window in _user_window.items():
            while window and (now - window[0]) >= RATE_WINDOW_SECONDS:
                window.popleft()
            if not window:
                empty_windows.append(uid)
        for uid in empty_windows:
            _user_window.pop(uid, None)

    @staticmethod
    def _log_disabled_once() -> None:
        """Emit the 'emails disabled' warning at most once per process."""
        global _disabled_warned
        with _disabled_lock:
            if _disabled_warned:
                return
            _disabled_warned = True
        logger.warning(
            "Email sending disabled: SMTP_HOST / EMAILS_FROM_EMAIL not set. "
            "System notifications will be skipped."
        )
