"""app_sync_state — per-user singleton holding the sequence counter and quota.

Lazily created on first sync. See §3.2 of the design.
"""
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AppSyncState(SQLModel, table=True):
    __tablename__ = "app_sync_state"

    user_id: UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", primary_key=True
    )
    # Last allocated per-user sequence; incremented under row lock on every write.
    current_seq: int = Field(default=0, sa_type=BigInteger, nullable=False)
    # Live (non-tombstone) record count, for quota.
    total_records: int = Field(default=0, nullable=False)
    # Sum of payload_bytes (ciphertext) over live records, for quota.
    total_bytes: int = Field(default=0, sa_type=BigInteger, nullable=False)
    # Current User-Master-Key generation. 0 = E2E not yet initialised.
    active_umk_version: int = Field(default=0, nullable=False)
    e2e_initialized_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    updated_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
