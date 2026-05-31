"""Pydantic (non-table) schemas for the App Sync protocol (§3.4, §12.5)."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlmodel import Field, SQLModel

from app.models.app_sync.app_sync_device import AppSyncDevicePublic


# ── Sync protocol ─────────────────────────────────────────────────────────


class SyncRecordUpsert(SQLModel):
    collection: str
    client_entity_id: str
    # AEAD envelope (§12.3); None only when deleted.
    payload_ciphertext: str | None = None
    enc_umk_version: int = 1
    # Client keyed HMAC; None only when deleted.
    content_fingerprint: str | None = None
    deleted: bool = False
    client_updated_at: datetime
    # Client's last-known seq; optional optimistic hint (not authoritative).
    base_seq: int | None = None


class SyncRecordPublic(SQLModel):
    collection: str
    client_entity_id: str
    payload_ciphertext: str | None = None
    enc_umk_version: int
    deleted: bool
    seq: int
    server_updated_at: datetime
    last_writer_client_id: str | None = None


class SyncPushResult(SQLModel):
    collection: str
    client_entity_id: str
    status: Literal["applied", "conflict", "unchanged", "rejected"]
    seq: int
    # Set when status == "conflict".
    server_record: SyncRecordPublic | None = None


class SyncRequest(SQLModel):
    cursor: int = 0
    changes: list[SyncRecordUpsert] = Field(default_factory=list)
    collections: list[str] | None = None
    limit: int = 500


class PullRequest(SQLModel):
    cursor: int = 0
    collections: list[str] | None = None
    limit: int = 500


class PushRequest(SQLModel):
    changes: list[SyncRecordUpsert] = Field(default_factory=list)


class WipeRequest(SQLModel):
    collections: list[str] | None = None


class SyncResponse(SQLModel):
    applied: list[SyncPushResult] = Field(default_factory=list)
    # Pulled records, seq-ordered.
    changes: list[SyncRecordPublic] = Field(default_factory=list)
    next_cursor: int
    has_more: bool
    server_time: datetime


class SyncStatePublic(SQLModel):
    cursor: int
    total_records: int
    total_bytes: int
    quota_bytes: int
    quota_records: int
    collection_counts: dict[str, int] = Field(default_factory=dict)


# ── Encryption / key-management (§12.5) ───────────────────────────────────


class EncryptionStatePublic(SQLModel):
    initialized: bool
    active_umk_version: int
    has_recovery: bool
    has_passphrase: bool
    devices: list[AppSyncDevicePublic] = Field(default_factory=list)


class KeyEnvelopeInput(SQLModel):
    wrap_method: Literal["device", "recovery", "passphrase"]
    umk_version: int = 1
    wrapped_key: str
    kdf: str | None = None
    kdf_params: dict | None = None
    device_id: UUID | None = None


class DeviceInput(SQLModel):
    device_label: str
    public_key: str
    external_client_id: UUID | None = None


class EncryptionInitRequest(SQLModel):
    device: DeviceInput
    # Initial device + recovery (+ optional passphrase) envelopes.
    envelopes: list[KeyEnvelopeInput] = Field(default_factory=list)
