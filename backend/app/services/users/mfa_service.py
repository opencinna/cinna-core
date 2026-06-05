"""MFA Service — Two-Factor Authentication business logic.

Implements the enrollment, verification, and login challenge flows for
WebAuthn passkeys and TOTP authenticator apps, plus single-use recovery
codes.  See the architectural plan at
``docs/drafts/user-2fa-passkeys-totp_plan.md`` and
``docs/development/backend/backend_development_llm.md`` for the broader
patterns this service follows (ValueError-on-failure error contract,
``SessionDep`` / ``CurrentUser`` injection, security-event audit trail).

Error contract:
    Every public method raises ``ValueError(code)`` on failure where
    ``code`` is one of the short stable strings consumed by the route
    layer and mapped to HTTP status codes:

    * ``invalid_code``                    → 400
    * ``challenge_expired``               → 410
    * ``challenge_not_found``             → 404
    * ``challenge_consumed``              → 410
    * ``attempt_limit_exceeded``          → 429
    * ``step_up_required``                → 401
    * ``factor_not_enrolled``             → 404
    * ``totp_already_enrolled``           → 409
    * ``passkey_not_found``               → 404
    * ``invalid_secret_token``            → 400
    * ``invalid_assertion``               → 400
"""
from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, UTC
from typing import Any

import pyotp
import qrcode
import qrcode.image.svg
from sqlalchemy import func
from sqlmodel import Session, delete, select
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import settings
from app.core.security import (
    decrypt_field,
    encrypt_field,
    get_password_hash,
    verify_password,
)
from app.models import (
    SecurityEventCreate,
    StepUpProof,
    User,
    UserMfaChallenge,
    UserPasskey,
    UserPasskeyPublic,
    UserRecoveryCode,
    UserTotpSecret,
    UserTrustedDevice,
)
from app.models.events import security_event as security_event_constants
from app.services.users.user_service import UserService

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────
# Recovery-code character set excludes ambiguous glyphs (no 0/O, 1/I/l).
_RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# How long step-up challenges live (separate notion from login challenges,
# but we reuse the same table for simplicity — short TTL).
_STEP_UP_TTL_SECONDS = 120
# Allowed values for ``UserMfaChallenge.first_factor``.
FIRST_FACTOR_PASSWORD = "password"
FIRST_FACTOR_GOOGLE = "google_oauth"
FIRST_FACTOR_STEP_UP = "step_up"
# TOTP enrollment handle (``secret_token``) lifetime.
_TOTP_ENROLLMENT_TTL_SECONDS = 600

# Per-user soft rate-limit on ``POST /login/mfa/verify`` — 10 verifications
# per 5 minutes. In-memory token bucket; resets on process restart, which
# is acceptable for the MVP (a real attacker who can restart the API
# already wins). See plan §4.4.
_VERIFY_RATE_LIMIT_MAX = 10
_VERIFY_RATE_LIMIT_WINDOW_SECONDS = 300
# Mapping ``user_id -> list[unix_timestamp]`` of recent verify attempts.
_verify_rate_limit_log: dict[uuid.UUID, list[float]] = {}
# Triggers an opportunistic sweep of stale-bucket keys from
# ``_verify_rate_limit_log`` when the dict grows past this size.  Keeps
# the in-memory footprint bounded for instances with many one-shot users.
_RATE_LIMIT_SWEEP_THRESHOLD = 1024
# Companion per-source rate limit for the anonymous bad-token branch of
# ``/login/mfa/verify``.  Tighter than the per-user limit because the
# attacker doesn't have a real user to be throttled by — and we want
# spray-prober traffic to die quickly without polluting the per-user
# audit trail.  Same in-memory caveat as above.
_ANONYMOUS_VERIFY_RATE_LIMIT_MAX = 20
_ANONYMOUS_VERIFY_RATE_LIMIT_WINDOW_SECONDS = 300
_anonymous_verify_rate_limit_log: dict[str, list[float]] = {}


def _pyotp_digest(algorithm: str) -> Any:
    """Translate the TOTP ``algorithm`` column value to the ``digest=``
    argument expected by :class:`pyotp.TOTP`.

    Defaults to SHA1 (the RFC-6238 standard) for unknown values.
    """
    import hashlib

    table = {
        "SHA1": hashlib.sha1,
        "SHA256": hashlib.sha256,
        "SHA512": hashlib.sha512,
    }
    return table.get((algorithm or "SHA1").upper(), hashlib.sha1)


