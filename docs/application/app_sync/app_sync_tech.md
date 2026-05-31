# App Sync — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/app_sync/__init__.py` — Re-exports all app sync models
- `backend/app/models/app_sync/app_sync_record.py` — `AppSyncRecord` (table)
- `backend/app/models/app_sync/app_sync_state.py` — `AppSyncState` (table)
- `backend/app/models/app_sync/app_sync_device.py` — `AppSyncDevice` (table), `AppSyncDevicePublic`
- `backend/app/models/app_sync/app_sync_key_envelope.py` — `AppSyncKeyEnvelope` (table), `AppSyncKeyEnvelopePublic`
- `backend/app/models/app_sync/app_sync_pairing.py` — `AppSyncPairing` (table), pairing Pydantic schemas
- `backend/app/models/app_sync/app_sync_schemas.py` — all non-table Pydantic schemas (sync protocol + key management)

### Backend — Routes

- `backend/app/api/routes/app_sync.py` — All endpoints under `/api/v1/app-sync`

### Backend — Services

- `backend/app/services/app_sync/app_sync_service.py` — `AppSyncService` + exception hierarchy

### Backend — Configuration

- `backend/app/core/config.py` — `APP_SYNC_*` constants

### Backend — Migrations

- `backend/app/alembic/versions/d54391bd8cf2_add_app_sync_tables.py` — Creates all five tables (revision `d54391bd8cf2`, down_revision `6e43bbbec5cc`)

---

## Database Schema

### app_sync_record

One row per synced entity. Payload is client-encrypted ciphertext; the server never decrypts it.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | Server-side surrogate key |
| `user_id` | UUID | FK → `user.id` ON DELETE CASCADE, indexed | Owning account; account deletion cascades |
| `collection` | VARCHAR(64) | NOT NULL | e.g. `note`, `job`, `note_folder`; `^[a-z][a-z0-9_]{0,63}$`; unknown-but-valid names accepted |
| `client_entity_id` | VARCHAR(128) | NOT NULL | Client-generated opaque id — UUID or nanoid; `^[A-Za-z0-9_-]{8,128}$` and not a bare integer; validated server-side; stable cross-device identity |
| `seq` | BIGINT | NOT NULL | Per-user monotonic sequence; the sync cursor |
| `payload_ciphertext` | TEXT | nullable | Client AEAD envelope, stored verbatim; `NULL` for tombstones |
| `enc_umk_version` | INTEGER | NOT NULL, default 1 | UMK generation this ciphertext uses; allows mixed versions during key rotation |
| `payload_bytes` | INTEGER | NOT NULL, default 0 | Ciphertext byte size for quota accounting |
| `content_fingerprint` | VARCHAR(88) | nullable | Client keyed HMAC (`HMAC(HKDF(UMK,"fp"), canonical_plaintext)`); compared for equality only; `NULL` for tombstones |
| `deleted` | BOOLEAN | NOT NULL, default false | Tombstone flag; deleted rows keep their seq so deletes propagate |
| `client_updated_at` | TIMESTAMP (tz-naive UTC) | NOT NULL | Client logical clock; the LWW comparison key |
| `server_updated_at` | TIMESTAMP WITH TZ | NOT NULL, default now | When the server last wrote this row |
| `created_at` | TIMESTAMP WITH TZ | NOT NULL, default now | First server write |
| `last_writer_client_id` | VARCHAR(64) | nullable | `external_client_id` from the desktop JWT; `NULL` for web tokens |

**Constraints and indexes:**
- `UNIQUE ix_app_sync_record_natural (user_id, collection, client_entity_id)` — the upsert key
- `INDEX ix_app_sync_record_user_seq (user_id, seq)` — pull hot path
- `INDEX ix_app_sync_record_user_collection (user_id, collection)` — per-collection counts and scoped pulls
- `INDEX ix_app_sync_record_user_id (user_id)` — FK index

**Record lifecycle:**
```
(absent) ──push upsert──► live (deleted=false, ciphertext set)
   live  ──push upsert──► live (new ciphertext, new seq, LWW may reject)
   live  ──push delete──► tombstone (deleted=true, payload_ciphertext=NULL, new seq)
tombstone  ──push upsert──► live again (undelete; e.g. "restore from trash")
tombstone  ──GC (≥180d)──► (row hard-deleted)   # future Phase 3 job
```

### app_sync_state

