"""TOTP authenticator-app secret.

One row per user — created only after enrollment is confirmed with a
valid 6-digit code.  The secret is encrypted at rest with the existing
Fernet helper (``encrypt_field`` / ``decrypt_field`` from
``app.core.security``); only ``MfaService`` ever decrypts.
"""
import re
import uuid
from datetime import datetime, UTC

from pydantic import field_validator
from sqlalchemy import BigInteger
from sqlmodel import Field, SQLModel, Column, Text


class UserTotpSecret(SQLModel, table=True):
    """Encrypted TOTP secret + RFC-6238 parameters.

    The 1:1 relationship with ``user`` is enforced via the unique
    constraint on ``user_id``.  ``last_used_step`` tracks the highest
    RFC-6238 time-step counter accepted so far so repeat-codes (replay
    within the valid window) are rejected.
    """
    __tablename__ = "user_totp_secret"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
        unique=True,
        index=True,
    )
    # Fernet-encrypted base32 secret.  Never returned through any API.
    secret_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    algorithm: str = Field(default="SHA1", max_length=16)
    digits: int = Field(default=6)
    period: int = Field(default=30)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = Field(default=None)
    # Last RFC-6238 step counter accepted by ``verify_totp`` — used to
    # detect replay within the valid window.  BigInteger because unix
    # second counts / 30 will exceed Int32 well within the next century.
    last_used_step: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )


# ── Public schemas ─────────────────────────────────────────────────────


class TotpEnrollResponse(SQLModel):
    """Response of ``POST /users/me/mfa/totp/begin``.

    ``secret_token`` is an HMAC-signed handle that the client echoes back
    to ``/finish`` — the raw secret is never stored server-side until
    finish succeeds.
    """
    secret_base32: str
    otpauth_uri: str
    qr_svg_data_uri: str
    secret_token: str


class TotpFinishRequest(SQLModel):
    secret_token: str
    # SQLModel's ``Field`` wrapper does not forward ``pattern=`` through to
    # Pydantic, so we enforce the six-digit constraint via an explicit
    # ``field_validator`` instead.
    code: str = Field(min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def _code_is_six_digits(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]{6}", value):
            raise ValueError("code must be 6 digits")
        return value