class MfaService:
    """Business logic for two-factor authentication.

    Stateless — all methods accept an explicit ``session``.  Callers are
    expected to be the FastAPI route layer; the service raises
    ``ValueError`` on failure and the routes translate to
    ``HTTPException``.
    """

    # ── Challenge lifecycle (post-first-factor) ────────────────────────

    @staticmethod
    def issue_challenge(
        *, session: Session, user: User, first_factor: str
    ) -> UserMfaChallenge:
        """Create a fresh 2FA challenge after the user has cleared the
        first authentication factor.

        The returned ``challenge_token`` is the opaque handle the client
        echoes back on ``POST /login/mfa/verify`` (and on the passkey
        options call).  Single-use — verifying it sets ``consumed_at``.
        """
        now = datetime.now(UTC)
        challenge = UserMfaChallenge(
            user_id=user.id,
            challenge_token=secrets.token_urlsafe(32),
            first_factor=first_factor,
            expires_at=now + timedelta(seconds=settings.MFA_CHALLENGE_TTL_SECONDS),
        )
        session.add(challenge)
        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_CHALLENGE_ISSUED,
            severity="low",
            details={"first_factor": first_factor},
        )
        session.commit()
        session.refresh(challenge)
        return challenge

    @staticmethod
    def get_challenge(*, session: Session, challenge_token: str) -> UserMfaChallenge:
        """Load a challenge by its opaque token and enforce lifecycle.

        Raises:
            ValueError(``challenge_not_found``) — unknown token.
            ValueError(``challenge_expired``)   — past ``expires_at``.
            ValueError(``challenge_consumed``)  — already verified.
            ValueError(``attempt_limit_exceeded``) — over the per-challenge cap.
        """
        if not challenge_token or not isinstance(challenge_token, str):
            raise ValueError("challenge_not_found")
        stmt = select(UserMfaChallenge).where(
            UserMfaChallenge.challenge_token == challenge_token
        )
        challenge = session.exec(stmt).first()
        if challenge is None:
            raise ValueError("challenge_not_found")
        if challenge.consumed_at is not None:
            raise ValueError("challenge_consumed")
        expires = _ensure_utc(challenge.expires_at)
        if expires < datetime.now(UTC):
            raise ValueError("challenge_expired")
        if challenge.attempts >= settings.MFA_MAX_ATTEMPTS_PER_CHALLENGE:
            raise ValueError("attempt_limit_exceeded")
        return challenge

    @staticmethod
    def _consume_challenge(session: Session, challenge: UserMfaChallenge) -> None:
        """Mark the challenge as fully consumed (verified)."""
        challenge.consumed_at = datetime.now(UTC)
        session.add(challenge)

    @staticmethod
    def _record_failed_attempt(
        session: Session, challenge: UserMfaChallenge
    ) -> None:
        """Bump the per-challenge failed-attempt counter and persist."""
        challenge.attempts += 1
        session.add(challenge)
        session.commit()
        session.refresh(challenge)

    @staticmethod
    def _log_challenge_lookup_failure(
        *,
        session: Session,
        challenge_token: str,
        method: str,
        reason: str,
    ) -> None:
        """Audit a verify attempt that died at the challenge lookup step.

        We cannot bump ``attempts`` on a row that may not exist or is
        already consumed, but the security trail still wants to know
        someone tried.  When the token resolves to a user row (even an
        expired/consumed challenge) we log under that ``user_id``;
        otherwise we silently swallow the audit (no row to attribute it
        to — the actual ``ValueError`` is already raised to the route).
        """
        if not challenge_token or not isinstance(challenge_token, str):
            return
        stmt = select(UserMfaChallenge).where(
            UserMfaChallenge.challenge_token == challenge_token
        )
        challenge = session.exec(stmt).first()
        if challenge is None:
            return
        user = session.get(User, challenge.user_id)
        if user is None:
            return
        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_CHALLENGE_FAILED,
            severity="medium",
            details={"method": method, "reason": reason},
        )

    @staticmethod
    def verify_challenge(
        *,
        session: Session,
        challenge_token: str,
        method: str,
        payload: dict,
        remember_device_days: int | None = None,
        user_agent: str | None = None,
    ) -> tuple[User, str | None]:
        """Verify a second-factor proof against a pending login challenge.

        Returns ``(user, trusted_device_token_or_None)`` on success and
        marks the challenge consumed.  When ``remember_device_days`` is
        set, a :class:`UserTrustedDevice` row is minted inside the same
        transaction and its plaintext token is returned (once) so the
        route can hand it back on the ``LoginToken``.  The route layer
        then issues the access token.

        Args:
            session: DB session.
            challenge_token: The token returned by ``issue_challenge``.
            method: ``"passkey"`` | ``"totp"`` | ``"recovery"``.
            payload: Method-specific dict (see plan §5.1.1).
            remember_device_days: ``1`` / ``7`` / ``30`` to register a
                trusted device, or ``None`` to skip device registration.
            user_agent: Best-effort device label captured at the route
                edge; stored on the trusted-device row for display.

        Raises:
            ValueError: Any of the codes documented at the module level,
                plus ``invalid_trust_duration`` when
                ``remember_device_days`` is outside the allowlist.
        """
        # Validate method membership BEFORE loading the challenge so a
        # malformed body doesn't bump ``attempts`` against a perfectly
        # good challenge row.
        if method not in {"passkey", "totp", "recovery"}:
            raise ValueError("invalid_method")

        # Load the challenge.  When ``get_challenge`` raises
        # ``challenge_expired`` / ``challenge_consumed`` / ``challenge_not_found``
        # we still want a ``MFA_CHALLENGE_FAILED`` audit entry, but we
        # cannot bump ``attempts`` on a row that may not exist or is
        # already consumed.  Best-effort user lookup for the audit log.
        try:
            challenge = MfaService.get_challenge(
                session=session, challenge_token=challenge_token
            )
        except ValueError as exc:
            MfaService._log_challenge_lookup_failure(
                session=session,
                challenge_token=challenge_token,
                method=method,
                reason=str(exc),
            )
            session.commit()
            raise
        user = session.get(User, challenge.user_id)
        if user is None or not user.is_active:
            raise ValueError("challenge_not_found")

        try:
            if method == "passkey":
                MfaService._verify_passkey_login(
                    session=session, challenge=challenge, user=user, payload=payload
                )
            elif method == "totp":
                code = (payload or {}).get("code", "")
                if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
                    raise ValueError("invalid_code")
                if not MfaService.verify_totp(
                    session=session, user=user, code=code
                ):
                    raise ValueError("invalid_code")
            else:  # method == "recovery"
                code = (payload or {}).get("code", "")
                if not isinstance(code, str):
                    raise ValueError("invalid_code")
                if not MfaService.consume_recovery_code(
                    session=session, user=user, code=code
                ):
                    raise ValueError("invalid_code")
        except ValueError as exc:
            # Bump the per-challenge attempts counter AND write the audit
            # row inside the same commit boundary so the two cannot drift
            # (see code-review finding #13).
            challenge.attempts += 1
            session.add(challenge)
            MfaService._log_event(
                session=session,
                user=user,
                event_type=security_event_constants.MFA_CHALLENGE_FAILED,
                severity="medium",
                details={"method": method, "reason": str(exc)},
            )
            session.commit()
            session.refresh(challenge)
            raise

        # Success path.
        user.two_factor_last_used_at = datetime.now(UTC)
        session.add(user)
        MfaService._consume_challenge(session, challenge)
        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_CHALLENGE_SUCCESS,
            severity="low",
            details={"method": method, "first_factor": challenge.first_factor},
        )

        # Optionally register a trusted device so this browser can skip
        # the challenge for the chosen window.  Runs in the SAME
        # transaction as the challenge-consume + success event: if the
        # duration is invalid (non-route caller) everything rolls back
        # together and no access token is issued.
        trusted_device_token: str | None = None
        if remember_device_days is not None:
            trusted_device_token = MfaService.register_trusted_device(
                session=session,
                user=user,
                days=remember_device_days,
                label=user_agent,
            )

        session.commit()
        session.refresh(user)
        return user, trusted_device_token

    # ── WebAuthn — registration (enrollment) ───────────────────────────

    @staticmethod
    def begin_passkey_registration(
        *, session: Session, user: User
    ) -> tuple[UserMfaChallenge, dict]:
        """Generate ``PublicKeyCredentialCreationOptions`` and persist a
        transient challenge row so we can verify the response later.

        The nickname is supplied to ``finish_passkey_registration``, not
        here — there is no use for it during ``begin``.

        Returns:
            Tuple ``(challenge_row, options_dict)``.  The route serialises
            ``options_dict`` and the client passes the ``challenge_token``
            back to ``finish_passkey_registration``.
        """
        # Exclude credentials already registered to this user to avoid
        # silent overwrites — the authenticator will refuse to register
        # the same key twice when its ID is in ``exclude_credentials``.
        existing = MfaService._list_user_passkeys(session, user.id)
        exclude_credentials = [
            PublicKeyCredentialDescriptor(id=p.credential_id) for p in existing
        ]

        options = generate_registration_options(
            rp_id=settings.mfa_webauthn_rp_id,
            rp_name=settings.MFA_WEBAUTHN_RP_NAME,
            user_id=str(user.id).encode("utf-8"),
            user_name=user.email,
            user_display_name=user.full_name or user.email,
            exclude_credentials=exclude_credentials,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            supported_pub_key_algs=[
                COSEAlgorithmIdentifier.ECDSA_SHA_256,
                COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
            ],
        )

        challenge = UserMfaChallenge(
            user_id=user.id,
            challenge_token=secrets.token_urlsafe(32),
            webauthn_challenge=bytes(options.challenge),
            first_factor=FIRST_FACTOR_STEP_UP,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=settings.MFA_CHALLENGE_TTL_SECONDS),
        )
        session.add(challenge)
        session.commit()
        session.refresh(challenge)

        options_dict = json.loads(options_to_json(options))
        return challenge, options_dict

    @staticmethod
    def finish_passkey_registration(
        *,
        session: Session,
        user: User,
        challenge_token: str,
        credential: dict,
        nickname: str,
    ) -> tuple[UserPasskey, list[str] | None]:
        """Verify a WebAuthn attestation and persist the new
        :class:`UserPasskey` row.  Flips ``User.two_factor_enabled=True``
        when this is the first factor.

        Returns ``(passkey, recovery_codes_or_None)``: ``recovery_codes``
        is a fresh plaintext batch when this enrollment turns 2FA on for
        the first time, ``None`` otherwise.  The decision lives here (not
        in the route) so any caller of this method gets the same
        behavior — the routes are just serialisers.
        """
        challenge = MfaService.get_challenge(
            session=session, challenge_token=challenge_token
        )
        if challenge.user_id != user.id or challenge.webauthn_challenge is None:
            raise ValueError("challenge_not_found")

        try:
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=bytes(challenge.webauthn_challenge),
                expected_origin=settings.mfa_webauthn_expected_origin,
                expected_rp_id=settings.mfa_webauthn_rp_id,
            )
        except Exception as exc:
            logger.warning(
                "Passkey registration verification failed for user %s: %s",
                user.id,
                exc,
            )
            MfaService._record_failed_attempt(session, challenge)
            MfaService._log_event(
                session=session,
                user=user,
                event_type=security_event_constants.MFA_CHALLENGE_FAILED,
                severity="medium",
                details={"flow": "passkey_register", "error": str(exc)},
            )
            session.commit()
            raise ValueError("invalid_assertion")

        device_type = _device_type_from_credential(credential)
        backed_up = _backed_up_from_credential(credential)
        transports = _transports_from_credential(credential)
        aaguid = _aaguid_from_verification(verification)

        passkey = UserPasskey(
            user_id=user.id,
            credential_id=bytes(verification.credential_id),
            public_key=bytes(verification.credential_public_key),
            sign_count=int(verification.sign_count or 0),
            transports=json.dumps(transports),
            aaguid=aaguid,
            nickname=(nickname or "Passkey")[:64],
            device_type=device_type,
            backed_up=backed_up,
        )
        session.add(passkey)
        MfaService._consume_challenge(session, challenge)

        # Flip the master switch and possibly issue recovery codes.
        first_factor_just_added = not user.two_factor_enabled
        MfaService._mark_factor_enrolled(session, user)

        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_ENROLLED,
            severity="medium",
            details={"factor": "passkey", "first_factor": first_factor_just_added},
        )
        session.commit()
        session.refresh(passkey)

        recovery_codes: list[str] | None = None
        if first_factor_just_added:
            recovery_codes = MfaService.generate_recovery_codes(
                session=session, user=user
            )
        return passkey, recovery_codes

    # ── WebAuthn — authentication (login & step-up) ────────────────────

    @staticmethod
    def begin_passkey_authentication(
        *, session: Session, challenge: UserMfaChallenge
    ) -> dict:
        """Generate ``PublicKeyCredentialRequestOptions`` bound to a
        pending login or step-up challenge.

        Once a ``webauthn_challenge`` nonce has been minted for the row
        it is FROZEN — subsequent calls return the same nonce.  Without
        this, a user opening two tabs would race: the second tab would
        overwrite the first tab's nonce and force the first tab's
        authenticator dialog to fail verification.

        Rejects users who have no passkeys with ``factor_not_enrolled``
        rather than emitting an ``allow_credentials=[]`` options object
        (which WebAuthn interprets as "any credential is allowed" —
        nonsense for the login challenge).
        """
        user = session.get(User, challenge.user_id)
        if user is None:
            raise ValueError("challenge_not_found")
        passkeys = MfaService._list_user_passkeys(session, user.id)
        if not passkeys:
            raise ValueError("factor_not_enrolled")
        allow_credentials = [
            PublicKeyCredentialDescriptor(id=p.credential_id) for p in passkeys
        ]
        if challenge.webauthn_challenge is not None:
            # Reissue the same options bound to the existing nonce so two
            # concurrent tabs don't trample each other.
            options = generate_authentication_options(
                rp_id=settings.mfa_webauthn_rp_id,
                challenge=bytes(challenge.webauthn_challenge),
                allow_credentials=allow_credentials,
                user_verification=UserVerificationRequirement.PREFERRED,
            )
            return json.loads(options_to_json(options))

        options = generate_authentication_options(
            rp_id=settings.mfa_webauthn_rp_id,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        challenge.webauthn_challenge = bytes(options.challenge)
        session.add(challenge)
        session.commit()
        return json.loads(options_to_json(options))

    @staticmethod
    def _verify_passkey_login(
        *,
        session: Session,
        challenge: UserMfaChallenge,
        user: User,
        payload: dict,
    ) -> UserPasskey:
        """Verify a WebAuthn assertion during the login MFA step.

        Updates ``sign_count`` / ``last_used_at`` on the matched passkey
        and emits :data:`MFA_SIGN_COUNT_REGRESSION` when the counter
        regresses (possible clone).
        """
        if challenge.webauthn_challenge is None:
            raise ValueError("invalid_assertion")
        if not isinstance(payload, dict):
            raise ValueError("invalid_assertion")

        credential_id_b64 = payload.get("id") or payload.get("rawId")
        if not credential_id_b64:
            raise ValueError("invalid_assertion")
        try:
            credential_id = _b64url_decode(credential_id_b64)
        except Exception:
            raise ValueError("invalid_assertion")

        stmt = select(UserPasskey).where(
            UserPasskey.user_id == user.id,
            UserPasskey.credential_id == credential_id,
        )
        passkey = session.exec(stmt).first()
        if passkey is None:
            raise ValueError("invalid_assertion")

        try:
            verification = verify_authentication_response(
                credential=payload,
                expected_challenge=bytes(challenge.webauthn_challenge),
                expected_origin=settings.mfa_webauthn_expected_origin,
                expected_rp_id=settings.mfa_webauthn_rp_id,
                credential_public_key=bytes(passkey.public_key),
                credential_current_sign_count=int(passkey.sign_count),
            )
        except Exception as exc:
            logger.warning(
                "Passkey authentication verification failed for user %s: %s",
                user.id,
                exc,
            )
            # Distinguish RP/origin mismatch vs generic failure so the
            # operator can spot mis-deployments.
            if "origin" in str(exc).lower() or "rp" in str(exc).lower():
                MfaService._log_event(
                    session=session,
                    user=user,
                    event_type=security_event_constants.MFA_PASSKEY_INVALID_ORIGIN,
                    severity="high",
                    details={"error": str(exc)},
                )
            raise ValueError("invalid_assertion")

        new_count = int(verification.new_sign_count or 0)
        if new_count < passkey.sign_count and passkey.sign_count > 0:
            # Possible cloned authenticator — record but do not block.
            MfaService._log_event(
                session=session,
                user=user,
                event_type=security_event_constants.MFA_SIGN_COUNT_REGRESSION,
                severity="high",
                details={
                    "passkey_id": str(passkey.id),
                    "old_sign_count": passkey.sign_count,
                    "new_sign_count": new_count,
                },
            )
        passkey.sign_count = new_count
        passkey.last_used_at = datetime.now(UTC)
        session.add(passkey)
        return passkey

    # ── TOTP ───────────────────────────────────────────────────────────

    @staticmethod
    def begin_totp_enrollment(*, session: Session, user: User) -> dict:
        """Generate a fresh TOTP secret and return the enrollment payload.

        Raises ``totp_already_enrolled`` when the user already has a TOTP
        secret — otherwise we'd silently hand out a second QR/handle that
        the user could complete only by overwriting their working secret
        (and ``finish_totp_enrollment`` would reject it).

        Nothing is persisted server-side — the secret travels back to
        ``finish_totp_enrollment`` packed into ``secret_token``: a Fernet
        envelope around a JSON blob bound to the user's id and stamped
        with an expiry.  ``finish`` re-decodes, asserts the user binding,
        then accepts only a fresh valid code before committing.
        """
        if MfaService.has_totp(session=session, user_id=user.id):
            raise ValueError("totp_already_enrolled")
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret, issuer=settings.MFA_TOTP_ISSUER)
        otpauth_uri = totp.provisioning_uri(
            name=user.email, issuer_name=settings.MFA_TOTP_ISSUER
        )
        qr_svg_data_uri = _render_qr_svg_data_uri(otpauth_uri)
        # Bind {user_id, secret, exp, nonce} so the handle is unusable on a
        # different account and expires after ``_TOTP_ENROLLMENT_TTL_SECONDS``.
        payload = {
            "user_id": str(user.id),
            "secret": secret,
            "exp": int(
                (
                    datetime.now(UTC)
                    + timedelta(seconds=_TOTP_ENROLLMENT_TTL_SECONDS)
                ).timestamp()
            ),
            "nonce": secrets.token_urlsafe(16),
        }
        secret_token = encrypt_field(json.dumps(payload))
        return {
            "secret_base32": secret,
            "otpauth_uri": otpauth_uri,
            "qr_svg_data_uri": qr_svg_data_uri,
            "secret_token": secret_token,
        }

    @staticmethod
    def finish_totp_enrollment(
        *, session: Session, user: User, secret_token: str, code: str
    ) -> tuple[UserTotpSecret, list[str] | None]:
        """Persist the TOTP secret after the user proves it works.

        Returns ``(row, recovery_codes_or_None)`` — codes are a fresh
        plaintext batch when this enrollment turns 2FA on for the first
        time, else ``None``. Mirrors :meth:`finish_passkey_registration`
        so the route layer is a pure serialiser.

        Raises:
            ValueError(``totp_already_enrolled``) — single-secret per user.
            ValueError(``invalid_secret_token``) — token tampering / wrong
                user binding / expired handle.
            ValueError(``invalid_code``)         — 6-digit code didn't verify.
        """
        existing = session.exec(
            select(UserTotpSecret).where(UserTotpSecret.user_id == user.id)
        ).first()
        if existing is not None:
            raise ValueError("totp_already_enrolled")

        if not isinstance(secret_token, str) or not secret_token:
            raise ValueError("invalid_secret_token")
        try:
            decoded = decrypt_field(secret_token)
            payload = json.loads(decoded)
        except Exception:
            raise ValueError("invalid_secret_token")
        if not isinstance(payload, dict):
            raise ValueError("invalid_secret_token")
        token_user_id = payload.get("user_id")
        token_secret = payload.get("secret")
        token_exp = payload.get("exp")
        if (
            not isinstance(token_user_id, str)
            or not isinstance(token_secret, str)
            or not isinstance(token_exp, int)
        ):
            raise ValueError("invalid_secret_token")
        # User binding — handle minted for one user cannot finish for another.
        if token_user_id != str(user.id):
            raise ValueError("invalid_secret_token")
        # TTL — refuse stale handles.
        if token_exp < int(datetime.now(UTC).timestamp()):
            raise ValueError("invalid_secret_token")
        secret = token_secret

        if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
            raise ValueError("invalid_code")

        # New row uses the default algorithm/digits/period — but use them
        # explicitly so we exercise the same plumbing ``verify_totp`` does.
        totp = pyotp.TOTP(
            secret,
            digits=6,
            interval=30,
            digest=_pyotp_digest("SHA1"),
        )
        if not totp.verify(code, valid_window=1):
            raise ValueError("invalid_code")

        # Record the exact step the enrollment code corresponds to —
        # not just "now" — so the user's follow-up first login (which
        # usually happens inside the same 30 s window) accepts a fresh
        # code as soon as the step rolls over.  Replay of the exact
        # enrollment code is still rejected because its step is ≤
        # ``last_used_step``.
        now_ts = int(datetime.now(UTC).timestamp())
        accepted_enroll_step: int | None = None
        for offset in (-1, 0, 1):
            if totp.at(now_ts, counter_offset=offset) == code:
                accepted_enroll_step = (now_ts // 30) + offset
                break
        row = UserTotpSecret(
            user_id=user.id,
            secret_encrypted=encrypt_field(secret),
            last_used_step=accepted_enroll_step,
        )
        session.add(row)
        first_factor_just_added = not user.two_factor_enabled
        MfaService._mark_factor_enrolled(session, user)
        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_ENROLLED,
            severity="medium",
            details={"factor": "totp", "first_factor": first_factor_just_added},
        )
        session.commit()
        session.refresh(row)
        recovery_codes: list[str] | None = None
        if first_factor_just_added:
            recovery_codes = MfaService.generate_recovery_codes(
                session=session, user=user
            )
        return row, recovery_codes

    @staticmethod
    def verify_totp(*, session: Session, user: User, code: str) -> bool:
        """Verify a 6-digit TOTP code against the stored secret.

        Honours the ``algorithm`` / ``digits`` / ``period`` columns so
        rows minted with non-default parameters continue to work.
        Updates ``last_used_step`` / ``last_used_at`` on success and
        rejects replay within the same step.

        ``last_used_step`` is flushed to the session immediately so that
        replay rejection holds even if a subsequent call in the same
        request raises before its own commit.

        Returns:
            ``True`` on a valid, non-replayed code.  ``False`` otherwise.
        """
        row = session.exec(
            select(UserTotpSecret).where(UserTotpSecret.user_id == user.id)
        ).first()
        if row is None:
            return False
        if (
            not isinstance(code, str)
            or not code.isdigit()
            or len(code) != row.digits
        ):
            return False
        try:
            secret = decrypt_field(row.secret_encrypted)
        except Exception:
            return False

        totp = pyotp.TOTP(
            secret,
            digits=row.digits,
            interval=row.period,
            digest=_pyotp_digest(row.algorithm),
        )
        # Accept current step ± 1 (clock skew of one period).
        now = datetime.now(UTC)
        current_step = int(now.timestamp() // row.period)
        accepted_step: int | None = None
        # ``pyotp.TOTP.at`` expects a unix timestamp + counter offset, not
        # a raw step counter, so we anchor on ``now`` and pass the offset.
        for offset in (-1, 0, 1):
            if totp.at(int(now.timestamp()), counter_offset=offset) == code:
                accepted_step = current_step + offset
                break
        if accepted_step is None:
            return False
        if row.last_used_step is not None and accepted_step <= row.last_used_step:
            # Replay within the valid window.
            return False
        row.last_used_step = accepted_step
        row.last_used_at = now
        session.add(row)
        # Flush so the new ``last_used_step`` lands in the DB even if the
        # caller raises before committing the broader transaction. The
        # caller is still responsible for the final commit.
        session.flush()
        return True

    # ── Recovery codes ─────────────────────────────────────────────────

    @staticmethod
    def regenerate_recovery_codes_with_step_up(
        *, session: Session, user: User, proof: StepUpProof
    ) -> list[str]:
        """Wipe the prior batch and mint a fresh set after verifying a
        step-up proof.

        Enforces the precondition order callers care about:

        1. 2FA must be enrolled — raise ``factor_not_enrolled`` early.
           If we ran ``require_recent_factor`` first, the user would get
           a confusing step-up error instead of the actionable "you
           haven't enrolled yet" message.
        2. The step-up proof must verify (raises ``step_up_required``).
        3. Generate the new batch (delegates to
           :meth:`generate_recovery_codes` with ``require_enrolled=True``
           so the precondition is re-checked at the persistence layer).
        """
        if not user.two_factor_enabled:
            raise ValueError("factor_not_enrolled")
        MfaService.require_recent_factor(
            session=session, user=user, proof=proof
        )
        return MfaService.generate_recovery_codes(
            session=session, user=user, require_enrolled=True
        )

    @staticmethod
    def generate_recovery_codes(
        *, session: Session, user: User, require_enrolled: bool = False
    ) -> list[str]:
        """Wipe any prior batch, mint a fresh set, and return the plain
        text codes (one-shot).  Persisted as bcrypt hashes.

        Args:
            session: DB session.
            user: Owner of the codes.
            require_enrolled: When ``True`` (used by the user-facing
                ``regenerate`` endpoint) raise ``factor_not_enrolled`` if
                the user has no second factor.  The enrollment-finish
                flows pass ``False`` because they invoke this BEFORE
                ``two_factor_enabled`` is observable on the session.
        """
        if require_enrolled and not user.two_factor_enabled:
            raise ValueError("factor_not_enrolled")
        # Wipe prior batch — single SQL DELETE on (user_id).
        session.exec(
            delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id)
        )
        codes: list[str] = []
        batch_id = uuid.uuid4()
        for _ in range(settings.MFA_RECOVERY_CODE_COUNT):
            raw = _generate_recovery_code(settings.MFA_RECOVERY_CODE_LENGTH)
            codes.append(raw)
            normalised = _normalise_recovery_code(raw)
            session.add(
                UserRecoveryCode(
                    user_id=user.id,
                    code_hash=get_password_hash(normalised),
                    batch_id=batch_id,
                )
            )
        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_RECOVERY_CODES_REGENERATED,
            severity="medium",
            details={"count": settings.MFA_RECOVERY_CODE_COUNT},
        )
        session.commit()
        return codes

    @staticmethod
    def consume_recovery_code(
        *, session: Session, user: User, code: str
    ) -> bool:
        """Consume a single recovery code.  Returns ``True`` if the code
        was valid and unused, ``False`` otherwise.
        """
        normalised = _normalise_recovery_code(code)
        if not normalised:
            return False
        stmt = select(UserRecoveryCode).where(
            UserRecoveryCode.user_id == user.id,
            UserRecoveryCode.used_at.is_(None),  # type: ignore[union-attr]
        )
        unused = list(session.exec(stmt))
        for row in unused:
            if verify_password(normalised, row.code_hash):
                row.used_at = datetime.now(UTC)
                session.add(row)
                MfaService._log_event(
                    session=session,
                    user=user,
                    event_type=security_event_constants.MFA_RECOVERY_CODE_USED,
                    severity="medium",
                    details={"recovery_code_id": str(row.id)},
                )
                session.commit()
                return True
        return False

    @staticmethod
    def remaining_recovery_codes(*, session: Session, user: User) -> int:
        """Count unused recovery codes for the Settings tab UI."""
        stmt = select(UserRecoveryCode).where(
            UserRecoveryCode.user_id == user.id,
            UserRecoveryCode.used_at.is_(None),  # type: ignore[union-attr]
        )
        return len(list(session.exec(stmt)))

    @staticmethod
    def total_recovery_codes(*, session: Session, user: User) -> int:
        """Count every recovery code in the user's current batch.

        Regeneration wipes the prior batch before inserting the new one,
        so this matches the count of the most recent
        :meth:`generate_recovery_codes` call.  The Settings UI uses it
        to render an "N of M remaining" badge.
        """
        total = session.exec(
            select(func.count(UserRecoveryCode.id)).where(
                UserRecoveryCode.user_id == user.id
            )
        ).one()
        return int(total or 0)

    @staticmethod
    def last_recovery_batch_at(
        *, session: Session, user: User
    ) -> datetime | None:
        """When was the most recent recovery-code batch generated?"""
        stmt = (
            select(UserRecoveryCode)
            .where(UserRecoveryCode.user_id == user.id)
            .order_by(UserRecoveryCode.created_at.desc())  # type: ignore[union-attr]
        )
        latest = session.exec(stmt).first()
        return latest.created_at if latest else None

    # ── Trusted devices ("Do not ask on this device") ─────────────────

    @staticmethod
    def register_trusted_device(
        *,
        session: Session,
        user: User,
        days: int,
        label: str | None,
    ) -> str:
        """Mint a trusted-device token so this browser can skip the 2FA
        challenge for ``days`` days.

        Stores only a bcrypt hash of an opaque random token (mirrors the
        recovery-code pattern) and returns the plaintext exactly once for
        the caller to surface on the ``LoginToken``.  Does **not** commit
        — the caller (``verify_challenge``) commits as part of its single
        success transaction.

        Raises ``ValueError("invalid_trust_duration")`` when ``days`` is
        not in :pyattr:`Settings.MFA_TRUSTED_DEVICE_ALLOWED_DAYS` — never
        trust an arbitrary client-supplied duration.
        """
        if days not in settings.MFA_TRUSTED_DEVICE_ALLOWED_DAYS:
            raise ValueError("invalid_trust_duration")
        token = secrets.token_urlsafe(32)
        device = UserTrustedDevice(
            user_id=user.id,
            token_hash=get_password_hash(token),
            expires_at=datetime.now(UTC) + timedelta(days=days),
            label=(label[:256] if isinstance(label, str) else None),
        )
        session.add(device)
        # Flush so the row gets its ``id`` for the audit detail without
        # committing — the caller owns the commit.
        session.flush()
        MfaService._log_event(
            session=session,
            user=user,
            event_type=security_event_constants.MFA_TRUSTED_DEVICE_REGISTERED,
            severity="medium",
            details={"days": days, "device_id": str(device.id)},
        )
        return token

    @staticmethod
    def consume_trusted_device(
        *, session: Session, user: User, token: str | None
    ) -> bool:
        """Validate a presented trusted-device ``token`` for ``user``.

        Returns ``True`` (and skips the challenge) when the token matches
        one of the user's live (unexpired) rows; ``False`` otherwise —
        silently, with no error and no oracle, so a forged token is
        indistinguishable from "no token presented".

        Resolution is ``user``-scoped and bcrypt-verifies the candidate
        against each live row (bcrypt salts each hash, so we cannot look
        up by hash directly — exactly the ``consume_recovery_code``
        pattern).  On a match it bumps ``last_used_at``, logs
        ``MFA_TRUSTED_DEVICE_USED``, and commits.
        """
        if not token or not isinstance(token, str):
            return False
        now = datetime.now(UTC)
        stmt = select(UserTrustedDevice).where(
            UserTrustedDevice.user_id == user.id
        )
        for device in session.exec(stmt):
            if _ensure_utc(device.expires_at) <= now:
                continue
            if verify_password(token, device.token_hash):
                device.last_used_at = now
                session.add(device)
                MfaService._log_event(
                    session=session,
                    user=user,
                    event_type=security_event_constants.MFA_TRUSTED_DEVICE_USED,
                    severity="low",
                    details={"device_id": str(device.id)},
                )
                session.commit()
                return True
        return False

    @staticmethod
    def purge_expired_trusted_devices(*, session: Session) -> int:
        """Delete every trusted-device row past its ``expires_at``.

        Called by the hourly cleanup job.  Expired rows are also rejected
        at read time in :meth:`consume_trusted_device`, so this is purely
        housekeeping.  Returns the number of rows deleted.
        """
        result = session.exec(
            delete(UserTrustedDevice).where(
                UserTrustedDevice.expires_at < datetime.now(UTC)
            )
        )
        session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    # ── Step-up re-authentication ──────────────────────────────────────

    @staticmethod
    def require_recent_factor(
        *, session: Session, user: User, proof: dict | Any
    ) -> None:
        """Validate a fresh-factor proof attached to a destructive
        mutation (disable 2FA, delete the last passkey, regenerate
        recovery codes).

        ``proof`` is a dict-like object with exactly one of:

        - ``password`` (str)
        - ``totp_code`` (str)
        - ``passkey_assertion`` (dict) + ``passkey_challenge_token`` (str)

        Raises ``ValueError("step_up_required")`` otherwise.
        """
        if proof is None:
            raise ValueError("step_up_required")
        if not isinstance(proof, dict):
            try:
                proof = proof.model_dump(exclude_unset=False)
            except Exception:
                raise ValueError("step_up_required")

        password = proof.get("password")
        totp_code = proof.get("totp_code")
        passkey_assertion = proof.get("passkey_assertion")
        passkey_challenge_token = proof.get("passkey_challenge_token")

        if password:
            if not user.hashed_password:
                raise ValueError("step_up_required")
            if not verify_password(password, user.hashed_password):
                raise ValueError("step_up_required")
            return

        if totp_code:
            if not MfaService.verify_totp(
                session=session, user=user, code=totp_code
            ):
                raise ValueError("step_up_required")
            return

        if passkey_assertion and passkey_challenge_token:
            try:
                challenge = MfaService.get_challenge(
                    session=session, challenge_token=passkey_challenge_token
                )
            except ValueError:
                raise ValueError("step_up_required")
            if challenge.user_id != user.id or challenge.first_factor != FIRST_FACTOR_STEP_UP:
                raise ValueError("step_up_required")
            try:
                MfaService._verify_passkey_login(
                    session=session,
                    challenge=challenge,
                    user=user,
                    payload=passkey_assertion,
                )
            except ValueError:
                MfaService._record_failed_attempt(session, challenge)
                raise ValueError("step_up_required")
            MfaService._consume_challenge(session, challenge)
            session.commit()
            return

        raise ValueError("step_up_required")

    @staticmethod
    def begin_step_up_passkey(
        *, session: Session, user: User
    ) -> tuple[UserMfaChallenge, dict]:
        """Create a step-up challenge bound to ``user`` and return the
        WebAuthn assertion options the client must complete.

        The returned ``challenge_token`` is sent back inside the proof
        body of the destructive mutation (``passkey_challenge_token``).
        """
        challenge = UserMfaChallenge(
            user_id=user.id,
            challenge_token=secrets.token_urlsafe(32),
            first_factor=FIRST_FACTOR_STEP_UP,
            expires_at=datetime.now(UTC) + timedelta(seconds=_STEP_UP_TTL_SECONDS),
        )
        session.add(challenge)
        session.commit()
        session.refresh(challenge)
        options = MfaService.begin_passkey_authentication(
            session=session, challenge=challenge
        )
        # Return as a tuple instead of stuffing the token into the
        # options dict — keeps the dict shape == WebAuthn spec.
        return challenge, options

    # ── Helpers used by routes ─────────────────────────────────────────

    @staticmethod
    def has_passkey(*, session: Session, user_id: uuid.UUID) -> bool:
        """Cheap existence check for the ``UserPublic`` derived flags."""
        stmt = select(UserPasskey.id).where(UserPasskey.user_id == user_id)
        return session.exec(stmt).first() is not None

    @staticmethod
    def has_totp(*, session: Session, user_id: uuid.UUID) -> bool:
        """Cheap existence check for the ``UserPublic`` derived flags."""
        stmt = select(UserTotpSecret.id).where(UserTotpSecret.user_id == user_id)
        return session.exec(stmt).first() is not None

    @staticmethod
    def list_passkeys(*, session: Session, user: User) -> list[UserPasskey]:
        """All passkeys owned by ``user``, newest first."""
        return MfaService._list_user_passkeys(session, user.id)

    @staticmethod
    def passkey_to_public(passkey: UserPasskey) -> UserPasskeyPublic:
        """Render a :class:`UserPasskey` DB row to its API-safe form.

        Decodes the JSON-encoded ``transports`` blob defensively — older
        rows / hand-touched data may not parse as a list, so we degrade
        to an empty list rather than 500.
        """
        try:
            transports = (
                json.loads(passkey.transports) if passkey.transports else []
            )
            if not isinstance(transports, list):
                transports = []
        except (ValueError, TypeError):
            transports = []
        return UserPasskeyPublic(
            id=passkey.id,
            nickname=passkey.nickname,
            transports=[t for t in transports if isinstance(t, str)],
            aaguid=passkey.aaguid,
            device_type=passkey.device_type,
            backed_up=passkey.backed_up,
            created_at=passkey.created_at,
            last_used_at=passkey.last_used_at,
        )

    @staticmethod
    def check_verify_rate_limit(*, session: Session, user: User) -> None:
        """Enforce the per-user soft throttle on ``POST /login/mfa/verify``.

        At most :data:`_VERIFY_RATE_LIMIT_MAX` verifications per
        :data:`_VERIFY_RATE_LIMIT_WINDOW_SECONDS` window, keyed on
        ``user_id``.  Logs :data:`MFA_RATE_LIMITED` (severity ``"medium"``)
        and raises ``ValueError("rate_limited")`` on a hit so the route
        layer maps to ``429``.

        In-memory token bucket — see plan §4.4.  Resets on process
        restart (acceptable for MVP).
        """
        now_ts = datetime.now(UTC).timestamp()
        window_start = now_ts - _VERIFY_RATE_LIMIT_WINDOW_SECONDS
        bucket = _verify_rate_limit_log.setdefault(user.id, [])
        # Drop timestamps outside the current window.
        bucket[:] = [ts for ts in bucket if ts >= window_start]
        if len(bucket) >= _VERIFY_RATE_LIMIT_MAX:
            MfaService._log_event(
                session=session,
                user=user,
                event_type=security_event_constants.MFA_RATE_LIMITED,
                severity="medium",
                details={
                    "window_seconds": _VERIFY_RATE_LIMIT_WINDOW_SECONDS,
                    "max_attempts": _VERIFY_RATE_LIMIT_MAX,
                },
            )
            session.commit()
            raise ValueError("rate_limited")
        bucket.append(now_ts)
        # Opportunistic sweep: when the dict grows past the threshold,
        # drop keys whose buckets only contain stale timestamps. Keeps the
        # in-memory cost bounded for instances with many one-shot users.
        if len(_verify_rate_limit_log) >= _RATE_LIMIT_SWEEP_THRESHOLD:
            stale = [
                uid
                for uid, ts_list in _verify_rate_limit_log.items()
                if not ts_list or max(ts_list) < window_start
            ]
            for uid in stale:
                del _verify_rate_limit_log[uid]

    @staticmethod
    def check_anonymous_verify_rate_limit(*, source_key: str) -> None:
        """Per-source rate limit for the bad-token branch of
        ``/login/mfa/verify``.

        At most :data:`_ANONYMOUS_VERIFY_RATE_LIMIT_MAX` attempts per
        :data:`_ANONYMOUS_VERIFY_RATE_LIMIT_WINDOW_SECONDS` window per
        ``source_key`` (typically the client IP).  Raises
        ``ValueError("rate_limited")`` on a hit; nothing is written to
        the DB because we don't know which user (if any) the caller is
        targeting.  The caller is expected to ``logging.warning`` the
        attempt so the spray-prober signal still lands in server logs.
        """
        now_ts = datetime.now(UTC).timestamp()
        window_start = now_ts - _ANONYMOUS_VERIFY_RATE_LIMIT_WINDOW_SECONDS
        bucket = _anonymous_verify_rate_limit_log.setdefault(source_key, [])
        bucket[:] = [ts for ts in bucket if ts >= window_start]
        if len(bucket) >= _ANONYMOUS_VERIFY_RATE_LIMIT_MAX:
            raise ValueError("rate_limited")
        bucket.append(now_ts)
        if (
            len(_anonymous_verify_rate_limit_log)
            >= _RATE_LIMIT_SWEEP_THRESHOLD
        ):
            stale = [
                k
                for k, ts_list in _anonymous_verify_rate_limit_log.items()
                if not ts_list or max(ts_list) < window_start
            ]
            for k in stale:
                del _anonymous_verify_rate_limit_log[k]

    @staticmethod
    def allowed_methods_for_user(*, session: Session, user: User) -> list[str]:
        """Return the subset of ``["passkey", "totp", "recovery"]`` the user
        can use to satisfy a login MFA challenge.

        Centralised so ``routes/login.py`` and ``routes/oauth.py`` cannot
        drift apart on what's offered.
        """
        methods: list[str] = []
        if MfaService.has_passkey(session=session, user_id=user.id):
            methods.append("passkey")
        if MfaService.has_totp(session=session, user_id=user.id):
            methods.append("totp")
        # Recovery is always offered as the escape hatch — only verifies
        # if the user has unused codes, which is checked here.
        if MfaService.remaining_recovery_codes(session=session, user=user) > 0:
            methods.append("recovery")
        return methods

    @staticmethod
    def get_user_passkey(
        *, session: Session, user: User, passkey_id: uuid.UUID
    ) -> UserPasskey:
        """Owner-scoped lookup for rename/delete."""
        passkey = session.get(UserPasskey, passkey_id)
        if passkey is None or passkey.user_id != user.id:
            raise ValueError("passkey_not_found")
        return passkey

    @staticmethod
    def rename_passkey(
        *,
        session: Session,
        user: User,
        passkey_id: uuid.UUID,
        nickname: str,
    ) -> UserPasskey:
        """Patch ``nickname`` on an owned passkey."""
        passkey = MfaService.get_user_passkey(
            session=session, user=user, passkey_id=passkey_id
        )
        passkey.nickname = (nickname or "Passkey")[:64]
        session.add(passkey)
        session.commit()
        session.refresh(passkey)
        return passkey

    @staticmethod
    def delete_passkey(
        *, session: Session, user: User, passkey_id: uuid.UUID
    ) -> None:
        """Delete a passkey.

        If this passkey is the user's last remaining 2FA factor (no other
        passkeys and no TOTP), automatically turn off 2FA via
        :meth:`UserService.disable_all_factors` — equivalent to the user
        running the explicit disable flow, but inline so a single click
        finishes the job.  Recovery codes and pending challenges are
        wiped in that path; a ``MFA_DISABLED`` event with
        ``reason="last_factor_removed"`` is logged for the audit trail.
        """
        passkey = MfaService.get_user_passkey(
            session=session, user=user, passkey_id=passkey_id
        )
        other_passkeys = (
            session.exec(
                select(UserPasskey).where(
                    UserPasskey.user_id == user.id,
                    UserPasskey.id != passkey.id,
                )
            ).all()
        )
        if (
            user.two_factor_enabled
            and not other_passkeys
            and not MfaService.has_totp(session=session, user_id=user.id)
        ):
            # disable_all_factors wipes every passkey row too, so we
            # don't need to delete `passkey` separately.
            UserService.disable_all_factors(
                session=session, user=user, reason="last_factor_removed"
            )
            return
        session.delete(passkey)
        session.commit()

    @staticmethod
    def disable_totp(*, session: Session, user: User) -> None:
        """Remove the TOTP secret.

        Idempotent: returns successfully even if no TOTP is enrolled (the
        UI already gates the button, and an idempotent disable is a
        friendlier API contract for external callers).

        Same auto-disable-on-last-factor semantics as
        :meth:`delete_passkey`: if TOTP is the user's only remaining
        factor, the entire 2FA configuration is wiped via
        :meth:`UserService.disable_all_factors` and ``MFA_DISABLED`` is
        logged with ``reason="last_factor_removed"``.
        """
        if not MfaService.has_totp(session=session, user_id=user.id):
            return
        if (
            user.two_factor_enabled
            and not MfaService.has_passkey(session=session, user_id=user.id)
        ):
            # disable_all_factors wipes the TOTP row along with everything else.
            UserService.disable_all_factors(
                session=session, user=user, reason="last_factor_removed"
            )
            return
        session.exec(
            delete(UserTotpSecret).where(UserTotpSecret.user_id == user.id)
        )
        session.commit()

    @staticmethod
    def _mark_factor_enrolled(session: Session, user: User) -> None:
        """Flip the master switch and stamp ``two_factor_enrolled_at``
        on first enrollment.  Idempotent for subsequent factors."""
        now = datetime.now(UTC)
        if not user.two_factor_enabled:
            user.two_factor_enabled = True
            user.two_factor_enrolled_at = now
        session.add(user)

    @staticmethod
    def _list_user_passkeys(
        session: Session, user_id: uuid.UUID
    ) -> list[UserPasskey]:
        stmt = (
            select(UserPasskey)
            .where(UserPasskey.user_id == user_id)
            .order_by(UserPasskey.created_at.desc())  # type: ignore[union-attr]
        )
        return list(session.exec(stmt).all())

    # ── Audit log ──────────────────────────────────────────────────────

    @staticmethod
    def _log_event(
        *,
        session: Session,
        user: User,
        event_type: str,
        severity: str,
        details: dict,
    ) -> None:
        """Thin wrapper that writes a :class:`SecurityEvent` row inside
        the same transaction as the caller.

        Importing :class:`SecurityEventService` here would risk circular
        imports (it owns a few cross-domain helpers), so we use the
        ``SecurityEventCreate`` schema and the ORM directly.
        """
        from app.models import SecurityEvent  # local to avoid cycles

        payload = SecurityEventCreate(
            event_type=event_type,
            severity=severity,
            details=details,
        )
        row = SecurityEvent(
            user_id=user.id,
            event_type=payload.event_type,
            severity=payload.severity,
            details=json.dumps(payload.details),
        )
        session.add(row)