Per-user singleton holding the sequence counter and quota accounting. Lazily created on first sync.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `user_id` | UUID | PK, FK → `user.id` ON DELETE CASCADE | One row per account |
| `current_seq` | BIGINT | NOT NULL, default 0 | Last allocated per-user sequence; incremented under row lock |
| `total_records` | INTEGER | NOT NULL, default 0 | Live (non-tombstone) record count; for quota |
| `total_bytes` | BIGINT | NOT NULL, default 0 | Sum of `payload_bytes` (ciphertext) over live records; for quota |
| `active_umk_version` | INTEGER | NOT NULL, default 0 | Current UMK generation; `0` = E2E not yet initialised |
| `e2e_initialized_at` | TIMESTAMP WITH TZ | nullable | When E2E was first set up |
| `updated_at` | TIMESTAMP WITH TZ | NOT NULL, default now | — |

### app_sync_device

Registered device public keys for E2E key sharing.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | — |
| `user_id` | UUID | FK → `user.id` ON DELETE CASCADE, indexed | — |
| `device_label` | VARCHAR(128) | NOT NULL | e.g. "Evgeny's MacBook" |
| `public_key` | TEXT | NOT NULL | X25519 public key (base64); private key never leaves the device |
| `external_client_id` | UUID | nullable | Links to `DesktopOAuthClient` when applicable |
| `is_revoked` | BOOLEAN | NOT NULL, default false | — |
| `created_at` | TIMESTAMP WITH TZ | NOT NULL | — |
| `last_seen_at` | TIMESTAMP WITH TZ | nullable | — |

**Index:** `ix_app_sync_device_user_id (user_id)`

### app_sync_key_envelope

Wrapped copies of the UMK — one per unlock method per UMK version (plus one per device for `device`-method wraps).

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | — |
| `user_id` | UUID | FK → `user.id` ON DELETE CASCADE, indexed | — |
| `wrap_method` | VARCHAR(16) | NOT NULL | `device` \| `recovery` \| `passphrase` |
| `umk_version` | INTEGER | NOT NULL | UMK generation this envelope wraps |
| `wrapped_key` | TEXT | NOT NULL | UMK ciphertext — opaque to the server |
| `kdf` | VARCHAR(32) | nullable | `hkdf` (recovery) / `argon2id` (passphrase); `NULL` for `device` method |
| `kdf_params` | JSON | nullable | `{salt, mem, ops, parallelism}` for KDFs that need parameters |
| `device_id` | UUID | FK → `app_sync_device.id` ON DELETE CASCADE, nullable | Set for `device`-method wraps; `NULL` for `recovery` / `passphrase` |
| `created_at` | TIMESTAMP WITH TZ | NOT NULL | — |

**Constraints:**
- `UNIQUE uq_app_sync_key_envelope_unlock (user_id, wrap_method, umk_version, device_id)` — upsert key
- `INDEX ix_app_sync_key_envelope_user_id (user_id)`

### app_sync_pairing

Short-lived blind relay for QR device pairing. The server only stores and relays the `sealed_umk` blob — it never sees the UMK.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | — |
| `user_id` | UUID | FK → `user.id` ON DELETE CASCADE, indexed | Both devices are the same account |
| `pairing_code_hash` | VARCHAR(64) | NOT NULL, indexed | SHA-256 of the raw pairing code; raw code only in the QR/screen |
| `new_device_pubkey` | TEXT | NOT NULL | The joining device's ephemeral X25519 public key |
| `device_label` | VARCHAR(128) | nullable | Optional label provided by the joining device |
| `sealed_umk` | TEXT | nullable | UMK sealed to `new_device_pubkey` by the existing device; `NULL` until completed |
| `status` | VARCHAR(16) | NOT NULL, default `pending` | `pending` → `completed` → `consumed`; or `expired` |
| `expires_at` | TIMESTAMP WITH TZ | NOT NULL | Short TTL (default 5 min, from `APP_SYNC_PAIRING_TTL_SECONDS`) |
| `created_at` | TIMESTAMP WITH TZ | NOT NULL | — |

**Indexes:** `ix_app_sync_pairing_user_id (user_id)`, `ix_app_sync_pairing_code_hash (pairing_code_hash)`

---

## Pydantic Schemas

**File:** `backend/app/models/app_sync/app_sync_schemas.py`

### Sync Protocol Schemas

