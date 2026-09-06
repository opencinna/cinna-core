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
    AccountOrigin,
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
from app.models.users.user import VALID_USER_ROLES, UserRole
from app.services.users.access_policy_service import (
    AccessPolicyService,
    RegistrationNotAllowedError,
)
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

    # ── The account-creation chokepoint ────────────────────────────────

    @staticmethod
    def create_account(
        session: Session,
        *,
        email: str,
        origin: AccountOrigin,
        password: str | None = None,
        full_name: str | None = None,
        username: str | None = None,
        google_id: str | None = None,
        role: str | None = None,
        is_superuser: bool = False,
        is_active: bool = True,
        email_confirmed: bool = False,
        skip_auto_provision: bool = False,
    ) -> User:
        """Build the one and only ``User`` row, from any arrival path.

        WHY THIS EXISTS
        ---------------
        Before this, three call sites each constructed a ``User`` and each
        re-derived the creation-time rules from memory: the signup path, the
        Google path, and the passwordless branch of ``create_external_user``.
        They already disagreed — only one of them normalised the address, only
        two of them set the default role through ``RoleService``, none of them
        agreed about ``email_confirmed``. Every rule that must hold for *an
        account*, as opposed to for one way of getting one, therefore lives
        here: address normalisation, the ``is_superuser ⇒ admin`` invariant,
        the policy-derived default role, and — the reason phase 2 exists at
        all — auto-provisioning of the company's managed AI credentials.

        A new arrival path (invitations, SCIM, an identity provider) that goes
        through this function inherits all of it and cannot forget any of it.
        That is the whole design; ``origin`` is what makes it expressible.

        NORMALISATION IS PART OF IDENTITY
        ---------------------------------
        The address is stripped + lowercased and validated with the same
        adapter the external path already used, because ``Alice@x.com`` and
        ``alice@x.com`` are one human and Postgres' unique index is
        case-sensitive. The duplicate check below runs on the *normalised*
        address for the same reason: a caller that checked the raw string
        would otherwise hand us a collision and get an ``IntegrityError`` 500
        instead of a 400.

        THE POLICY GATE IS RE-ASSERTED, NOT MOVED
        -----------------------------------------
        ``register_user`` and ``create_user_from_google`` still call
        ``can_register`` *before* their duplicate check — that ordering is
        what keeps a closed instance from answering differently for a known
        and an unknown address. The second call here is not that check
        repeated for its answer; it is the structural guarantee that a future
        path which forgets the gate is refused anyway. It is a pure read of an
        already-materialised row, and it is idempotent.

        Args:
            email: The address. Normalised here.
            origin: Which arrival path this is. See :class:`AccountOrigin`.
            password: Plaintext to hash, or ``None`` for a passwordless
                account (Google, invited-but-not-yet-accepted, passwordless
                external senders). Length is validated by the request schema
                at the edge, not here.
            role: Explicit role, for the origins that carry admin intent.
                ``None`` derives it from the access policy.
            is_superuser: Forces ``role='admin'`` and auto-confirmation.
            email_confirmed: True when the arrival path itself verified the
                address (Google). Superusers are confirmed regardless.
            skip_auto_provision: Suppress
                ``AccountProvisioningService.on_account_created``. For paths
                that apply their own explicit credential list (phase 3's
                invite wizard).

        Raises:
            ValueError: invalid address, or the address already exists.
            RegistrationNotAllowedError: the access policy refuses this
                origin for this address.
        """
        try:
            email = _EMAIL_ADAPTER.validate_python(email.strip().lower())
        except PydanticValidationError as exc:
            raise ValueError(f"Invalid email address: {email!r}") from exc

        decision = AccessPolicyService.can_register(
            session, email=email, origin=origin
        )
        if not decision.allowed:
            assert decision.reason is not None
            raise RegistrationNotAllowedError(decision.reason)

        if UserService.get_user_by_email(session=session, email=email):
            raise ValueError(
                "The user with this email already exists in the system"
            )

        if role is not None and role not in VALID_USER_ROLES:
            # Checked here because this is now the only door. ``POST /users/``
            # validates a role on PATCH but not on create, and phase 3's invite
            # wizard will pass one straight through — one check at the
            # chokepoint retires the whole class rather than adding a third
            # place to forget it.
            raise ValueError(
                f"Invalid role '{role}'. Must be one of "
                f"{', '.join(VALID_USER_ROLES)}."
            )

        # Superusers are trusted/admin-bootstrapped: ``role`` is pinned to
        # ``admin`` so the ``role ⇔ is_superuser`` invariant holds for fresh
        # rows even when a caller passes something else, and they are
        # auto-confirmed — an unconfirmed superuser would have its own
        # notifications gated and agent limit clamped, which defeats the
        # purpose. The anti-abuse gate targets ordinary public signups.
        if is_superuser:
            resolved_role = UserRole.ADMIN.value
            email_confirmed = True
        elif role is not None:
            resolved_role = role
        else:
            resolved_role = RoleService.derive_default_role(
                session=session, is_superuser=False
            )

        user = User(
            email=email,
            hashed_password=(
                get_password_hash(password) if password is not None else None
            ),
            google_id=google_id,
            full_name=full_name,
            username=username,
            is_active=is_active,
            is_superuser=is_superuser,
            role=resolved_role,
            email_confirmed=email_confirmed,
            email_confirmed_at=(
                datetime.now(timezone.utc) if email_confirmed else None
            ),
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        if not skip_auto_provision:
            # ``on_account_created`` already promises never to raise. The
            # ``try`` is here anyway, and deliberately: this is the line that
            # makes "an account is never lost to a provisioning failure" a
            # property of the *account* code rather than a promise borrowed
            # from another module. The account row is committed above; nothing
            # after this point may take it away.
            # Snapshotted while the session is certainly good: on an aborted
            # transaction even ``user.id`` is a query, so a handler that logs
            # before it repairs throws from inside itself. Same rule the
            # provisioning service documents under ERROR HANDLING.
            new_user_id = user.id
            origin_value = origin.value
            try:
                from app.services.users.account_provisioning_service import (
                    AccountProvisioningService,
                )

                AccountProvisioningService.on_account_created(
                    session, user, origin
                )
            except Exception:  # pragma: no cover - the callee already guards
                # Unconditional, for the reason given in
                # ``AccountProvisioningService._restore_session``: a statement
                # -level failure aborts the transaction without clearing
                # ``session.is_active``, so a guard on that flag skips the
                # case the caller's next commit will die on.
                try:
                    session.rollback()
                except Exception:
                    logger.exception(
                        "Could not restore the session after a provisioning "
                        "failure for user %s.", new_user_id
                    )
                logger.warning(
                    "Auto-provisioning raised for new user %s (origin=%s) "
                    "despite guaranteeing it would not; the account stands.",
                    new_user_id,
                    origin_value,
                    exc_info=True,
                )

        return user

    @staticmethod
    def create_user(
        *,
        session: Session,
        user_create: UserCreate,
        origin: AccountOrigin = AccountOrigin.ADMIN,
    ) -> User:
        """Create a user from an admin-shaped ``UserCreate`` payload.

        A thin adapter over :meth:`create_account`: it exists because the
        admin route, the first-superuser seed and a good deal of test code
        already speak ``UserCreate``. ``origin`` defaults to ``admin`` — the
        seed in ``core.db.init_db`` overrides it with ``seed``.

        ``UserBase.role`` carries a column default, so "the caller did not
        say" and "the caller said ``agent-user``" look identical on the
        parsed model. Only the first should pick up the admin-configured
        policy default, so the distinction is read off ``exclude_unset``
        rather than off the value.
        """
        explicitly_set = user_create.model_dump(exclude_unset=True)
        return UserService.create_account(
            session,
            email=user_create.email,
            origin=origin,
            password=user_create.password,
            full_name=user_create.full_name,
            username=user_create.username,
            role=user_create.role if "role" in explicitly_set else None,
            is_superuser=user_create.is_superuser,
            is_active=user_create.is_active,
        )

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
        Register a new user through public signup.

        The access policy is the gate: open registration, password auth on,
        and a match against the allowed email patterns. Every refusal carries
        a reason code and **no** hint about whether the address exists — the
        duplicate check runs afterwards, so a closed instance answers the same
        way for a known and an unknown address.

        Raises:
            RegistrationNotAllowedError: policy refusal; ``str(exc)`` is the
                reason code.
            ValueError: If the email already exists.
        """
        decision = AccessPolicyService.can_register(
            session, email=email, origin=AccountOrigin.SIGNUP
        )
        if not decision.allowed:
            assert decision.reason is not None
            raise RegistrationNotAllowedError(decision.reason)

        existing = UserService.get_user_by_email(session=session, email=email)
        if existing:
            raise ValueError(
                "The user with this email already exists in the system"
            )

        user = UserService.create_account(
            session,
            email=email,
            origin=AccountOrigin.SIGNUP,
            password=password,
            full_name=full_name,
        )
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
        such accounts are ordinary users — they pick up the access policy's
        default role and every downstream gate (agent limits, credential isolation, catalog
        visibility) applies unchanged.

        Deliberately does **not** consult the access policy's registration
        mode or email patterns (``origin="external"`` is ungated): the
        integration's own allowlist is the registration gate, and re-checking
        the signup patterns here would silently break configurations where
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
        # Validate + normalise before the lookup, so the idempotency check
        # below asks about the same address ``create_account`` will store.
        # (``create_account`` normalises again; this is not redundant — the
        # get-or-create contract needs the canonical form *here*, before it
        # decides whether to create at all.)
        try:
            email = _EMAIL_ADAPTER.validate_python(email.strip().lower())
        except PydanticValidationError as exc:
            raise ValueError(f"Invalid email address: {email!r}") from exc

        existing = UserService.get_user_by_email(session=session, email=email)
        if existing:
            return existing

        # Both branches differ only in whether a password hash exists at all;
        # everything else about the account is identical, which is why they
        # go through the one chokepoint instead of one of them building a row.
        user = UserService.create_account(
            session,
            email=email,
            origin=AccountOrigin.EXTERNAL,
            password=None if passwordless else secrets.token_urlsafe(32),
            is_active=True,
        )

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
            ValueError: If token invalid, user not found, user inactive, or
                password auth is off for this user (message
                ``password_auth_disabled``).
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
        # Gated here rather than in the route because this is the only place
        # the token's owner is resolved; the route maps the reason code to a
        # 403. Superusers keep the break-glass path.
        AccessPolicyService.require_password_auth(session, user)
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
        # Password auth off — skip the send SILENTLY, exactly like the
        # cooldown below. Raising here would turn recovery into an oracle for
        # "this address belongs to a superuser", since superusers keep the
        # break-glass path.
        policy = AccessPolicyService.resolve(session)
        if not AccessPolicyService.is_password_auth_allowed(policy, user):
            return
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
