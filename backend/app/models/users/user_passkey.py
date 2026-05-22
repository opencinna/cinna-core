"""WebAuthn passkey credential record.

Each row represents one registered WebAuthn authenticator (FIDO2 / platform
or roaming) bound to a user.  Multiple passkeys per user are allowed —
deletion cascades when the owning user is removed.

See the architectural plan at
``docs/drafts/user-2fa-passkeys-totp_plan.md`` for the full design.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import LargeBinary
from sqlmodel import Field, SQLModel, Column


class UserPasskey(SQLModel, table=True):
    """Registered WebAuthn credential.

    The ``credential_id`` raw bytes are globally unique.  ``public_key``
    stores the COSE-encoded public key blob returned by the WebAuthn
    registration ceremony — opaque to the platform, decoded only by the
    ``webauthn`` (py_webauthn) library during verification.
    """
    __tablename__ = "user_passkey"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", index=True
    )
    credential_id: bytes = Field(
        sa_column=Column(LargeBinary, nullable=False, unique=True, index=True),
    )
    public_key: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    sign_count: int = Field(default=0)
    # JSON list of WebAuthn transports: "usb", "nfc", "ble", "internal", "hybrid"
    transports: str = Field(default="[]")
    # Authenticator AAGUID (best-effort device-model label)
    aaguid: str | None = Field(default=None, max_length=64)
    # User-chosen label, e.g. "YubiKey 5" or "iPhone Touch ID"
    nickname: str = Field(max_length=64)
    # "platform" (Touch ID, Windows Hello) or "cross-platform" (security key)
    device_type: str = Field(max_length=32)
    # Set when WebAuthn `flags.bs == True` — useful to mark synced (iCloud) keys
    backed_up: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = Field(default=None)


# ── Public schemas ─────────────────────────────────────────────────────


class UserPasskeyPublic(SQLModel):
    """API-safe passkey representation — omits the raw ``credential_id``
    and ``public_key`` blobs."""
    id: uuid.UUID
    nickname: str
    transports: list[str]
    aaguid: str | None
    device_type: str
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None


class UserPasskeysPublic(SQLModel):
    data: list[UserPasskeyPublic]
    count: int


class UserPasskeyUpdate(SQLModel):
    """Patch request for renaming a passkey."""
    nickname: str = Field(min_length=1, max_length=64)
