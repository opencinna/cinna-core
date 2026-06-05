"""Short-lived MFA challenge row.

Created after the first factor (password / Google OAuth) succeeds when
the user has 2FA enabled.  The opaque ``challenge_token`` is returned to
the client and exchanged at ``POST /login/mfa/verify`` for an access
token.  Expires after :pyattr:`Settings.MFA_CHALLENGE_TTL_SECONDS`
seconds (default 5 min) and is single-use.
"""
import uuid
from datetime import datetime, UTC
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import Index, LargeBinary
from sqlmodel import Field, SQLModel, Column


# Allowed values for :pyattr:`UserMfaChallenge.first_factor`. Mirrors the
# pattern used by ``UserRole`` (Literal type alias, not a Python Enum) so
# the OpenAPI schema gets a clean string-union and there's a single
# source of truth callers can import.
MfaFirstFactor = Literal["password", "google_oauth", "step_up"]


class UserMfaChallenge(SQLModel, table=True):
    """Server-side handle binding a first-factor success to a pending
    second-factor verification."""
    __tablename__ = "user_mfa_challenge"
    __table_args__ = (
        Index("ix_user_mfa_challenge_user_created", "user_id", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", index=True
    )
    # URL-safe random token returned to the client (256-bit) — unique.
    challenge_token: str = Field(max_length=128, unique=True, index=True)
    # WebAuthn assertion-challenge nonce.  Null until ``passkey/options``
    # is called on this challenge row.
    webauthn_challenge: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    # How the user proved who they were before this challenge row was
    # created. Stored as a plain string for SQLModel column mapping; the
    # set of valid values is the :data:`MfaFirstFactor` Literal:
    #   - ``password``     — `/login/access-token`
    #   - ``google_oauth`` — `/auth/google/callback`
    #   - ``step_up``      — synthetic challenge issued for an
    #     authenticated step-up proof (passkey enrollment + step-up
    #     passkey assertion). Never reaches the login challenge flow.
    first_factor: str = Field(max_length=32)
    # Incremented on every failed verification — capped at
    # ``MFA_MAX_ATTEMPTS_PER_CHALLENGE``.
    attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field()
    consumed_at: datetime | None = Field(default=None)


# ── Public schemas ─────────────────────────────────────────────────────


class MfaChallenge(SQLModel):
    """Response when ``POST /login/access-token`` requires a second
    factor.

    ``kind`` is a literal discriminator that lets the frontend branch
    on a single union shape (``LoginResponse``).
    """
    kind: Literal["mfa_challenge"] = "mfa_challenge"
    challenge_token: str
    expires_at: datetime
    allowed_methods: list[str]


class MfaVerifyRequest(BaseModel):
    """Body of ``POST /login/mfa/verify``.

    ``method`` is one of ``"passkey"``, ``"totp"``, or ``"recovery"``.
    ``payload`` shape varies by method:

    - ``passkey`` — full WebAuthn ``AuthenticationResponseJSON`` dict
    - ``totp``   — ``{"code": "123456"}``
    - ``recovery`` — ``{"code": "xxxx-xxxx"}``

    Plain ``pydantic.BaseModel`` (not ``SQLModel``) so the ``Literal``
    constraint on ``method`` propagates into OpenAPI as an enum and the
    generated TypeScript client gets a string-union type.
    """
    challenge_token: str
    method: Literal["passkey", "totp", "recovery"]
    payload: dict
    # "Do not ask on this device" duration. When set, a successful verify
    # mints a ``UserTrustedDevice`` row and returns the plaintext token on
    # the ``LoginToken`` response.  ``Literal`` so the OpenAPI enum (and TS
    # union) reject arbitrary durations at the edge; the service re-checks
    # against ``MFA_TRUSTED_DEVICE_ALLOWED_DAYS`` for non-route callers.
    remember_device_days: Literal[1, 7, 30] | None = None


class LoginToken(SQLModel):
    """Discriminated alternative to ``MfaChallenge`` returned when 2FA
    is not required.

    Mirrors :class:`app.models.users.user.Token` with an explicit
    ``kind`` literal so the frontend can branch on a single union shape.
    """
    kind: Literal["token"] = "token"
    access_token: str
    token_type: str = "bearer"
    # Plaintext trusted-device token, returned exactly once by
    # ``/login/mfa/verify`` when a ``remember_device_days`` duration was
    # requested and the device was registered.  ``None`` on every other
    # ``LoginToken`` (plain login, skip-path login, no-duration verify).
    # Adding an optional field keeps the ``LoginResponse`` discriminated
    # union backward-compatible.
    trusted_device_token: str | None = None


class MfaStatus(SQLModel):
    """Response of ``GET /users/me/mfa/status``."""
    enabled: bool
    has_passkey: bool
    has_totp: bool
    has_recovery_codes: bool
    passkey_count: int
    last_used_at: datetime | None
    enrolled_at: datetime | None


class StepUpProof(SQLModel):
    """Fresh-factor proof required to disable/weaken 2FA.

    Exactly one of these must be supplied — validated server-side.
    """
    password: str | None = None
    totp_code: str | None = None
    # Full WebAuthn ``AuthenticationResponseJSON`` dict — paired with the
    # ``passkey_challenge_token`` returned by a prior step-up
    # /options call.
    passkey_assertion: dict | None = None
    passkey_challenge_token: str | None = None


# ── Login response discriminated union ────────────────────────────────
#
# ``LoginResponse = LoginToken | MfaChallenge`` — kept as a typing alias
# so the FastAPI / OpenAPI generator emits a single union type with a
# ``kind`` discriminator the frontend can branch on.

LoginResponse = LoginToken | MfaChallenge


class PasskeyAuthOptionsRequest(SQLModel):
    """Body of ``POST /login/mfa/passkey/options``."""
    challenge_token: str


class PasskeyAuthOptionsResponse(SQLModel):
    """Response of ``POST /login/mfa/passkey/options``.

    Nests the WebAuthn ``PublicKeyCredentialRequestOptionsJSON`` under
    ``options`` so the frontend can pass it straight to
    ``@simplewebauthn/browser`` without our request handle leaking into
    the spec-defined options object.  The caller already holds
    ``challenge_token`` (they supplied it in the request body), so we do
    not echo it back.
    """
    options: dict

