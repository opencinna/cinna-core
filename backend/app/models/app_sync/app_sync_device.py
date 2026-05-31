"""app_sync_device — registered device public keys for E2E key sharing.

The private key never leaves the device. See §12.4 of the design.
"""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AppSyncDevice(SQLModel, table=True):
    __tablename__ = "app_sync_device"
    __table_args__ = (Index("ix_app_sync_device_user_id", "user_id"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    device_label: str = Field(max_length=128, nullable=False)
    # X25519 public key (base64). Private key never leaves the device.
    public_key: str = Field(nullable=False)
    # Links to the DesktopOAuthClient device when applicable.
    external_client_id: UUID | None = Field(default=None, nullable=True)
    is_revoked: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    last_seen_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )


class AppSyncDeviceCreate(SQLModel):
    device_label: str = Field(max_length=128)
    public_key: str
    external_client_id: UUID | None = None


class AppSyncDevicePublic(SQLModel):
    id: UUID
    device_label: str
    public_key: str
    external_client_id: UUID | None = None
    is_revoked: bool
    created_at: datetime
    last_seen_at: datetime | None = None

    model_config = {"from_attributes": True}
