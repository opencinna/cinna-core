"""app_sync_record — one row per synced entity.

The body is **client-encrypted ciphertext** stored verbatim; the server never
decrypts it (zero-knowledge). See §3.1 / §3.4 of the design.
"""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AppSyncRecord(SQLModel, table=True):
    __tablename__ = "app_sync_record"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "collection",
            "client_entity_id",
            name="ix_app_sync_record_natural",
        ),
        Index("ix_app_sync_record_user_seq", "user_id", "seq"),
        Index("ix_app_sync_record_user_collection", "user_id", "collection"),
        Index("ix_app_sync_record_user_id", "user_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    collection: str = Field(max_length=64, nullable=False)
    # Client-generated globally-unique UUID (validated as UUID at the service layer).
    client_entity_id: str = Field(max_length=128, nullable=False)
    # Per-user monotonic sequence assigned at write time — the sync cursor.
    seq: int = Field(sa_type=BigInteger, nullable=False)
    # Client-encrypted AEAD envelope, stored verbatim. NULL for tombstones.
    payload_ciphertext: str | None = Field(default=None, nullable=True)
    # UMK generation this ciphertext was encrypted under.
    enc_umk_version: int = Field(default=1, nullable=False)
    # Ciphertext byte size, for quota accounting.
    payload_bytes: int = Field(default=0, nullable=False)
    # Client-supplied keyed fingerprint, compared only for equality. NULL for tombstones.
    content_fingerprint: str | None = Field(default=None, max_length=88, nullable=True)
    deleted: bool = Field(default=False, nullable=False)
    # Client logical clock — the LWW comparison key (tz-naive UTC).
    client_updated_at: datetime = Field(nullable=False)
    server_updated_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    # external_client_id of the desktop device that last wrote. NULL for web tokens.
    last_writer_client_id: str | None = Field(
        default=None, max_length=64, nullable=True
    )
