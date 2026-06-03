"""AppSyncService — the zero-knowledge sync substrate.

THE SERVER DOES NO PAYLOAD CRYPTOGRAPHY. There are intentionally no
``_encrypt`` / ``_decrypt`` helpers: ``payload_ciphertext`` and ``wrapped_key``
are stored verbatim and returned verbatim. Conflict resolution (LWW) runs on
cleartext metadata only (``client_updated_at`` + ``seq``); the no-op
short-circuit compares the client-supplied ``content_fingerprint`` for equality.

See docs/application/app_sync/app_sync_tech.md.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.config import settings
from app.models.app_sync.app_sync_device import AppSyncDevice, AppSyncDevicePublic
from app.models.app_sync.app_sync_key_envelope import (
    AppSyncKeyEnvelope,
    AppSyncKeyEnvelopePublic,
)
from app.models.app_sync.app_sync_pairing import (
    AppSyncPairing,
    PairingStartResponse,
    PairingStatusPublic,
)
from app.models.app_sync.app_sync_record import AppSyncRecord
from app.models.app_sync.app_sync_schemas import (
    DeviceInput,
    EncryptionInitRequest,
    EncryptionStatePublic,
    KeyEnvelopeInput,
    SyncPushResult,
    SyncRecordPublic,
    SyncRecordUpsert,
    SyncResponse,
    SyncStatePublic,
)
from app.models.app_sync.app_sync_state import AppSyncState

_COLLECTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# client_entity_id is an opaque, client-generated stable id (UUID, nanoid, …).
# Shape allowlist: URL-safe, length-bounded — accepts UUIDs (36 chars) and
# nanoids (21 chars, A-Za-z0-9_-); excludes whitespace, slashes, path
# traversal, control chars, and absurd lengths. Fits VARCHAR(128).
_CLIENT_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# Footgun-blocker: a pure-digits id is the device-local autoincrement-rowid
# mistake. UUIDs and nanoids both contain non-digit chars, so neither matches.
_BARE_INTEGER_RE = re.compile(r"^\d+$")
_CLOCK_SKEW_CEILING = timedelta(hours=24)


def _utc_now_naive() -> datetime:
    """Server clock as a tz-naive UTC datetime (matches client_updated_at storage)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalise an incoming datetime to tz-naive UTC for storage/comparison."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


# ── Exception hierarchy ───────────────────────────────────────────────────