# ── Module-private helpers ────────────────────────────────────────────


def _ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` with a UTC tzinfo if it was stored naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string (with or without padding) to bytes."""
    if not isinstance(value, str):
        raise ValueError("not a base64url string")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _generate_recovery_code(length: int) -> str:
    """Generate a single recovery code formatted as ``XXXX-XXXX...``."""
    raw = "".join(
        secrets.choice(_RECOVERY_CODE_ALPHABET) for _ in range(max(length, 8))
    )
    # Split into 4-char groups for readability.
    chunks = [raw[i : i + 4] for i in range(0, len(raw), 4)]
    return "-".join(chunks)


def _normalise_recovery_code(code: str) -> str:
    """Strip whitespace, dashes; uppercase — matches what's hashed."""
    if not isinstance(code, str):
        return ""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _render_qr_svg_data_uri(otpauth_uri: str) -> str:
    """Render an ``otpauth://`` URI as an SVG data URI."""
    img = qrcode.make(otpauth_uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    raw_svg = buf.getvalue()
    b64 = base64.b64encode(raw_svg).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _device_type_from_credential(credential: dict) -> str:
    """Best-effort ``platform`` / ``cross-platform`` label."""
    extra = credential.get("authenticatorAttachment") or credential.get(
        "transports"
    )
    if isinstance(extra, str) and extra in {"platform", "cross-platform"}:
        return extra
    transports = _transports_from_credential(credential)
    if "internal" in transports:
        return "platform"
    return "cross-platform"


def _backed_up_from_credential(credential: dict) -> bool:
    """Pull the ``backed_up`` flag out of the credential payload if the
    client supplied it.  Defaults to ``False``."""
    resp = credential.get("response") or {}
    if isinstance(resp, dict):
        flag = resp.get("backedUp")
        if isinstance(flag, bool):
            return flag
    return False


def _transports_from_credential(credential: dict) -> list[str]:
    """Extract WebAuthn transports from the registration response."""
    resp = credential.get("response") or {}
    if isinstance(resp, dict):
        transports = resp.get("transports") or []
        if isinstance(transports, list):
            return [t for t in transports if isinstance(t, str)]
    return []


def _aaguid_from_verification(verification: Any) -> str | None:
    """Pull the AAGUID off the py_webauthn verification result if present."""
    aaguid = getattr(verification, "aaguid", None)
    if aaguid is None:
        return None
    if isinstance(aaguid, bytes):
        return aaguid.hex()
    return str(aaguid)
