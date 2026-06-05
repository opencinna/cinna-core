"""Trusted-device "Do not ask on this device" token.

One row per trusted device per user.  Created on the ``/login/mfa``
screen when the user opts to skip the 2FA challenge on the same device
for a bounded window (1/7/30 days).  Mirrors the
:class:`UserRecoveryCode` hashing pattern — **the plaintext token is
never stored at rest**; only a bcrypt hash is persisted, and the
plaintext is returned exactly once at mint time.  On subsequent logins
the frontend presents the plaintext token; the backend bcrypt-verifies
it against the requesting user's own live rows and, on a match, skips
the second-factor challenge.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class UserTrustedDevice(SQLModel, table=True):
    """Bcrypt-hashed, per-device, time-limited 2FA-skip token.

    The token is opaque (``secrets.token_urlsafe(32)``), carries no
    structure or user id, and is only ever validated against the
    requesting user's own rows.  Wiped whenever 2FA is disabled and
    swept once expired by the hourly cleanup job.
    """
    __tablename__ = "user_trusted_device"
    __table_args__ = (
        Index(
            "ix_user_trusted_device_user_expires", "user_id", "expires_at"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", index=True
    )
    # bcrypt hash of the opaque plaintext token (``get_password_hash``).
    # Never returned through any API.
    token_hash: str = Field()
    # ``created_at + remember_device_days``. The skip is rejected once
    # ``now >= expires_at``.
    expires_at: datetime = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Updated each time the token is used to skip a challenge.
    last_used_at: datetime | None = Field(default=None)
    # Best-effort device label — truncated User-Agent captured at mint
    # time. Display-only.
    label: str | None = Field(default=None, max_length=256)


# ── Public schemas ─────────────────────────────────────────────────────


class TrustedDevicePublic(SQLModel):
    """API-safe projection of a :class:`UserTrustedDevice` row.

    Omits ``token_hash`` entirely — the secret never leaves the server.
    Defined for an optional future Settings device-management list (not
    used in MVP routes).
    """
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    label: str | None