class AppSyncError(Exception):
    """Base App Sync service error."""

    def __init__(self, message: str, status_code: int = 400, detail: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


class PayloadTooLargeError(AppSyncError):
    def __init__(self, client_entity_id: str, size: int):
        super().__init__(
            f"Record {client_entity_id} ciphertext is {size} bytes, exceeds the "
            f"{settings.APP_SYNC_MAX_PAYLOAD_BYTES}-byte limit",
            status_code=413,
            detail={
                "client_entity_id": client_entity_id,
                "payload_bytes": size,
                "max_payload_bytes": settings.APP_SYNC_MAX_PAYLOAD_BYTES,
            },
        )


class QuotaExceededError(AppSyncError):
    def __init__(self, total_bytes: int, total_records: int):
        super().__init__(
            "Sync storage quota exceeded",
            status_code=413,
            detail={
                "total_bytes": total_bytes,
                "quota_bytes": settings.APP_SYNC_QUOTA_BYTES,
                "total_records": total_records,
                "quota_records": settings.APP_SYNC_QUOTA_RECORDS,
            },
        )


class BatchTooLargeError(AppSyncError):
    def __init__(self, count: int):
        super().__init__(
            f"Push batch of {count} records exceeds the "
            f"{settings.APP_SYNC_MAX_RECORDS_PER_PUSH}-record limit",
            status_code=422,
        )


class InvalidPayloadError(AppSyncError):
    def __init__(self, message: str = "Malformed sync record"):
        super().__init__(message, status_code=422)


class E2ENotInitializedError(AppSyncError):
    def __init__(self) -> None:
        super().__init__(
            "End-to-end encryption is not initialised for this account. "
            "Run POST /app-sync/encryption/init first.",
            status_code=409,
        )


class E2EAlreadyInitializedError(AppSyncError):
    def __init__(self) -> None:
        super().__init__(
            "End-to-end encryption is already initialised for this account.",
            status_code=409,
        )


class NotFoundError(AppSyncError):
    def __init__(self, message: str = "Not found"):
        super().__init__(message, status_code=404)


class PairingError(AppSyncError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message, status_code=status_code)


# ── Service ────────────────────────────────────────────────────────────────


class AppSyncService:

    # ── State helpers ────────────────────────────────────────────────────

    @staticmethod
    def _get_or_create_state(session: Session, user_id: UUID) -> AppSyncState:
        state = session.get(AppSyncState, user_id)
        if state is None:
            state = AppSyncState(user_id=user_id)
            session.add(state)
            session.flush()
        return state

    @staticmethod
    def _lock_state(session: Session, user_id: UUID) -> AppSyncState:
        """Acquire a ``SELECT ... FOR UPDATE`` row lock on the user's state.

        Serialises a single user's writes so the seq cursor stays gap-free.
        Creates the row lazily if absent.
        """
        state = session.exec(
            select(AppSyncState)
            .where(AppSyncState.user_id == user_id)
            .with_for_update()
        ).first()
        if state is None:
            state = AppSyncState(user_id=user_id)
            session.add(state)
            session.flush()
            state = session.exec(
                select(AppSyncState)
                .where(AppSyncState.user_id == user_id)
                .with_for_update()
            ).first()
            assert state is not None
        return state

    @staticmethod
    def _allocate_seq(state: AppSyncState, n: int = 1) -> int:
        """Allocate ``n`` consecutive seq values from an already-locked state row.

        Returns the first allocated seq (the block is ``[first, first + n - 1]``).
        Callers must pass the ``AppSyncState`` row obtained from ``_lock_state``
        (held under ``SELECT ... FOR UPDATE`` for the transaction). The counter
        is incremented in memory — no per-record DB round trip.
        """
        first = state.current_seq + 1
        state.current_seq += n
        state.updated_at = datetime.now(UTC)
        return first

    # ── Validation ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_upsert(change: SyncRecordUpsert) -> None:
        """Structural validation only — never decryptability (§4.4)."""
        if not _COLLECTION_RE.match(change.collection):
            raise InvalidPayloadError(
                f"Invalid collection name '{change.collection}'"
            )
        # client_entity_id must be a well-formed opaque client id — URL-safe,
        # 8–128 chars (accepts UUIDs and nanoids), and not a bare integer (the
        # cross-device collision footgun-blocker, §3.5 / §4.4).
        if not _CLIENT_ENTITY_ID_RE.match(str(change.client_entity_id)):
            raise InvalidPayloadError(
                f"client_entity_id '{change.client_entity_id}' is not a "
                "well-formed opaque client id"
            )
        if _BARE_INTEGER_RE.match(str(change.client_entity_id)):
            raise InvalidPayloadError(
                f"client_entity_id '{change.client_entity_id}' must not be a "
                "bare integer (device-local rowid)"
            )

        if change.deleted:
            return

        # Non-tombstone upserts must carry ciphertext + fingerprint + version.
        if not change.payload_ciphertext:
            raise InvalidPayloadError(
                f"Record {change.client_entity_id} is missing payload_ciphertext"
            )
        if not change.content_fingerprint:
            raise InvalidPayloadError(
                f"Record {change.client_entity_id} is missing content_fingerprint"
            )
        size = len(change.payload_ciphertext.encode("utf-8"))
        if size > settings.APP_SYNC_MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(change.client_entity_id, size)

    # ── Push ─────────────────────────────────────────────────────────────

    @staticmethod
    def push(
        session: Session,
        user,
        *,
        changes: list[SyncRecordUpsert],
        writer_client_id: str | None,
    ) -> list[SyncPushResult]:
        """Apply a batch of upserts/deletes atomically under the seq lock.

        Rejects with E2ENotInitializedError if active_umk_version == 0.
        Stores ciphertext VERBATIM — no decryption ever happens.
        """
        if len(changes) > settings.APP_SYNC_MAX_RECORDS_PER_PUSH:
            raise BatchTooLargeError(len(changes))

        # Validate the whole batch up front — a validation failure writes nothing.
        for change in changes:
            AppSyncService._validate_upsert(change)

        now_naive = _utc_now_naive()
        clock_ceiling = now_naive + _CLOCK_SKEW_CEILING

        # Lock the user's state row; all seq allocation + quota mutation happen here.
        # The E2E gate is checked under the lock so it can't race with init.
        locked = AppSyncService._lock_state(session, user.id)
        if locked.active_umk_version == 0:
            raise E2ENotInitializedError()

        results: list[SyncPushResult] = []
        for change in changes:
            results.append(
                AppSyncService._apply_one(
                    session,
                    user_id=user.id,
                    state=locked,
                    change=change,
                    now_naive=now_naive,
                    clock_ceiling=clock_ceiling,
                    writer_client_id=writer_client_id,
                )
            )

        locked.updated_at = datetime.now(UTC)
        session.add(locked)
        session.commit()
        return results

    @staticmethod
    def _apply_one(
        session: Session,
        *,
        user_id: UUID,
        state: AppSyncState,
        change: SyncRecordUpsert,
        now_naive: datetime,
        clock_ceiling: datetime,
        writer_client_id: str | None,
    ) -> SyncPushResult:
        # Clamp future client clocks to bound LWW abuse (§4.4 / §9).
        client_ts = _to_naive_utc(change.client_updated_at)
        if client_ts > clock_ceiling:
            client_ts = now_naive

        existing = session.exec(
            select(AppSyncRecord).where(
                AppSyncRecord.user_id == user_id,
                AppSyncRecord.collection == change.collection,
                AppSyncRecord.client_entity_id == change.client_entity_id,
            )
        ).first()

        new_bytes = (
            len(change.payload_ciphertext.encode("utf-8"))
            if (not change.deleted and change.payload_ciphertext)
            else 0
        )

        if existing is None:
            # A delete of an entity we never had: ignore (no row, no seq burn).
            if change.deleted:
                return SyncPushResult(
                    collection=change.collection,
                    client_entity_id=change.client_entity_id,
                    status="unchanged",
                    seq=state.current_seq,
                )
            AppSyncService._enforce_quota(
                state, delta_records=1, delta_bytes=new_bytes
            )
            seq = AppSyncService._allocate_seq(state)
            record = AppSyncRecord(
                user_id=user_id,
                collection=change.collection,
                client_entity_id=change.client_entity_id,
                seq=seq,
                payload_ciphertext=change.payload_ciphertext,
                enc_umk_version=change.enc_umk_version,
                payload_bytes=new_bytes,
                content_fingerprint=change.content_fingerprint,
                deleted=False,
                client_updated_at=client_ts,
                server_updated_at=datetime.now(UTC),
                last_writer_client_id=writer_client_id,
            )
            session.add(record)
            return SyncPushResult(
                collection=change.collection,
                client_entity_id=change.client_entity_id,
                status="applied",
                seq=seq,
            )

        # No-op short-circuit (idempotency): identical live content → unchanged,
        # regardless of timestamp ordering. Burns no seq, makes re-pushes safe.
        if (
            not change.deleted
            and not existing.deleted
            and change.content_fingerprint is not None
            and change.content_fingerprint == existing.content_fingerprint
        ):
            return SyncPushResult(
                collection=change.collection,
                client_entity_id=change.client_entity_id,
                status="unchanged",
                seq=existing.seq,
            )

        # LWW: incoming wins iff its client_updated_at is strictly newer, or
        # equal-time with a differing fingerprint (tie-break by writing).
        incoming_wins = client_ts > existing.client_updated_at or (
            client_ts == existing.client_updated_at
            and change.content_fingerprint != existing.content_fingerprint
        )
        if not incoming_wins:
            # Existing row is authoritative — return it so the client overwrites.
            return SyncPushResult(
                collection=change.collection,
                client_entity_id=change.client_entity_id,
                status="conflict",
                seq=existing.seq,
                server_record=AppSyncService._record_to_public(existing),
            )

        # Quota delta: account for the byte change (and live-count change for
        # tombstone transitions).
        old_bytes = existing.payload_bytes if not existing.deleted else 0
        delta_records = 0
        if change.deleted and not existing.deleted:
            delta_records = -1
        elif not change.deleted and existing.deleted:
            delta_records = 1
        AppSyncService._enforce_quota(
            state, delta_records=delta_records, delta_bytes=new_bytes - old_bytes
        )

        seq = AppSyncService._allocate_seq(state)
        existing.seq = seq
        existing.client_updated_at = client_ts
        existing.server_updated_at = datetime.now(UTC)
        existing.enc_umk_version = change.enc_umk_version
        existing.last_writer_client_id = writer_client_id
        if change.deleted:
            existing.deleted = True
            existing.payload_ciphertext = None
            existing.content_fingerprint = None
            existing.payload_bytes = 0
        else:
            existing.deleted = False
            existing.payload_ciphertext = change.payload_ciphertext
            existing.content_fingerprint = change.content_fingerprint
            existing.payload_bytes = new_bytes
        session.add(existing)
        return SyncPushResult(
            collection=change.collection,
            client_entity_id=change.client_entity_id,
            status="applied",
            seq=seq,
        )

    @staticmethod
    def _enforce_quota(
        state: AppSyncState, *, delta_records: int, delta_bytes: int
    ) -> None:
        projected_records = state.total_records + delta_records
        projected_bytes = state.total_bytes + delta_bytes
        if (
            projected_records > settings.APP_SYNC_QUOTA_RECORDS
            or projected_bytes > settings.APP_SYNC_QUOTA_BYTES
        ):
            raise QuotaExceededError(projected_bytes, projected_records)
        state.total_records = projected_records
        state.total_bytes = projected_bytes

    # ── Pull ─────────────────────────────────────────────────────────────

    @staticmethod
    def pull(
        session: Session,
        user,
        *,
        cursor: int,
        collections: list[str] | None,
        limit: int,
    ) -> tuple[list[SyncRecordPublic], int, bool]:
        """Return (records, next_cursor, has_more) for seq > cursor, seq-ordered.

        Returns payload_ciphertext exactly as stored — no server crypto.
        """
        limit = max(1, min(limit, settings.APP_SYNC_MAX_PULL_LIMIT))
        stmt = select(AppSyncRecord).where(
            AppSyncRecord.user_id == user.id,
            AppSyncRecord.seq > cursor,
        )
        if collections:
            stmt = stmt.where(AppSyncRecord.collection.in_(collections))  # type: ignore[attr-defined]
        stmt = stmt.order_by(AppSyncRecord.seq).limit(limit + 1)  # type: ignore[arg-type]

        rows = list(session.exec(stmt).all())
        has_more = len(rows) > limit
        rows = rows[:limit]

        records = [AppSyncService._record_to_public(r) for r in rows]
        next_cursor = rows[-1].seq if rows else cursor
        return records, next_cursor, has_more

    @staticmethod
    def push_only(
        session: Session,
        user,
        *,
        changes: list[SyncRecordUpsert],
        writer_client_id: str | None,
    ) -> SyncResponse:
        """Push-only convenience: apply ``changes`` and report the new cursor.

        NOTE on ``next_cursor``: the returned value is the user's post-push
        *global* max seq (``state.cursor``), which is **informational only**. It
        is NOT a safe pull cursor — a client that adopted it as its delta cursor
        would skip records written concurrently by other devices. Clients MUST
        advance their real cursor by pulling via ``POST /`` (combined sync) or
        ``POST /pull``. The value is returned because the API contract documents
        ``/push`` as reporting a ``next_cursor``.
        """
        applied = AppSyncService.push(
            session, user, changes=changes, writer_client_id=writer_client_id
        )
        state = AppSyncService.get_state(session, user)
        return SyncResponse(
            applied=applied,
            changes=[],
            next_cursor=state.cursor,
            has_more=False,
            server_time=datetime.now(UTC),
        )

    # ── Combined sync ────────────────────────────────────────────────────

    @staticmethod
    def sync(
        session: Session,
        user,
        *,
        cursor: int,
        changes: list[SyncRecordUpsert],
        collections: list[str] | None,
        limit: int,
        writer_client_id: str | None,
    ) -> SyncResponse:
        """Push ``changes`` then pull records with seq > cursor (push-then-pull)."""
        applied: list[SyncPushResult] = []
        if changes:
            applied = AppSyncService.push(
                session, user, changes=changes, writer_client_id=writer_client_id
            )
        records, next_cursor, has_more = AppSyncService.pull(
            session, user, cursor=cursor, collections=collections, limit=limit
        )
        return SyncResponse(
            applied=applied,
            changes=records,
            next_cursor=next_cursor,
            has_more=has_more,
            server_time=datetime.now(UTC),
        )

    # ── State / wipe ─────────────────────────────────────────────────────

    @staticmethod
    def get_state(session: Session, user) -> SyncStatePublic:
        state = session.get(AppSyncState, user.id)
        cursor = state.current_seq if state else 0
        total_records = state.total_records if state else 0
        total_bytes = state.total_bytes if state else 0

        counts_stmt = (
            select(AppSyncRecord.collection, func.count())  # type: ignore[arg-type]
            .where(
                AppSyncRecord.user_id == user.id,
                AppSyncRecord.deleted == False,  # noqa: E712
            )
            .group_by(AppSyncRecord.collection)  # type: ignore[arg-type]
        )
        collection_counts = {row[0]: row[1] for row in session.exec(counts_stmt).all()}

        return SyncStatePublic(
            cursor=cursor,
            total_records=total_records,
            total_bytes=total_bytes,
            quota_bytes=settings.APP_SYNC_QUOTA_BYTES,
            quota_records=settings.APP_SYNC_QUOTA_RECORDS,
            collection_counts=collection_counts,
        )

    @staticmethod
    def wipe(session: Session, user, *, collections: list[str] | None) -> int:
        """Tombstone the caller's live records (optionally per collection).

        Each live row is converted to a tombstone (``deleted=True``, payload
        cleared) and given a **freshly allocated seq** so peers that already
        pulled the original rows learn of the deletion on their next delta pull
        (§5.1 / §9 — "observe the wipe as tombstones"). Live quota counters are
        reset for the wiped rows. Already-tombstoned rows are left untouched.
        Returns the number of records tombstoned.
        """
        state = AppSyncService._lock_state(session, user.id)

        stmt = select(AppSyncRecord).where(
            AppSyncRecord.user_id == user.id,
            AppSyncRecord.deleted == False,  # noqa: E712
        )
        if collections:
            stmt = stmt.where(AppSyncRecord.collection.in_(collections))  # type: ignore[attr-defined]

        live_rows = list(session.exec(stmt).all())
        if not live_rows:
            session.commit()
            return 0

        now = datetime.now(UTC)
        freed_bytes = 0
        for record in live_rows:
            freed_bytes += record.payload_bytes
            record.seq = AppSyncService._allocate_seq(state)
            record.deleted = True
            record.payload_ciphertext = None
            record.content_fingerprint = None
            record.payload_bytes = 0
            record.server_updated_at = now
            session.add(record)

        state.total_records = max(0, state.total_records - len(live_rows))
        state.total_bytes = max(0, state.total_bytes - freed_bytes)
        state.updated_at = now
        session.add(state)

        session.commit()
        return len(live_rows)

    # ── Encryption / key management (§12.5) ───────────────────────────────

    @staticmethod
    def get_encryption_state(session: Session, user) -> EncryptionStatePublic:
        state = session.get(AppSyncState, user.id)
        active_version = state.active_umk_version if state else 0
        initialized = active_version > 0

        methods = set()
        if initialized:
            methods = set(
                session.exec(
                    select(AppSyncKeyEnvelope.wrap_method).where(
                        AppSyncKeyEnvelope.user_id == user.id,
                        AppSyncKeyEnvelope.umk_version == active_version,
                    )
                ).all()
            )

        devices = session.exec(
            select(AppSyncDevice).where(
                AppSyncDevice.user_id == user.id,
                AppSyncDevice.is_revoked == False,  # noqa: E712
            )
        ).all()

        return EncryptionStatePublic(
            initialized=initialized,
            active_umk_version=active_version,
            has_recovery="recovery" in methods,
            has_passphrase="passphrase" in methods,
            devices=[AppSyncDevicePublic.model_validate(d) for d in devices],
        )

    @staticmethod
    def init_encryption(
        session: Session,
        user,
        *,
        data: EncryptionInitRequest,
    ) -> EncryptionStatePublic:
        """First device only — register the device + initial envelopes.

        Computes the next UMK generation server-authoritatively: one past the
        highest ``enc_umk_version`` any of the user's records have *ever* used
        (live or tombstoned). On a never-initialized account this is 1; after a
        ``reset_encryption`` that left stale v1 records behind it is 2, etc.
        Generation numbers are never reused while records still carry them,
        which would otherwise let a new device mistake stale ciphertext for
        decryptable-under-current data (the AEAD AAD binds to ``umk_version``).
        """
        # Lock the state row so two concurrent inits can't both pass the gate.
        # Mirrors push()'s under-lock E2E check.
        state = AppSyncService._lock_state(session, user.id)
        if state.active_umk_version != 0:
            raise E2EAlreadyInitializedError()

        if not data.envelopes:
            raise InvalidPayloadError("At least one key envelope is required")
        if not any(e.wrap_method == "device" for e in data.envelopes):
            raise InvalidPayloadError("A device envelope is required at init")
        if not any(e.wrap_method == "recovery" for e in data.envelopes):
            raise InvalidPayloadError("A recovery envelope is required at init")

        # Highest generation any record (live or tombstoned) ever used; the new
        # UMK takes the next number so it can't collide with surviving records.
        max_record_version = session.exec(
            select(func.max(AppSyncRecord.enc_umk_version)).where(
                AppSyncRecord.user_id == user.id
            )
        ).one()
        new_version = (max_record_version or 0) + 1

        device = AppSyncService._create_device(session, user.id, data.device)

        for env in data.envelopes:
            device_id = env.device_id
            if env.wrap_method == "device":
                # A device wrap must bind to the device registered in this call.
                if device_id is None:
                    device_id = device.id
                elif device_id != device.id:
                    raise InvalidPayloadError(
                        "A device envelope's device_id must reference the device "
                        "registered in this init request"
                    )
            # The server is authoritative on the generation label — stamp every
            # envelope at new_version so they agree with active_umk_version (and
            # so get_encryption_state's umk_version-filtered methods query finds
            # them). The wrapped key unwraps regardless of the label.
            AppSyncService._create_envelope(
                session,
                user.id,
                env,
                device_id=device_id,
                umk_version=new_version,
            )

        state.active_umk_version = new_version
        state.e2e_initialized_at = datetime.now(UTC)
        state.updated_at = datetime.now(UTC)
        session.add(state)
        session.commit()
        return AppSyncService.get_encryption_state(session, user)

    @staticmethod
    def reset_encryption(session: Session, user) -> EncryptionStatePublic:
        """Tear E2E back down so the account can be set up fresh ("first device"
        again).

        Deletes every key envelope, every device, and any pending pairing relay
        rows, then sets ``active_umk_version`` back to 0 so ``init_encryption``
        is allowed again and ``get_encryption_state`` reports ``initialized =
        False``. Device rows are **hard-deleted** here (full teardown), in
        contrast to single-device ``revoke_device`` which soft-marks
        ``is_revoked=True`` and keeps the row for the audit/UI device list.
        The record log / seq cursor are deliberately left intact —
        callers tombstone records separately via ``wipe`` (``DELETE /``); the
        old ciphertext is undecryptable under the next generation's key anyway.

        Idempotent: a no-op (still returns the state) when E2E was never set up.
        """
        state = AppSyncService._lock_state(session, user.id)

        for env in session.exec(
            select(AppSyncKeyEnvelope).where(AppSyncKeyEnvelope.user_id == user.id)
        ).all():
            session.delete(env)

        for device in session.exec(
            select(AppSyncDevice).where(AppSyncDevice.user_id == user.id)
        ).all():
            session.delete(device)

        for pairing in session.exec(
            select(AppSyncPairing).where(AppSyncPairing.user_id == user.id)
        ).all():
            session.delete(pairing)

        state.active_umk_version = 0
        state.e2e_initialized_at = None
        state.updated_at = datetime.now(UTC)
        session.add(state)
        session.commit()
        return AppSyncService.get_encryption_state(session, user)

    @staticmethod
    def list_envelopes(
        session: Session, user, *, umk_version: int | None
    ) -> list[AppSyncKeyEnvelopePublic]:
        stmt = select(AppSyncKeyEnvelope).where(AppSyncKeyEnvelope.user_id == user.id)
        if umk_version is not None:
            stmt = stmt.where(AppSyncKeyEnvelope.umk_version == umk_version)
        rows = session.exec(stmt).all()
        return [AppSyncKeyEnvelopePublic.model_validate(r) for r in rows]

    @staticmethod
    def add_envelope(
        session: Session, user, *, data: KeyEnvelopeInput
    ) -> AppSyncKeyEnvelopePublic:
        if data.device_id is not None:
            device = session.get(AppSyncDevice, data.device_id)
            if device is None or device.user_id != user.id:
                raise NotFoundError("Device not found")
        # Upsert on the unique (user, wrap_method, umk_version, device_id) tuple.
        existing = session.exec(
            select(AppSyncKeyEnvelope).where(
                AppSyncKeyEnvelope.user_id == user.id,
                AppSyncKeyEnvelope.wrap_method == data.wrap_method,
                AppSyncKeyEnvelope.umk_version == data.umk_version,
                AppSyncKeyEnvelope.device_id == data.device_id,
            )
        ).first()
        if existing is not None:
            existing.wrapped_key = data.wrapped_key
            existing.kdf = data.kdf
            existing.kdf_params = data.kdf_params
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return AppSyncKeyEnvelopePublic.model_validate(existing)

        env = AppSyncService._create_envelope(
            session, user.id, data, device_id=data.device_id
        )
        session.commit()
        session.refresh(env)
        return AppSyncKeyEnvelopePublic.model_validate(env)

    @staticmethod
    def delete_envelope(session: Session, user, *, envelope_id: UUID) -> None:
        env = session.get(AppSyncKeyEnvelope, envelope_id)
        if env is None or env.user_id != user.id:
            raise NotFoundError("Key envelope not found")
        session.delete(env)
        session.commit()

    @staticmethod
    def register_device(
        session: Session, user, *, data: DeviceInput
    ) -> AppSyncDevicePublic:
        device = AppSyncService._create_device(session, user.id, data)
        session.commit()
        session.refresh(device)
        return AppSyncDevicePublic.model_validate(device)

    @staticmethod
    def list_devices(session: Session, user) -> list[AppSyncDevicePublic]:
        rows = session.exec(
            select(AppSyncDevice).where(AppSyncDevice.user_id == user.id)
        ).all()
        return [AppSyncDevicePublic.model_validate(d) for d in rows]

    @staticmethod
    def revoke_device(session: Session, user, *, device_id: UUID) -> None:
        """Mark a device revoked and delete its ``device`` key envelopes.

        Future-confidentiality requires the client to then rotate the UMK
        (§12.7); the server cannot force that but removes the unlock path.
        """
        device = session.get(AppSyncDevice, device_id)
        if device is None or device.user_id != user.id:
            raise NotFoundError("Device not found")
        device.is_revoked = True
        session.add(device)
        # Remove its device-wrap envelopes (CASCADE would also fire on delete,
        # but we keep the device row for the audit/UI list, so delete explicitly).
        envelopes = session.exec(
            select(AppSyncKeyEnvelope).where(
                AppSyncKeyEnvelope.user_id == user.id,
                AppSyncKeyEnvelope.device_id == device_id,
            )
        ).all()
        for env in envelopes:
            session.delete(env)
        session.commit()

    # ── Pairing relay (§12.6) ─────────────────────────────────────────────

    @staticmethod
    def pairing_start(
        session: Session,
        user,
        *,
        new_device_pubkey: str,
        device_label: str | None,
    ) -> PairingStartResponse:
        code = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.APP_SYNC_PAIRING_TTL_SECONDS
        )
        pairing = AppSyncPairing(
            user_id=user.id,
            pairing_code_hash=AppSyncService._hash_code(code),
            new_device_pubkey=new_device_pubkey,
            device_label=device_label,
            status="pending",
            expires_at=expires_at,
        )
        session.add(pairing)
        session.commit()
        return PairingStartResponse(pairing_code=code, expires_at=expires_at)

    @staticmethod
    def pairing_get(session: Session, user, *, code: str) -> PairingStatusPublic:
        pairing = AppSyncService._load_pairing(session, user.id, code)

        sealed_umk = pairing.sealed_umk
        status = pairing.status
        # Single-use delivery: once the joining device retrieves the sealed UMK,
        # consume the row so the blob can't be fetched again within the TTL
        # (§12.6 — "single-use; consumed on success").
        if pairing.status == "completed" and sealed_umk is not None:
            pairing.status = "consumed"
            pairing.sealed_umk = None
            session.add(pairing)
            session.commit()
            status = "completed"  # report the successful delivery to this caller

        return PairingStatusPublic(
            new_device_pubkey=pairing.new_device_pubkey,
            device_label=pairing.device_label,
            status=status,
            sealed_umk=sealed_umk,
            expires_at=pairing.expires_at,
        )

    @staticmethod
    def pairing_complete(
        session: Session, user, *, code: str, sealed_umk: str
    ) -> None:
        pairing = AppSyncService._load_pairing(session, user.id, code)
        if pairing.status != "pending":
            raise PairingError("Pairing request is no longer pending", status_code=409)
        pairing.sealed_umk = sealed_umk
        pairing.status = "completed"
        session.add(pairing)
        session.commit()

    @staticmethod
    def _load_pairing(session: Session, user_id: UUID, code: str) -> AppSyncPairing:
        code_hash = AppSyncService._hash_code(code)
        pairing = session.exec(
            select(AppSyncPairing).where(
                AppSyncPairing.user_id == user_id,
                AppSyncPairing.pairing_code_hash == code_hash,
            )
        ).first()
        if pairing is None:
            raise NotFoundError("Pairing request not found")
        expires_at = pairing.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            if pairing.status != "expired":
                pairing.status = "expired"
                session.add(pairing)
                session.commit()
            raise PairingError("Pairing request has expired", status_code=410)
        return pairing

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    # ── Internal builders ────────────────────────────────────────────────

    @staticmethod
    def _create_device(
        session: Session, user_id: UUID, data: DeviceInput
    ) -> AppSyncDevice:
        device = AppSyncDevice(
            user_id=user_id,
            device_label=data.device_label,
            public_key=data.public_key,
            external_client_id=data.external_client_id,
            last_seen_at=datetime.now(UTC),
        )
        session.add(device)
        session.flush()
        return device

    @staticmethod
    def _create_envelope(
        session: Session,
        user_id: UUID,
        data: KeyEnvelopeInput,
        *,
        device_id: UUID | None,
        umk_version: int | None = None,
    ) -> AppSyncKeyEnvelope:
        # Callers may override the generation label (e.g. init_encryption, where
        # the server is authoritative on which UMK generation this is).
        env = AppSyncKeyEnvelope(
            user_id=user_id,
            wrap_method=data.wrap_method,
            umk_version=data.umk_version if umk_version is None else umk_version,
            wrapped_key=data.wrapped_key,
            kdf=data.kdf,
            kdf_params=data.kdf_params,
            device_id=device_id,
        )
        session.add(env)
        session.flush()
        return env

    # ── Mapping ──────────────────────────────────────────────────────────

    @staticmethod
    def _record_to_public(record: AppSyncRecord) -> SyncRecordPublic:
        return SyncRecordPublic(
            collection=record.collection,
            client_entity_id=record.client_entity_id,
            payload_ciphertext=record.payload_ciphertext,
            enc_umk_version=record.enc_umk_version,
            deleted=record.deleted,
            seq=record.seq,
            server_updated_at=record.server_updated_at,
            last_writer_client_id=record.last_writer_client_id,
        )
