"""MFA enrollment & management API.

All routes under ``/users/me/mfa/*`` require an authenticated user.
Login-time MFA endpoints (``POST /login/mfa/...``) live in
``routes/login.py`` so they can stay anonymous.

See the architectural plan at ``docs/drafts/user-2fa-passkeys-totp_plan.md``
section 5.1.3.  Errors raised by :class:`MfaService` as ``ValueError(code)``
are translated to HTTPException via :func:`translate_mfa_error`
(in :mod:`app.api.routes._mfa_errors`).
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, SessionDep
from app.api.routes._mfa_errors import translate_mfa_error
from app.models import (
    Message,
    MfaStatus,
    RecoveryCodeStatus,
    RecoveryCodesPlaintext,
    StepUpProof,
    TotpEnrollResponse,
    TotpFinishRequest,
    UserPasskeyPublic,
    UserPasskeyUpdate,
    UserPasskeysPublic,
)
from app.services.users.mfa_service import MfaService
from app.services.users.user_service import UserService

router = APIRouter(prefix="/users/me/mfa", tags=["mfa"])


# ── Request bodies (route-local) ──────────────────────────────────────


class PasskeyBeginRequest(BaseModel):
    """Body of ``POST /users/me/mfa/passkeys/begin``.

    Currently has no fields — the nickname is supplied at ``finish``
    time.  Kept as a typed body so the OpenAPI shape stays stable when
    (if) we add fields here later (e.g. ``authenticator_attachment``
    preference).
    """


class PasskeyFinishRequest(BaseModel):
    """Body of ``POST /users/me/mfa/passkeys/finish``."""
    challenge_token: str
    credential: dict
    nickname: str = Field(min_length=1, max_length=64)


class StepUpPasskeyOptions(BaseModel):
    """Response of ``POST /users/me/mfa/step-up/passkey/options`` —
    options JSON plus the ``challenge_token`` to feed into the proof."""
    challenge_token: str
    options: dict


class BeginPasskeyRegistrationResponse(BaseModel):
    """Response of ``POST /users/me/mfa/passkeys/begin``.

    ``options`` is the ``PublicKeyCredentialCreationOptionsJSON`` the
    browser feeds directly into ``navigator.credentials.create()``
    (via ``@simplewebauthn/browser``).  ``challenge_token`` is the opaque
    server-side handle the client echoes back to ``/passkeys/finish``.

    The two are nested rather than merged so the client can pass
    ``options`` straight to the WebAuthn library without accidentally
    leaking the server handle into the spec-defined options object.
    """
    challenge_token: str
    options: dict


# Backwards-compat re-export — keep the historical name available so
# any consumer that imported ``routes.mfa._translate_mfa_error`` keeps
# working.  New call sites should ``from app.api.routes._mfa_errors
# import translate_mfa_error`` directly.
_translate_mfa_error = translate_mfa_error


# ── Schema converters ─────────────────────────────────────────────────


# ── Status & passkey listing ──────────────────────────────────────────


@router.get("/status", response_model=MfaStatus)
def mfa_status(session: SessionDep, current_user: CurrentUser) -> Any:
    """Return whether 2FA is on for the current user and which factors
    are enrolled.  Drives the Settings → Security tab header."""
    passkeys = MfaService.list_passkeys(session=session, user=current_user)
    return MfaStatus(
        enabled=current_user.two_factor_enabled,
        has_passkey=len(passkeys) > 0,
        has_totp=MfaService.has_totp(session=session, user_id=current_user.id),
        has_recovery_codes=MfaService.remaining_recovery_codes(
            session=session, user=current_user
        )
        > 0,
        passkey_count=len(passkeys),
        last_used_at=current_user.two_factor_last_used_at,
        enrolled_at=current_user.two_factor_enrolled_at,
    )


@router.get("/passkeys", response_model=UserPasskeysPublic)
def list_passkeys(session: SessionDep, current_user: CurrentUser) -> Any:
    """List all registered passkeys for the current user."""
    passkeys = MfaService.list_passkeys(session=session, user=current_user)
    return UserPasskeysPublic(
        data=[MfaService.passkey_to_public(p) for p in passkeys], count=len(passkeys)
    )


# ── Passkey enrollment ────────────────────────────────────────────────


@router.post("/passkeys/begin", response_model=BeginPasskeyRegistrationResponse)
def begin_passkey_registration(
    session: SessionDep,
    current_user: CurrentUser,
    body: PasskeyBeginRequest | None = None,
) -> Any:
    """Start the WebAuthn registration ceremony.

    Returns a :class:`BeginPasskeyRegistrationResponse` with the
    ``PublicKeyCredentialCreationOptions`` nested under ``options`` plus
    the ``challenge_token`` the client must echo back to ``finish``.

    The two are kept on separate keys so the frontend can pass ``options``
    straight to ``@simplewebauthn/browser`` without contaminating the
    spec-defined options object with our server-side challenge handle.
    """
    try:
        challenge, options = MfaService.begin_passkey_registration(
            session=session, user=current_user
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return BeginPasskeyRegistrationResponse(
        challenge_token=challenge.challenge_token, options=options
    )


@router.post("/passkeys/finish")
def finish_passkey_registration(
    session: SessionDep, current_user: CurrentUser, body: PasskeyFinishRequest
) -> dict:
    """Verify the WebAuthn attestation and persist the passkey.

    Response: ``{ "passkey": UserPasskeyPublic, "recovery_codes": [...] | None }``
    where ``recovery_codes`` is populated only when this enrollment turns
    2FA on for the first time (so the UI knows to pop the one-shot
    recovery-codes modal).
    """
    try:
        passkey, codes = MfaService.finish_passkey_registration(
            session=session,
            user=current_user,
            challenge_token=body.challenge_token,
            credential=body.credential,
            nickname=body.nickname,
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)

    recovery = (
        RecoveryCodesPlaintext(codes=codes, generated_at=datetime.now(UTC))
        if codes is not None
        else None
    )
    return {
        "passkey": MfaService.passkey_to_public(passkey).model_dump(mode="json"),
        "recovery_codes": recovery.model_dump(mode="json") if recovery else None,
    }


@router.patch("/passkeys/{passkey_id}", response_model=UserPasskeyPublic)
def rename_passkey(
    session: SessionDep,
    current_user: CurrentUser,
    passkey_id: uuid.UUID,
    body: UserPasskeyUpdate,
) -> Any:
    """Rename an owned passkey."""
    try:
        passkey = MfaService.rename_passkey(
            session=session,
            user=current_user,
            passkey_id=passkey_id,
            nickname=body.nickname,
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return MfaService.passkey_to_public(passkey)


@router.delete("/passkeys/{passkey_id}", response_model=Message)
def delete_passkey(
    session: SessionDep, current_user: CurrentUser, passkey_id: uuid.UUID
) -> Any:
    """Delete an owned passkey.

    If the deleted passkey is the user's last remaining 2FA factor
    (no other passkeys, no TOTP), 2FA is automatically turned off via
    the same wipe-and-flag flow as ``POST /mfa/disable`` — the caller
    does not need to make a separate disable call.
    """
    try:
        MfaService.delete_passkey(
            session=session, user=current_user, passkey_id=passkey_id
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return Message(message="Passkey deleted")


# ── TOTP enrollment ───────────────────────────────────────────────────


@router.post("/totp/begin", response_model=TotpEnrollResponse)
def begin_totp_enrollment(session: SessionDep, current_user: CurrentUser) -> Any:
    """Generate a fresh TOTP secret and return the QR + otpauth URI.

    Nothing is persisted yet — the encrypted ``secret_token`` must be
    echoed back to ``/finish`` along with a valid 6-digit code.
    """
    try:
        payload = MfaService.begin_totp_enrollment(
            session=session, user=current_user
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return TotpEnrollResponse(**payload)


@router.post("/totp/finish")
def finish_totp_enrollment(
    session: SessionDep, current_user: CurrentUser, body: TotpFinishRequest
) -> dict:
    """Persist the TOTP secret if the 6-digit code verifies.

    Mirrors :func:`finish_passkey_registration` — when this enrollment
    turns 2FA on for the first time, the response contains a
    ``recovery_codes`` block (one-shot).
    """
    try:
        _row, codes = MfaService.finish_totp_enrollment(
            session=session,
            user=current_user,
            secret_token=body.secret_token,
            code=body.code,
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)

    recovery = (
        RecoveryCodesPlaintext(codes=codes, generated_at=datetime.now(UTC))
        if codes is not None
        else None
    )
    return {
        "message": "TOTP enrolled",
        "recovery_codes": recovery.model_dump(mode="json") if recovery else None,
    }


@router.delete("/totp", response_model=Message)
def disable_totp(
    session: SessionDep, current_user: CurrentUser, proof: StepUpProof
) -> Any:
    """Remove the TOTP secret.  Requires a fresh-factor proof.

    If TOTP is the user's last remaining 2FA factor, 2FA is
    automatically turned off via the same wipe-and-flag flow as
    ``POST /mfa/disable``.
    """
    try:
        MfaService.require_recent_factor(
            session=session, user=current_user, proof=proof
        )
        MfaService.disable_totp(session=session, user=current_user)
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return Message(message="TOTP disabled")


# ── Recovery codes ────────────────────────────────────────────────────


@router.get("/recovery-codes", response_model=RecoveryCodeStatus)
def recovery_codes_status(session: SessionDep, current_user: CurrentUser) -> Any:
    """Return the remaining-count and last-regeneration timestamp.

    Never returns the plaintext codes — those are shown exactly once at
    generation time."""
    return RecoveryCodeStatus(
        remaining_count=MfaService.remaining_recovery_codes(
            session=session, user=current_user
        ),
        total_count=MfaService.total_recovery_codes(
            session=session, user=current_user
        ),
        last_regenerated_at=MfaService.last_recovery_batch_at(
            session=session, user=current_user
        ),
    )


@router.post(
    "/recovery-codes/regenerate", response_model=RecoveryCodesPlaintext
)
def regenerate_recovery_codes(
    session: SessionDep, current_user: CurrentUser, proof: StepUpProof
) -> Any:
    """Wipe the prior batch and mint a fresh set of plaintext recovery
    codes.  Requires a fresh-factor proof."""
    try:
        codes = MfaService.regenerate_recovery_codes_with_step_up(
            session=session, user=current_user, proof=proof
        )
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return RecoveryCodesPlaintext(codes=codes, generated_at=datetime.now(UTC))


# ── Step-up & disable 2FA ─────────────────────────────────────────────


@router.post("/step-up/passkey/options", response_model=StepUpPasskeyOptions)
def begin_step_up_passkey(
    session: SessionDep, current_user: CurrentUser
) -> Any:
    """Issue a passkey step-up challenge.

    The frontend uses this to power the "use my passkey to confirm"
    button on destructive actions (disable 2FA, regenerate recovery
    codes, delete the last passkey).  The returned ``challenge_token``
    must be supplied as ``passkey_challenge_token`` inside the
    destructive request's ``StepUpProof`` body.
    """
    challenge, options = MfaService.begin_step_up_passkey(
        session=session, user=current_user
    )
    return StepUpPasskeyOptions(
        challenge_token=challenge.challenge_token, options=options
    )


@router.post("/disable", response_model=Message)
def disable_two_factor(
    session: SessionDep, current_user: CurrentUser, proof: StepUpProof
) -> Any:
    """Turn off 2FA — wipes ALL factors and the master flag.

    Requires a fresh factor proof; the access token alone is NOT enough.
    """
    try:
        MfaService.require_recent_factor(
            session=session, user=current_user, proof=proof
        )
        UserService.disable_all_factors(session=session, user=current_user)
    except ValueError as exc:
        raise translate_mfa_error(exc)
    return Message(message="Two-factor authentication disabled")