```python
SyncRecordUpsert        # Request: one mutation in a push batch
    collection: str
    client_entity_id: str
    payload_ciphertext: str | None    # AEAD envelope (§12.3); None only when deleted
    enc_umk_version: int = 1
    content_fingerprint: str | None   # Client keyed HMAC; None only when deleted
    deleted: bool = False
    client_updated_at: datetime
    base_seq: int | None              # Optional optimistic hint; not authoritative

SyncRecordPublic        # Response: one record returned on pull or conflict
    collection: str
    client_entity_id: str
    payload_ciphertext: str | None
    enc_umk_version: int
    deleted: bool
    seq: int
    server_updated_at: datetime
    last_writer_client_id: str | None

SyncPushResult          # Per-record outcome inside SyncResponse.applied
    collection: str
    client_entity_id: str
    status: Literal["applied", "conflict", "unchanged", "rejected"]
    seq: int
    server_record: SyncRecordPublic | None   # Set when status == "conflict"

SyncRequest             # Body for POST /
    cursor: int = 0
    changes: list[SyncRecordUpsert] = []
    collections: list[str] | None = None
    limit: int = 500

PullRequest             # Body for POST /pull
    cursor: int = 0
    collections: list[str] | None = None
    limit: int = 500

PushRequest             # Body for POST /push
    changes: list[SyncRecordUpsert] = []

WipeRequest             # Body for DELETE /
    collections: list[str] | None = None

SyncResponse            # Response for all sync verbs
    applied: list[SyncPushResult] = []
    changes: list[SyncRecordPublic] = []   # Pulled records, seq-ordered
    next_cursor: int
    has_more: bool
    server_time: datetime

SyncStatePublic         # Response for GET /state
    cursor: int
    total_records: int
    total_bytes: int
    quota_bytes: int
    quota_records: int
    collection_counts: dict[str, int] = {}
```

### Key Management Schemas

```python
EncryptionStatePublic   # Response for GET /encryption and POST /encryption/init
    initialized: bool
    active_umk_version: int
    has_recovery: bool
    has_passphrase: bool
    devices: list[AppSyncDevicePublic] = []

KeyEnvelopeInput        # Body for POST /keys
    wrap_method: Literal["device", "recovery", "passphrase"]
    umk_version: int = 1
    wrapped_key: str
    kdf: str | None = None
    kdf_params: dict | None = None
    device_id: UUID | None = None

DeviceInput             # Embedded in EncryptionInitRequest; also body for POST /devices
    device_label: str
    public_key: str
    external_client_id: UUID | None = None

EncryptionInitRequest   # Body for POST /encryption/init
    device: DeviceInput
    envelopes: list[KeyEnvelopeInput] = []   # 422 if empty, or no device envelope, or no recovery envelope
```

Pairing schemas (`PairingStartRequest`, `PairingStartResponse`, `PairingStatusPublic`, `PairingCompleteRequest`) are in `app_sync_pairing.py`.

---

## API Endpoints

All endpoints are under prefix `/api/v1/app-sync`, tag `"App Sync"`, require `CurrentUser`.

### Sync Verbs

| Method | Path | Body | Response | Description |
|--------|------|------|----------|-------------|
| `POST` | `/` | `SyncRequest` | `SyncResponse` | Primary bidirectional sync: push `changes` then pull `seq > cursor` |
| `POST` | `/pull` | `PullRequest` | `SyncResponse` (empty `applied`) | Pull-only. Used for fresh-login bootstrap loop until `has_more == false` |
| `POST` | `/push` | `PushRequest` | `SyncResponse` (empty `changes`) | Push-only. Returns `applied` + informational `next_cursor` (post-push global max seq — NOT a safe pull cursor; use `POST /` or `POST /pull` to advance the real cursor) |
| `GET` | `/state` | — | `SyncStatePublic` | Cursor, quota usage, per-collection live counts |
| `DELETE` | `/` | `WipeRequest?` | `Message` | Tombstone caller's sync data (optionally per collection); resets quota counters; advances seq so peers observe the wipe |

Status codes for sync verbs:
- `409` — push before E2E initialised
- `413` — single record exceeds `APP_SYNC_MAX_PAYLOAD_BYTES` or quota exceeded (structured `detail`)
- `422` — batch exceeds `APP_SYNC_MAX_RECORDS_PER_PUSH`, malformed or bare-integer `client_entity_id`, malformed/missing ciphertext or fingerprint

### Encryption / Key Management

