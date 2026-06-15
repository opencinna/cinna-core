import logging
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm

logger = logging.getLogger(__name__)

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.api.routes._mfa_errors import translate_mfa_error
from app.core import security
from app.core.config import settings
from app.models import (
    ConfirmEmailRequest,
    LoginResponse,
    LoginToken,
    Message,
    MfaChallenge,
    MfaVerifyRequest,
    NewPassword,
    PasskeyAuthOptionsRequest,
    PasskeyAuthOptionsResponse,
    User,
    UserPublic,
)
from app.services.users.email_confirmation_service import EmailConfirmationService
from app.services.users.mfa_service import FIRST_FACTOR_PASSWORD, MfaService
from app.services.users.user_service import UserService
from app.utils import (
    generate_password_reset_token,
    generate_reset_password_email,
)

router = APIRouter(tags=["login"])


# Backwards-compat alias so any existing test that imports the private
# helper still works.  New code should import ``translate_mfa_error``
# from :mod:`app.api.routes._mfa_errors`.
_translate_mfa_error = translate_mfa_error


@router.post("/login/access-token", response_model=LoginResponse)
def login_access_token(
    session: SessionDep,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    trusted_device_token: Annotated[
        str | None, Header(alias="X-Trusted-Device")
    ] = None,
) -> Any:
    """OAuth2-compatible password login.

    Returns a :class:`LoginResponse` discriminated union:

    - :class:`LoginToken` (``kind="token"``)   — straight access token
      when 2FA is off, or when 2FA is on **and** a valid unexpired
      trusted-device token is presented (the "Do not ask on this device"
      skip).
    - :class:`MfaChallenge` (``kind="mfa_challenge"``) — short-lived
      challenge handle when ``user.two_factor_enabled=True`` and no valid
      trusted-device token was presented.  The frontend completes the
      flow via ``POST /login/mfa/verify``.

    The optional ``X-Trusted-Device`` header carries the opaque
    trusted-device token minted at a prior ``/login/mfa/verify``.  Absent
    or forged → normal challenge path (no error, no oracle).
    """
    user = UserService.authenticate(
        session=session, email=form_data.username, password=form_data.password
    )
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    if user.two_factor_enabled:
        # "Do not ask on this device" skip — only attempted when 2FA is
        # on (a non-2FA user has no challenge to skip).  Runs after the
        # password already verified, so it opens no new probing surface.
        if MfaService.consume_trusted_device(
            session=session, user=user, token=trusted_device_token
        ):
            access_token_expires = timedelta(
                minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
            )
            return LoginToken(
                access_token=security.create_access_token(
                    user.id, expires_delta=access_token_expires
                )
            )
        challenge = MfaService.issue_challenge(
            session=session, user=user, first_factor=FIRST_FACTOR_PASSWORD
        )
        return MfaChallenge(
            challenge_token=challenge.challenge_token,
            expires_at=challenge.expires_at,
            allowed_methods=MfaService.allowed_methods_for_user(
                session=session, user=user
            ),
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return LoginToken(
        access_token=security.create_access_token(
            user.id, expires_delta=access_token_expires
        )
    )


@router.post(
    "/login/mfa/passkey/options", response_model=PasskeyAuthOptionsResponse
)
def login_mfa_passkey_options(
    session: SessionDep, body: PasskeyAuthOptionsRequest
) -> Any:
    """Issue WebAuthn assertion options bound to a pending MFA challenge.

    The frontend feeds the returned ``options`` into
    ``navigator.credentials.get()`` and POSTs the resulting assertion to
    ``/login/mfa/verify`` with ``method="passkey"``.  The
    ``PublicKeyCredentialRequestOptionsJSON`` is nested under
    :attr:`PasskeyAuthOptionsResponse.options` so the frontend can pass
    it straight to ``@simplewebauthn/browser``.
    """
    try:
        challenge = MfaService.get_challenge(
            session=session, challenge_token=body.challenge_token
        )
        options = MfaService.begin_passkey_authentication(
            session=session, challenge=challenge
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return PasskeyAuthOptionsResponse(options=options)


@router.post("/login/mfa/verify", response_model=LoginToken)
def login_mfa_verify(
    request: Request, session: SessionDep, body: MfaVerifyRequest
) -> Any:
    """Verify a second-factor proof and issue the final access token.

    Two rate-limit layers fire before any verification work runs:

    * **Anonymous, per-source.** Catches token-spray probes that supply
      fabricated ``challenge_token`` values — these never resolve to a
      user and therefore can't be throttled by the per-user limit.
    * **Per-user (challenge owner).** Caps real verification attempts
      against any one account.

    The per-user limit follows the user (not the network connection) so
    it survives a NAT / proxy swap mid-flow.
    """
    # Pydantic already enforces ``method ∈ {passkey,totp,recovery}`` at
    # the API edge via the ``Literal`` annotation on
    # :class:`MfaVerifyRequest.method`. ``MfaService.verify_challenge``
    # double-checks for non-route callers — both layers belong here.

    # Anonymous per-source limit runs FIRST so a spray attacker with
    # random tokens still gets throttled. ``get_challenge`` would raise
    # on bad tokens without ever attaching to a user, so the per-user
    # limit alone wouldn't catch this.
    source_key = request.client.host if request.client else "unknown"
    try:
        MfaService.check_anonymous_verify_rate_limit(source_key=source_key)
    except ValueError as exc:
        raise translate_mfa_error(exc)

    # Resolve the challenge so we can rate-limit by user id even if the
    # caller is supplying garbage payloads. Bad tokens are logged at
    # warning level — we can't audit-log to ``SecurityEvent`` without a
    # ``user_id``, but the warning lands in server logs for trend spotting.
    try:
        challenge = MfaService.get_challenge(
            session=session, challenge_token=body.challenge_token
        )
    except ValueError as exc:
        if str(exc) in ("challenge_not_found", "challenge_expired", "challenge_consumed"):
            logger.warning(
                "mfa_verify_bad_challenge code=%s source=%s",
                str(exc),
                source_key,
            )
        raise translate_mfa_error(exc)

    challenge_user = session.get(User, challenge.user_id)
    if challenge_user is None:
        logger.warning(
            "mfa_verify_orphan_challenge user_id=%s source=%s",
            challenge.user_id,
            source_key,
        )
        raise translate_mfa_error(ValueError("challenge_not_found"))

    try:
        MfaService.check_verify_rate_limit(session=session, user=challenge_user)
    except ValueError as exc:
        raise translate_mfa_error(exc)

    try:
        user, trusted_device_token = MfaService.verify_challenge(
            session=session,
            challenge_token=body.challenge_token,
            method=body.method,
            payload=body.payload or {},
            remember_device_days=body.remember_device_days,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return LoginToken(
        access_token=security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        trusted_device_token=trusted_device_token,
    )


@router.post("/login/test-token", response_model=UserPublic)
def test_token(current_user: CurrentUser) -> Any:
    """
    Test access token
    """
    return current_user


@router.post("/password-recovery/{email}")
def recover_password(email: str, session: SessionDep) -> Message:
    """
    Password Recovery
    """
    try:
        UserService.recover_password(session=session, email=email)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Message(message="Password recovery email sent")


@router.post("/reset-password/")
def reset_password(session: SessionDep, body: NewPassword) -> Message:
    """
    Reset password
    """
    try:
        UserService.reset_password(
            session=session, token=body.token, new_password=body.new_password
        )
    except ValueError as e:
        detail = str(e)
        if detail == "Invalid token":
            raise HTTPException(status_code=400, detail=detail)
        elif detail == "Inactive user":
            raise HTTPException(status_code=400, detail=detail)
        else:
            raise HTTPException(status_code=404, detail=detail)
    return Message(message="Password updated successfully")


@router.post("/confirm-email/")
def confirm_email(session: SessionDep, body: ConfirmEmailRequest) -> Message:
    """
    Confirm an email address from the token in the confirmation link.

    Public, token-bearing, idempotent — confirming an already-confirmed
    user returns success without resending anything.
    """
    try:
        EmailConfirmationService.confirm_email(session=session, token=body.token)
    except ValueError as e:
        detail = str(e)
        if detail == "Invalid token":
            raise HTTPException(status_code=400, detail=detail)
        if detail == "Inactive user":
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=404, detail=detail)
    return Message(message="Email confirmed successfully")


@router.post("/resend-confirmation/{email}")
def resend_confirmation(email: str, session: SessionDep) -> Message:
    """
    Public, by-email resend of the confirmation email.

    Non-enumerating: always returns the same generic success message
    whether or not the email exists / is already confirmed / is in
    cooldown. The actual send (if any) happens silently server-side.
    """
    EmailConfirmationService.resend_confirmation(session=session, email=email)
    return Message(message="If the email is registered and unconfirmed, a confirmation email has been sent")


@router.post(
    "/password-recovery-html-content/{email}",
    dependencies=[Depends(get_current_active_superuser)],
    response_class=HTMLResponse,
)
def recover_password_html_content(email: str, session: SessionDep) -> Any:
    """
    HTML Content for Password Recovery
    """
    user = UserService.get_user_by_email(session=session, email=email)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="The user with this username does not exist in the system.",
        )
    password_reset_token = generate_password_reset_token(email=email)
    email_data = generate_reset_password_email(
        email_to=user.email, email=email, token=password_reset_token
    )

    return HTMLResponse(
        content=email_data.html_content, headers={"subject:": email_data.subject}
    )
