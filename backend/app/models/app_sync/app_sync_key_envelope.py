"""app_sync_key_envelope — wrapped copies of the UMK (one per unlock method x version).

The wrapped key is opaque ciphertext the server cannot open. See §12.4 of the design.
"""
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


WrapMethod = Literal["device", "recovery", "passphrase"]


class AppSyncKeyEnvelope(SQLModel, table=True):
    __tablename__ = "app_sync_key_envelope"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "wrap_method",
            "umk_version",
            "device_id",
            name="uq_app_sync_key_envelope_unlock",
        ),
        Index("ix_app_sync_key_envelope_user_id", "user_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    # device | recovery | passphrase
    wrap_method: str = Field(max_length=16, nullable=False)
    umk_version: int = Field(nullable=False)
    # The UMK ciphertext — opaque to the server.
    wrapped_key: str = Field(nullable=False)
    # hkdf (recovery) / argon2id (passphrase)
    kdf: str | None = Field(default=None, max_length=32, nullable=True)
    kdf_params: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    device_id: UUID | None = Field(
        default=None,
        foreign_key="app_sync_device.id",
        ondelete="CASCADE",
        nullable=True,
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


class AppSyncKeyEnvelopeCreate(SQLModel):
    wrap_method: WrapMethod
    umk_version: int
    wrapped_key: str
    kdf: str | None = None
    kdf_params: dict | None = None
    device_id: UUID | None = None


class AppSyncKeyEnvelopePublic(SQLModel):
    id: UUID
    wrap_method: str
    umk_version: int
    wrapped_key: str
    kdf: str | None = None
    kdf_params: dict | None = None
    device_id: UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
