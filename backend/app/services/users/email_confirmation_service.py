"""Email Confirmation Service.

Owns the email-confirmation lifecycle (token send, resend with cooldown,
confirm, Google auto-confirm) and the single source of truth for the
outbound-email gate (:meth:`is_outbound_email_allowed`).

The gate exists to harden a publicly-reachable server against abuse of its
outbound-email capability: an unconfirmed account may not send or receive
any platform email EXCEPT password recovery (which bypasses the gate and is
rate-limited separately).

Cooldown anchors live as columns on the ``User`` row (not in memory) because
the public/by-email resend + recovery endpoints have no authenticated user
and may be served by multiple workers.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.core.config import settings
from app.models.users.user import User
from app.utils import (
    generate_confirmation_email,
    generate_email_confirmation_token,
    send_email,
    verify_email_confirmation_token,
)

logger = logging.getLogger(__name__)


def _cooldown_elapsed(last_sent: datetime | None, interval: timedelta) -> bool:
    """Return True if ``interval`` has elapsed since ``last_sent`` (or never sent).

    Handles naive timestamps coming back from the DB by assuming UTC, so the
    comparison never raises on tz-aware vs naive.
    """
    if last_sent is None:
        return True
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_sent >= interval


class EmailConfirmationService:
    """Business logic for the email-confirmation feature."""

    # ── Central outbound-email gate ──────────────────────────────────
    @staticmethod
    def is_outbound_email_allowed(user: User | None) -> bool:
        """The single source of truth for the outbound-email gate.

        Returns True only for active, email-confirmed users. Fail-safe:
        a missing or inactive user returns False. Password-recovery
        callers must NOT use this — recovery bypasses the gate
        intentionally and checks ``is_active`` itself.
        """
        if user is None:
            return False
        if not user.is_active:
            return False
        return bool(user.email_confirmed)

    # ── Confirmation lifecycle ───────────────────────────────────────
    @staticmethod
    def send_confirmation_email(
        *, session: Session, user: User, force: bool = False
    ) -> bool:
        """Send a confirmation email if appropriate.

        Sends when emails are enabled, the user is active and unconfirmed,
        and either ``force=True`` (first send at account creation) or the
        resend cooldown has elapsed. Stamps
        ``last_confirmation_email_sent_at`` on send.

        Returns True if an email was sent, False if suppressed (cooldown,
        emails disabled, inactive, or already confirmed). Never raises on
        a delivery failure — logs and reports False so callers (signup,
        admin-create) are never blocked by SMTP problems.
        """
        if not settings.emails_enabled:
            return False
        if not user.is_active or user.email_confirmed or not user.email:
            return False
        if not force and not _cooldown_elapsed(
            user.last_confirmation_email_sent_at,
            timedelta(seconds=settings.CONFIRMATION_EMAIL_COOLDOWN_SECONDS),
        ):
            return False

        token = generate_email_confirmation_token(email=user.email)
        email_data = generate_confirmation_email(
            email_to=user.email, email=user.email, token=token
        )
        try:
            send_email(
                email_to=user.email,
                subject=email_data.subject,
                html_content=email_data.html_content,
            )
        except Exception as e:  # noqa: BLE001 — delivery must never block flows
            logger.error(
                f"Failed to send confirmation email to {user.email}: {e}",
                exc_info=True,
            )
            return False

        user.last_confirmation_email_sent_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
        session.refresh(user)
        return True

    @staticmethod
    def resend_confirmation(*, session: Session, email: str) -> None:
        """Public, by-email resend. Always silent (no enumeration oracle).

        Looks up the user; if it exists, is active, unconfirmed, and the
        cooldown has elapsed, sends a confirmation email. Any other state
        (unknown email, already confirmed, in cooldown) silently no-ops.
        """
        user = session.exec(select(User).where(User.email == email)).first()
        if not user:
            return
        EmailConfirmationService.send_confirmation_email(
            session=session, user=user, force=False
        )

    @staticmethod
    def confirm_email(*, session: Session, token: str) -> User:
        """Verify the token and mark the user confirmed (idempotent).

        Raises ValueError("Invalid token") on a bad/expired token (or one
        lacking the confirmation purpose), and ValueError on a missing /
        inactive user.
        """
        email = verify_email_confirmation_token(token=token)
        if not email:
            raise ValueError("Invalid token")
        user = session.exec(select(User).where(User.email == email)).first()
        if not user:
            raise ValueError(
                "The user with this email does not exist in the system."
            )
        if not user.is_active:
            raise ValueError("Inactive user")
        if not user.email_confirmed:
            EmailConfirmationService.mark_confirmed(session=session, user=user)
        return user

    @staticmethod
    def mark_confirmed(*, session: Session, user: User) -> None:
        """Directly confirm a user (no token) — used for Google OAuth.

        Idempotent: a no-op when the user is already confirmed.
        """
        if user.email_confirmed:
            return
        user.email_confirmed = True
        user.email_confirmed_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
        session.refresh(user)

    @staticmethod
    def resend_available_at(user: User) -> datetime | None:
        """Earliest time the next resend is allowed, for the UI countdown.

        ``None`` when no confirmation email has been sent yet (resend is
        immediately available).
        """
        last_sent = user.last_confirmation_email_sent_at
        if last_sent is None:
            return None
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        return last_sent + timedelta(
            seconds=settings.CONFIRMATION_EMAIL_COOLDOWN_SECONDS
        )