| Method | Path | Body | Response | Description |
|--------|------|------|----------|-------------|
| `GET` | `/encryption` | — | `EncryptionStatePublic` | Init state, active version, available methods, non-revoked devices |
| `POST` | `/encryption/init` | `EncryptionInitRequest` | `EncryptionStatePublic` | First device only: register device + initial envelopes, set `active_umk_version=1`. Serialised under `_lock_state()`. `409` if already initialised. `422` if `device` or `recovery` envelope is absent |
| `GET` | `/keys` | — | `list[AppSyncKeyEnvelopePublic]` | List envelopes; optional `?umk_version=` filter |
| `POST` | `/keys` | `KeyEnvelopeInput` | `AppSyncKeyEnvelopePublic` | Add or replace a wrapped UMK envelope (upsert on natural key) |
| `DELETE` | `/keys/{envelope_id}` | — | `Message` | Remove a wrapped UMK envelope. `404` if not found or owned by another user |

### Devices

| Method | Path | Body | Response | Description |
|--------|------|------|----------|-------------|
| `POST` | `/devices` | `DeviceInput` | `AppSyncDevicePublic` | Register a device public key |
| `GET` | `/devices` | — | `list[AppSyncDevicePublic]` | List all devices (including revoked) for the trusted-devices UI |
| `DELETE` | `/devices/{device_id}` | — | `Message` | Revoke a device: mark `is_revoked=True`, delete its `device`-method envelopes. `404` if not found or owned by another user |

### Device Pairing Relay

| Method | Path | Body | Response | Description |
|--------|------|------|----------|-------------|
| `POST` | `/pairing/start` | `PairingStartRequest` | `PairingStartResponse` | Joining device: creates relay row → returns `pairing_code` + `expires_at` |
| `GET` | `/pairing/{code}` | — | `PairingStatusPublic` | Joining device polls for `sealed_umk`. Consuming the blob marks the row `consumed` (single-use delivery). `404` if not found; `410` if expired |
| `POST` | `/pairing/{code}/complete` | `PairingCompleteRequest` | `Message` | Existing unlocked device posts `sealed_umk` sealed to the joining device's ephemeral pubkey. `409` if status is not `pending`; `410` if expired |

---

## Service Layer

**File:** `backend/app/services/app_sync/app_sync_service.py`

All methods are `@staticmethod`. The service contains no `_encrypt` / `_decrypt` helpers — storing ciphertext verbatim is the literal implementation of "zero-knowledge."

### Exception Hierarchy

```python
AppSyncError(Exception)           # base, HTTP 400
  PayloadTooLargeError             # 413; carries {client_entity_id, payload_bytes, max_payload_bytes}
  QuotaExceededError               # 413; carries {total_bytes, quota_bytes, total_records, quota_records}
  BatchTooLargeError               # 422
  InvalidPayloadError              # 422 (malformed ciphertext / missing fingerprint / bad collection name / malformed / bare-integer entity id)
  E2ENotInitializedError           # 409 (push before init)
  E2EAlreadyInitializedError       # 409 (init called again)
  NotFoundError                    # 404 (device / envelope / pairing not found or wrong owner)
  PairingError                     # 400 or 409 or 410 depending on constructor args
```

The route module's `_handle_service_error()` converts these to `HTTPException` in one place.

### Core Service Methods

```python
AppSyncService.sync(session, user, *, cursor, changes, collections, limit,
                    writer_client_id) -> SyncResponse
    # Push changes, then pull seq > cursor. Primary entry point.
    # Delegates to push() then pull(). Empty changes list skips the push path.

AppSyncService.push(session, user, *, changes, writer_client_id) -> list[SyncPushResult]
    # Validates the full batch up front (a single invalid record aborts with no writes).
    # Acquires SELECT ... FOR UPDATE on app_sync_state.
    # Checks E2E gate (active_umk_version == 0 → E2ENotInitializedError).
    # Applies each change via _apply_one(); commits once.

AppSyncService.push_only(session, user, *, changes, writer_client_id) -> SyncResponse
    # Convenience wrapper used by POST /push: calls push() then get_state()
    # to populate next_cursor; returns SyncResponse with empty changes list.
    # IMPORTANT: next_cursor is the post-push global max seq and is informational
    # only. It is NOT a safe pull cursor — a client that adopted it would skip
    # records written concurrently by other devices. Clients must advance their
    # real cursor by pulling via POST / or POST /pull.

AppSyncService.pull(session, user, *, cursor, collections, limit)
                   -> tuple[list[SyncRecordPublic], int, bool]
    # SELECT ... WHERE user_id = ? AND seq > ? [AND collection IN (?)] ORDER BY seq LIMIT limit+1
    # Returns (records, next_cursor, has_more). Payload ciphertext returned verbatim.

AppSyncService.get_state(session, user) -> SyncStatePublic
    # Reads app_sync_state (or defaults to 0s if absent) + GROUP BY collection
    # for live-record counts in one query.

AppSyncService.wipe(session, user, *, collections) -> int
    # Acquires SELECT ... FOR UPDATE on app_sync_state.
    # Converts all live rows (optionally filtered by collection) to tombstones,
    # each with a freshly allocated seq so peers observe the wipe on next pull.
    # Resets quota counters. Returns count of tombstoned rows.
```

