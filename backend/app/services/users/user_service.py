"""
User Service - Business logic for user management operations.
"""
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone, UTC
from typing import Any

from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import or_
from sqlmodel import Session, col, delete, func, select

from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.models import (
    SecurityEvent,
    SecurityEventCreate,
    User,
    UserCreate,
    UserMfaChallenge,
    UserPasskey,
    UserRecoveryCode,
    UserTotpSecret,
    UserTrustedDevice,
    UserUpdate,
)
from app.models.events import security_event as security_event_constants
from app.services.users.auth_service import AuthService
from app.services.users.role_service import RoleService
from app.utils import (
    generate_password_reset_token,
    generate_reset_password_email,
    send_email,
    verify_password_reset_token,
)

logger = logging.getLogger(__name__)

# Single validator for externally-supplied addresses (see
# ``UserService.create_external_user``). Mirrors ``UserBase.email``.
_EMAIL_ADAPTER: TypeAdapter[EmailStr] = TypeAdapter(EmailStr)


class UserService:
    """
    Service for user CRUD and password management operations.

    Raises ValueError on domain/business rule failures.
    Routes translate ValueError to HTTPException.
    """

    @staticmethod
    def create_user(*, session: Session, user_create: UserCreate) -> User:
        """Create a new user with hashed password.

        Superusers are upgraded to ``admin`` so the
        ``role ⇔ is_superuser`` invariant holds for freshly created rows.
        Non-superusers default to the operator-configured
        ``DEFAULT_USER_ROLE`` (``agent-user`` by default), resolved via
        ``RoleService.derive_default_role`` — the single source of truth
        for the creation-time default.
        """
        # Honour caller-provided role if present (e.g., admin creating
        # a developer); otherwise derive from is_superuser + config.
        provided = user_create.model_dump(exclude_unset=True)
        if "role" not in provided:
            provided_role = RoleService.derive_default_role(
                is_superuser=user_create.is_superuser
            )
        else:
            provided_role = user_create.role

        update: dict[str, Any] = {
            "hashed_password": get_password_hash(user_create.password),
            "role": provided_role,
        }
        # Superusers are trusted/admin-bootstrapped and are auto-confirmed —
        # an unconfirmed superuser would have its own notifications/email
        # gated and agent limit clamped, which defeats the purpose. The
        # anti-abuse gate targets ordinary public signups.
        if user_create.is_superuser:
            update["email_confirmed"] = True
            update["email_confirmed_at"] = datetime.now(timezone.utc)

        db_obj = User.model_validate(user_create, update=update)
        session.add(db_obj)
        session.commit()
        session.refresh(db_obj)
        return db_obj

    @staticmethod
    def update_user(*, session: Session, db_user: User, user_in: UserUpdate) -> Any:
        """Update an existing user, hashing password if provided."""
        user_data = user_in.model_dump(exclude_unset=True)
        extra_data = {}
        if "password" in user_data:
            password = user_data["password"]
            hashed_password = get_password_hash(password)
            extra_data["hashed_password"] = hashed_password
        db_user.sqlmodel_update(user_data, update=extra_data)
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        return db_user

    @staticmethod
    def get_user_by_email(*, session: Session, email: str) -> User | None:
        """Look up a user by email address."""
        statement = select(User).where(User.email == email)
        return session.exec(statement).first()

    @staticmethod
    def search_users(
        *,
        session: Session,
        query: str,
        exclude_user_id: uuid.UUID | None = None,
        limit: int = 10,
    ) -> list[User]:
        """Case-insensitive substring search on email / full_name.

        Backs the sharing pickers' ``GET /users/search`` endpoint. Returns
        active users only, ordered by email, optionally excluding the
        requester. Callers should enforce a minimum query length before
        calling — an empty/whitespace query returns no results.
        """
        term = (query or "").strip()
        if not term:
            return []
        # Escape LIKE wildcards so a user typing "%" or "_" matches those
        # literal characters rather than acting as a wildcard.
        safe = (
            term.lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{safe}%"
        statement = select(User).where(
            User.is_active == True,  # noqa: E712
            or_(
                func.lower(User.email).like(pattern, escape="\\"),
                func.lower(func.coalesce(User.full_name, "")).like(
                    pattern, escape="\\"
                ),
            ),
        )
        if exclude_user_id is not None:
            statement = statement.where(User.id != exclude_user_id)
        statement = statement.order_by(col(User.email)).limit(limit)
        return list(session.exec(statement).all())

    @staticmethod
    def authenticate(*, session: Session, email: str, password: str) -> User | None:
        """Authenticate a user by email and password. Returns None on failure."""
        db_user = UserService.get_user_by_email(session=session, email=email)
        if not db_user:
            return None
        if not db_user.hashed_password:
            return None
        if not verify_password(password, db_user.hashed_password):
            return None
        return db_user

    @staticmethod
    def register_user(
        *, session: Session, email: str, password: str, full_name: str | None = None
    ) -> User:
        """
        Register a new user with domain whitelist and duplicate checks.

        Raises:
            ValueError: If domain not allowed or email already exists.
        """
        if not AuthService.is_email_domain_allowed(email):
            raise ValueError("Registration is restricted to specific email domains")

        existing = UserService.get_user_by_email(session=session, email=email)
        if existing:
            raise ValueError(
                "The user with this email already exists in the system"
            )

        user_create = UserCreate(email=email, password=password, full_name=full_name)
        user = UserService.create_user(session=session, user_create=user_create)
        # First confirmation email at signup (force=True bypasses cooldown).
        # Self-service signups start unconfirmed; this lets them confirm.
        from app.services.users.email_confirmation_service import (
            EmailConfirmationService,
        )
        EmailConfirmationService.send_confirmation_email(
            session=session, user=user, force=True
        )
        return user

    @staticmethod
    def create_external_user(
        *,
        session: Session,
        email: str,
        confirmed: bool,
        provenance: str,
        passwordless: bool = False,
    ) -> User:
        """
        Get-or-create the platform account for an externally-arriving sender.

        Shared by every inbound integration that meets a person before that
        person has ever visited the platform: the email integration (sender of
        an inbound mail) and server channels (sender of a chat message). All
        such accounts are ordinary users — they pick up ``DEFAULT_USER_ROLE``
        and every downstream gate (agent limits, credential isolation, catalog
        visibility) applies unchanged.

        Deliberately does **not** enforce ``AUTH_WHITELIST_USER_DOMAINS``: the
        integration's own allowlist is the registration gate, and re-checking
        the signup whitelist here would silently break configurations where
        the two differ.

        Idempotent, and never mutates an account that already exists — an
        external contact must not be able to flip flags on someone's real
        account (e.g. confirm an address they don't control).

        The address is normalised (stripped + lowercased) and validated here
        rather than at each call site, so both integrations agree on what
        counts as "the same person" — otherwise ``Alice@x.com`` and
        ``alice@x.com`` would become two accounts for the same human.

        Args:
            session: Database session.
            email: The sender's address, as verified by the integration.
            confirmed: True only when the transport itself verified the
                address (Google-signed identity). Confirmed users skip the
                confirmation email; unconfirmed ones are sent one so they can
                confirm later if they become an operator.
            provenance: Short origin tag for the audit log, e.g.
                ``"email_integration"`` or ``"server_channel:<id>"``.
            passwordless: True to create the account with no password at all
                (``hashed_password=None``, as Google OAuth signup does).
                False generates an unguessable random password the user never
                receives — kept as the email integration's existing behaviour.

        Returns:
            The existing or newly created ``User``.

        Raises:
            ValueError: If ``email`` is not a valid address.
        """
        # Validate before the lookup. The `passwordless` branch below builds a
        # `User` (table=True) directly, and SQLModel skips validation on table
        # models — without this, the two branches would disagree on what a
        # valid address is.
        try:
            email = _EMAIL_ADAPTER.validate_python(email.strip().lower())
        except PydanticValidationError as exc:
            raise ValueError(f"Invalid email address: {email!r}") from exc

        existing = UserService.get_user_by_email(session=session, email=email)
        if existing:
            return existing

        if passwordless:
            user = User(
                email=email,
                hashed_password=None,
                is_active=True,
                is_superuser=False,
                role=RoleService.derive_default_role(is_superuser=False),
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        else:
            random_password = secrets.token_urlsafe(32)
            user_create = UserCreate(
                email=email,
                password=random_password,
                is_active=True,
            )
            user = UserService.create_user(session=session, user_create=user_create)

        from app.services.users.email_confirmation_service import (
            EmailConfirmationService,
        )

        if confirmed:
            EmailConfirmationService.mark_confirmed(session=session, user=user)
        else:
            # Unconfirmed external users can still be reached: their
            # agent-reply path is gated on the agent/install OWNER, not on
            # this sender, so legitimate replies are unaffected.
            EmailConfirmationService.send_confirmation_email(
                session=session, user=user, force=True
            )

        logger.info(
            "Created external user %s (provenance=%s, confirmed=%s, passwordless=%s)",
            user.id,
            provenance,
            confirmed,
            passwordless,
        )
        return user

    @staticmethod
    def update_password(
        *, session: Session, user: User, current_password: str, new_password: str
    ) -> None:
        """
        Update password for a user who already has one set.

        Raises:
            ValueError: If no password set, current password wrong, or same password.
        """
        if not user.hashed_password:
            raise ValueError("No password set. Use set-password endpoint first.")
        if not verify_password(current_password, user.hashed_password):
            raise ValueError("Incorrect password")
        if current_password == new_password:
            raise ValueError(
                "New password cannot be the same as the current one"
            )
        user.hashed_password = get_password_hash(new_password)
        session.add(user)
        session.commit()

    @staticmethod
    def set_password(*, session: Session, user: User, new_password: str) -> None:
        """
        Set password for an OAuth user who doesn't have one yet.

        Raises:
            ValueError: If password already set.
        """
        if user.hashed_password:
            raise ValueError(
                "Password already set. Use update password endpoint instead."
            )
        user.hashed_password = get_password_hash(new_password)
        session.add(user)
        session.commit()

    @staticmethod
    def reset_password(*, session: Session, token: str, new_password: str) -> None:
        """
        Reset password using a password-reset token.

        Raises:
            ValueError: If token invalid, user not found, or user inactive.
        """
        email = verify_password_reset_token(token=token)
        if not email:
            raise ValueError("Invalid token")
        user = UserService.get_user_by_email(session=session, email=email)
        if not user:
            raise ValueError(
                "The user with this email does not exist in the system."
            )
        if not user.is_active:
            raise ValueError("Inactive user")
        user.hashed_password = get_password_hash(password=new_password)
        session.add(user)
        session.commit()

    @staticmethod
    def disable_all_factors(
        *,
        session: Session,
        user: User,
        reason: str = "user_initiated",
    ) -> None:
        """Wipe every 2FA artefact for ``user`` and flip the master flag off.

        Called by ``POST /users/me/mfa/disable`` after a step-up factor
        has already been verified, AND from ``MfaService.disable_totp`` /
        ``MfaService.delete_passkey`` when the removed factor was the
        user's last one (``reason="last_factor_removed"``). Removes:

        - all :class:`UserPasskey` rows
        - the :class:`UserTotpSecret` row (if any)
        - all :class:`UserRecoveryCode` rows
        - pending :class:`UserMfaChallenge` rows
        - all :class:`UserTrustedDevice` rows (so any live
          "Do not ask on this device" token becomes inert)

        Also writes a :data:`MFA_DISABLED` security-event row for the
        audit trail.  Idempotent — safe to call when no factors are
        enrolled.
        """
        for stmt in (
            delete(UserPasskey).where(UserPasskey.user_id == user.id),
            delete(UserTotpSecret).where(UserTotpSecret.user_id == user.id),
            delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id),
            delete(UserMfaChallenge).where(UserMfaChallenge.user_id == user.id),
            delete(UserTrustedDevice).where(
                UserTrustedDevice.user_id == user.id
            ),
        ):
            session.exec(stmt)
        user.two_factor_enabled = False
        # We intentionally keep ``two_factor_enrolled_at`` and
        # ``two_factor_last_used_at`` for historical reference — they
        # are cleared on the next fresh enrollment.
        session.add(user)
        payload = SecurityEventCreate(
            event_type=security_event_constants.MFA_DISABLED,
            severity="medium",
            details={"reason": reason},
        )
        session.add(
            SecurityEvent(
                user_id=user.id,
                event_type=payload.event_type,
                severity=payload.severity,
                details=json.dumps(payload.details),
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        # Bulk deletes don't auto-expire ORM-tracked rows. Refresh the
        # passed-in user so any post-call read in the same request sees
        # the updated `two_factor_enabled=False` without surprises.
        session.refresh(user)

    @staticmethod
    def recover_password(*, session: Session, email: str) -> None:
        """
        Send a password recovery email.

        Password recovery is NEVER gated by ``email_confirmed`` — an
        unconfirmed user must still be able to recover their password.
        A per-user cooldown (``last_password_recovery_email_sent_at``)
        rate-limits repeated sends; while cooling down the send is skipped
        SILENTLY so the public response stays a generic "email sent".

        Raises:
            ValueError: If user not found.
        """
        user = UserService.get_user_by_email(session=session, email=email)
        if not user:
            raise ValueError(
                "The user with this email does not exist in the system."
            )
        # Cooldown — skip the send silently if still cooling down (preserve
        # the generic success message; never raise here).
        last_sent = user.last_password_recovery_email_sent_at
        if last_sent is not None:
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            interval = timedelta(
                seconds=settings.PASSWORD_RECOVERY_EMAIL_COOLDOWN_SECONDS
            )
            if datetime.now(timezone.utc) - last_sent < interval:
                return

        password_reset_token = generate_password_reset_token(email=email)
        email_data = generate_reset_password_email(
            email_to=user.email, email=email, token=password_reset_token
        )
        send_email(
            email_to=user.email,
            subject=email_data.subject,
            html_content=email_data.html_content,
        )
        user.last_password_recovery_email_sent_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
