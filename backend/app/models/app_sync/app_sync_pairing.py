"""app_sync_pairing — short-lived blind relay for QR device pairing.

The server only relays ``sealed_umk`` (ciphertext sealed to the joining device's
ephemeral key); it never sees the UMK. See §12.4 / §12.6 of the design.
"""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AppSyncPairing(SQLModel, table=True):
    __tablename__ = "app_sync_pairing"
    __table_args__ = (
        Index("ix_app_sync_pairing_user_id", "user_id"),
        Index("ix_app_sync_pairing_code_hash", "pairing_code_hash"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    # SHA-256 of the pairing code; raw code only in the QR/screen.
    pairing_code_hash: str = Field(max_length=64, nullable=False)
    # The joining device's ephemeral X25519 public key.
    new_device_pubkey: str = Field(nullable=False)
    device_label: str | None = Field(default=None, max_length=128, nullable=True)
    # UMK sealed to new_device_pubkey by the existing device; NULL until completed.
    sealed_umk: str | None = Field(default=None, nullable=True)
    # pending -> completed -> consumed; or expired.
    status: str = Field(default="pending", max_length=16, nullable=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# ── Pydantic schemas ──────────────────────────────────────────────────────


class PairingStartRequest(SQLModel):
    new_device_pubkey: str
    device_label: str | None = None


class PairingStartResponse(SQLModel):
    pairing_code: str
    expires_at: datetime


class PairingStatusPublic(SQLModel):
    new_device_pubkey: str
    device_label: str | None = None
    status: str
    sealed_umk: str | None = None
    expires_at: datetime


class PairingCompleteRequest(SQLModel):
    sealed_umk: str