### Sequence Allocation (Gap-Free Per-User Ordering)

```python
AppSyncService._lock_state(session, user_id) -> AppSyncState
    # SELECT ... FOR UPDATE on app_sync_state; lazily creates the row if absent.
    # The row lock serialises a single user's writes, ensuring seq is gap-free.

AppSyncService._allocate_seq(state, n=1) -> int
    # Increments state.current_seq by n in memory (no extra DB round trip).
    # Returns the first allocated seq. Callers pass the locked state row.
```

A global `BIGSERIAL` is not used because its visibility gaps under concurrent transactions could cause a pulling client to miss records whose seq was allocated but not yet committed.

### LWW Logic (`_apply_one`)

For each `SyncRecordUpsert` in a batch:

1. Clamp `client_updated_at` to `server_time + 24h` ceiling.
2. Look up the existing row by `(user_id, collection, client_entity_id)`.
3. **New entity (no existing row):** a delete-of-unknown returns `unchanged`; otherwise enforce quota, allocate seq, insert → `applied`.
4. **No-op short-circuit:** if the incoming fingerprint matches the existing fingerprint (and neither is a tombstone) → `unchanged`, no seq allocated, safe to re-send.
5. **LWW comparison:** `incoming_wins = (incoming_ts > existing_ts) or (incoming_ts == existing_ts and fingerprints differ)`.
6. **Loses:** return `conflict` with `server_record`.
7. **Wins:** adjust quota deltas (byte change + live/tombstone transition), allocate seq, update row → `applied`.

### Key Management Methods

```python
AppSyncService.get_encryption_state(session, user) -> EncryptionStatePublic
AppSyncService.init_encryption(session, user, *, data: EncryptionInitRequest) -> EncryptionStatePublic
    # Acquires _lock_state() first (SELECT ... FOR UPDATE) so concurrent calls
    # cannot both pass the active_umk_version == 0 gate. Re-checks the version
    # under the lock and raises E2EAlreadyInitializedError (409) if non-zero.
    # Requires at least one device envelope AND at least one recovery envelope;
    # raises InvalidPayloadError (422) if either is absent.
    # Creates the device row then the envelopes; sets active_umk_version=1
    # and e2e_initialized_at.
AppSyncService.list_envelopes(session, user, *, umk_version) -> list[AppSyncKeyEnvelopePublic]
AppSyncService.add_envelope(session, user, *, data) -> AppSyncKeyEnvelopePublic
    # Upserts on (user_id, wrap_method, umk_version, device_id) unique key.
AppSyncService.delete_envelope(session, user, *, envelope_id) -> None
AppSyncService.register_device(session, user, *, data) -> AppSyncDevicePublic
AppSyncService.list_devices(session, user) -> list[AppSyncDevicePublic]
AppSyncService.revoke_device(session, user, *, device_id) -> None
    # Sets is_revoked=True; explicitly deletes device-method envelopes for this
    # device (CASCADE would fire on row delete, but device row is kept for the
    # UI list, so envelopes are deleted explicitly).
```

### Pairing Relay Methods

```python
AppSyncService.pairing_start(session, user, *, new_device_pubkey, device_label) -> PairingStartResponse
    # Generates a 24-byte URL-safe random code, stores SHA-256 hash, sets TTL.
AppSyncService.pairing_get(session, user, *, code) -> PairingStatusPublic
    # Loads by SHA-256(code); checks expiry → 410. If status == "completed" and
    # sealed_umk is present: clears sealed_umk, sets status = "consumed" (single-use
    # delivery), reports status = "completed" to this caller.
AppSyncService.pairing_complete(session, user, *, code, sealed_umk) -> None
    # Loads by SHA-256(code); validates status == "pending" → 409 if not;
    # stores sealed_umk; sets status = "completed".
```

---

## Configuration Constants

**File:** `backend/app/core/config.py`

| Constant | Default | Notes |
|----------|---------|-------|
| `APP_SYNC_MAX_PAYLOAD_BYTES` | `1_048_576` (1 MiB) | Per-record ciphertext size ceiling |
| `APP_SYNC_MAX_RECORDS_PER_PUSH` | `500` | Push batch size ceiling |
| `APP_SYNC_MAX_PULL_LIMIT` | `500` | Pull pagination ceiling (independent of push batch) |
| `APP_SYNC_QUOTA_BYTES` | `268_435_456` (256 MiB) | Per-user total ciphertext storage quota |
| `APP_SYNC_QUOTA_RECORDS` | `50_000` | Per-user live record count quota |
| `APP_SYNC_TOMBSTONE_RETENTION_DAYS` | `180` | Minimum days tombstones are retained (GC future work) |
| `APP_SYNC_PAIRING_TTL_SECONDS` | `300` | Pairing relay TTL (5 minutes) |

---

## Alembic Migration

**File:** `backend/app/alembic/versions/d54391bd8cf2_add_app_sync_tables.py`

- **Revision:** `d54391bd8cf2`
- **Down revision:** `6e43bbbec5cc`

**Upgrade** creates five tables in this order (respecting FK dependencies):
1. `app_sync_device` + `ix_app_sync_device_user_id`
2. `app_sync_pairing` + `ix_app_sync_pairing_user_id`, `ix_app_sync_pairing_code_hash`
3. `app_sync_record` + `ix_app_sync_record_natural` (unique), `ix_app_sync_record_user_seq`, `ix_app_sync_record_user_collection`, `ix_app_sync_record_user_id`
4. `app_sync_state` (PK = `user_id`)
5. `app_sync_key_envelope` + `uq_app_sync_key_envelope_unlock` (unique), `ix_app_sync_key_envelope_user_id`

All foreign keys use `ON DELETE CASCADE` so account deletion removes all sync data and all key envelopes.

**Downgrade** drops tables in reverse dependency order: `app_sync_key_envelope`, `app_sync_state`, `app_sync_record`, `app_sync_pairing`, `app_sync_device`.

---

## Zero-Knowledge Invariant

The service contains no `_encrypt` / `_decrypt` helpers. There is no code path that reads or transforms `payload_ciphertext` or `wrapped_key` for cryptographic purposes. Both columns are stored by assignment and returned by read — identical to any text field. The server's role is ordering (seq), conflict resolution (LWW on `client_updated_at`), and quota accounting (ciphertext `payload_bytes`). None of these operations require plaintext.

The `content_fingerprint` column is compared only for equality (`==`) in the no-op short-circuit check and nowhere else. The server cannot reverse it, compute it independently, or use it to learn anything about the plaintext.

---

## Input Validation Rules

| Check | Rejection | Notes |
|-------|-----------|-------|
| `client_entity_id` matches `^[A-Za-z0-9_-]{8,128}$` and is not a bare integer (`^\d+$`) | `422 InvalidPayloadError` | Cross-device collision footgun-blocker (§3.5) |
| `collection` matches `^[a-z][a-z0-9_]{0,63}$` | `422 InvalidPayloadError` | Unknown-but-valid names accepted (forward compatible) |
| Non-tombstone must have `payload_ciphertext` | `422 InvalidPayloadError` | — |
| Non-tombstone must have `content_fingerprint` | `422 InvalidPayloadError` | — |
| `payload_ciphertext` byte size ≤ `APP_SYNC_MAX_PAYLOAD_BYTES` | `413 PayloadTooLargeError` | Measured on UTF-8 encoded ciphertext string |
| Batch size ≤ `APP_SYNC_MAX_RECORDS_PER_PUSH` | `422 BatchTooLargeError` | Checked before any record validation |
| `active_umk_version == 0` on push | `409 E2ENotInitializedError` | Checked under the seq lock |
| Projected totals ≤ quota | `413 QuotaExceededError` | Checked per-record against projected post-apply totals |
| `client_updated_at` ≤ server_time + 24h | Clamped (not rejected) | Bound clock-skew abuse of LWW |

---

## Infrastructure Notes

`/api/v1/app-sync*` falls under the existing `/api/` nginx location block — no new proxy configuration required. See [Nginx Setup](../../infrastructure/nginx_setup.md).

The web SPA does not consume these endpoints (the server cannot decrypt the content). Client regeneration (`bash scripts/generate-client.sh`) keeps the OpenAPI spec in sync for spec hygiene and any optional Settings → Security control.

---

*Last updated: 2026-05-31*
