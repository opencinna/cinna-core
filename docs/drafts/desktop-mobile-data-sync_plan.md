# Native Client Data Sync — Implementation Plan

> **Feature name:** `native-client-data-sync`
> **Status:** Draft / architecture
> **Audience:** developer / LLM implementing the server side in **cinna-core**
> **Scope note:** This plan covers **only** the cinna-core server side — the storage substrate and the sync protocol that native clients (Cinna Desktop today, Cinna Mobile later) use to back up and synchronise their profile-scoped data. It does **not** add any cinna-core SPA UI for viewing/editing notes or jobs, and it does **not** cover the desktop/mobile client implementation (that lives in the `cinna-desktop` repo). The client contract is described so the server can be built to fit it.

---

## 1. Overview

Cinna Desktop stores all of a user's personal working data — **notes**, **jobs** (reusable work specs), their **folders**, **job runs**, and **chats** — in a local profile-scoped SQLite database. Today that data is trapped on one device: log out (which deletes the local cache) and it's gone; install on a second device and you start empty.

This feature adds a **server-side sync substrate** to cinna-core so that a logged-in native client can push its local data to the server and pull it back on any other device signed into the same Cinna account. The canonical scenario:

> User logs into Cinna Desktop → creates jobs, runs chats, makes notes → logs out (local cache wiped) → logs back in (same account, same or different device) → all data is synced down seamlessly. A future Cinna Mobile app signing into the same account sees the same data.

**Privacy principle (the design's north star)**

> Everything a user does on their own device is **private to them**. The sync server (cinna-core) is **zero-knowledge** — it stores only ciphertext it cannot decrypt and never sees note bodies, job prompts, chat context, or any payload content. The **only** information anyone other than the user can see is what the user *deliberately sends to an external service* — most notably, the specific messages routed to an **A2A agent** (which that agent's owner must, by definition, be able to read to answer them). The *rest* of the chat — its surrounding local context, the user's reasoning, attached notes, other turns — stays private on the device. See [§4.3 Privacy boundary](#43-privacy-boundary--private-on-device-vs-deliberately-shared).

**Core capabilities**

- A **generic, opaque per-user document store** (`app_sync_record`) — the server stores client-**encrypted** documents keyed by `(user_id, collection, client_entity_id)` without ever seeing their plaintext or shape.
- **Mandatory end-to-end encryption** — there is **no** server-readable mode. Payloads are encrypted on-device with a key the server never holds; the server performs *zero* crypto on payloads (§12).
- **Frictionless key sharing across devices** — a new device joins by **scanning a QR code** from an existing device (a blind server relay transfers the key as ciphertext), and every user holds a **recovery key** (mnemonic / downloadable file / QR) as the offline backup. An optional passphrase is offered for users who prefer typing a secret (§12.6).
- A **delta sync protocol** built on a per-user monotonic sequence cursor: clients pull "everything changed since cursor N" and push their local mutations in batches.
- **Tombstones** so deletes propagate across devices.
- **Last-Writer-Wins (LWW)** conflict resolution keyed by a client logical timestamp (server-visible metadata only — which is *why* E2E is drop-in), with the authoritative state returned so the loser can reconcile.
- **Per-user quotas** and **payload size limits** (computed on ciphertext — the server never sees plaintext).
- Reuse of the **existing native-client auth** (`CurrentUser` / Desktop OAuth tokens, including the live-revocation check) — no new auth surface.
- A clear privacy boundary for the heavier collections (**chats**, **job runs**): agent conversations that *necessarily* live server-side (A2A) are referenced by an encrypted pointer, never re-exposed; everything device-local is E2E.

**High-level flow**

```
 Cinna Desktop                         cinna-core                      Cinna Mobile
 (SQLite, profile)                     (Postgres)                      (future)
       │                                   │                                │
       │  POST /app-sync  {cursor, changes[]}  │                                │
       ├──────────────────────────────────►  apply LWW, assign seq,        │
       │                                   │  store ciphertext (CANNOT read),│
       │                                   │  bump quota                    │
       │  ◄────  {applied[], changes[],    │                                │
       │          next_cursor, has_more}   │                                │
       │                                   │                                │
       │             (user logs in on a fresh device) ─────────────────────┤
       │                                   │  POST /app-sync {cursor:0}         │
       │                                   ◄────────────────────────────────┤
       │                                   │  full snapshot (paginated) ───►│
```

---

## 2. Architecture Overview

### 2.1 The central design decision: opaque document store, not a typed mirror

There are two ways the server could hold this data:

| Approach | What it means | Verdict |
|----------|---------------|---------|
| **Typed mirror** | cinna-core defines `desktop_note`, `desktop_job`, `desktop_job_folder`, … tables that mirror the desktop schema field-for-field, with typed CRUD, validation, relationships. | ❌ Rejected. The server never reads or acts on this data (no UI, explicitly out of scope). Every desktop schema change would force a cinna-core migration + client regen. High coupling, high cost, zero benefit. |
| **Opaque document store** (chosen) | A single `app_sync_record` table stores client-authored JSON blobs partitioned by `collection`. The server is a dumb, versioned, conflict-resolving substrate; the client owns the schema. | ✅ Chosen. Desktop and mobile only need to agree on the JSON shape per collection. cinna-core ships once and never migrates when the note/job schema evolves. Matches the "server is a sync substrate" intent exactly. |

The opaque store is the right call precisely **because** the constraint is "we care only about app sync, not exposing this in cinna-core UI." The server's job is durability + ordering + conflict resolution, not interpretation.

### 2.2 Components

```
Native Client (Desktop / Mobile)
        │   JWT (web or Desktop OAuth token — same CurrentUser dep)
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  POST /api/v1/app-sync          AppSyncService.sync()               │
│  POST /api/v1/app-sync/pull       ├── allocate per-user seq (row lock) │
│  POST /api/v1/app-sync/push       ├── LWW apply (client_updated_at)    │
│  GET  /api/v1/app-sync/state      ├── store ciphertext VERBATIM        │
│  DELETE /api/v1/app-sync          │     (NO payload crypto — blind)    │
│  + key-mgmt & pairing (§12.5)     ├── quota + size enforcement         │
│                               └── tombstones + pagination          │
└──────────────────────────────────────────────────────────────────┘
        │                                   │
        ▼                                   ▼
 app_sync_record (Postgres)      app_sync_state (Postgres)
 (one row per synced entity,         (per-user seq counter, quota,
  opaque client-encrypted             active UMK version)
  payload — server-blind)        app_sync_key_envelope / app_sync_device
                                  (wrapped keys + device pubkeys; §12.4)
```

- **Models:** `backend/app/models/app_sync/app_sync_record.py`, `backend/app/models/app_sync/app_sync_state.py`
- **Service:** `backend/app/services/app_sync/app_sync_service.py`
- **Routes:** `backend/app/api/routes/app_sync.py` (tag `"App Sync"`, prefix `/api/v1/app-sync`), registered in `backend/app/api/main.py`
- **Auth:** `CurrentUser` (works transparently with web JWTs and Desktop OAuth tokens; the desktop live-revocation check in `get_current_user` already applies)

### 2.3 Data flow (delta sync)

1. **Cursor.** Each client persists the highest server `seq` it has applied locally (`sync_cursor`). On fresh login the cursor is `0`.
2. **Pull.** Client asks for all records with `seq > cursor` (including tombstones), in `seq` order, paginated. It applies each to its local store and advances `cursor` to `next_cursor`.
3. **Push.** Client sends its local mutations (upserts + deletes) tagged with `client_updated_at`. The server allocates new `seq` values, applies LWW, and returns the authoritative record for any conflict so the client can overwrite its local copy.
4. **Combined.** The primary `POST /api/v1/app-sync` does push-then-pull in one round trip, which is what an offline-first client wants: "here are my changes, give me yours."

### 2.4 Integration points

- **[Desktop Auth](../application/desktop_auth/desktop_auth.md)** — provides the access tokens. The `client_kind="desktop"` + `external_client_id` JWT claims are read for **device attribution** (which device last wrote a record) and the existing live-revocation check secures the endpoints for free.
- **[External Agent Access](../application/external_agent_access/external_agent_access.md)** — the sync surface is a sibling under `/api/v1/external`-style native-client APIs. For **agent chats**, the conversation already persists as cinna-core `Session` rows reachable via `GET /api/v1/external/sessions`; sync stores only a pointer (`external_session_id`) rather than duplicating message bodies.
- **[Agent Sessions](../application/agent_sessions/agent_sessions.md)** — the `Session` model that backs agent-chat restore.
- **[Tasks](../application/input_tasks/input_tasks.md)** — `cinna_task` jobs/runs already point at server-side tasks (`short_code`); their run records are portable as-is.
- **[Backend patterns](../development/backend/backend_development_llm.md)** — SQLModel models, service layer, `CurrentUser`/`SessionDep`, Alembic.
- **[Security](../development/security/security.md)** — the server is **zero-knowledge** for payloads: all encryption happens client-side (§12), so unlike credentials (which use server-side Fernet in `backend/app/core/security.py`), the sync server holds **no** key that can open a payload.

---

## 3. Data Models

### 3.1 `app_sync_record` table

**File:** `backend/app/models/app_sync/app_sync_record.py`

One row per synced entity (a note, a job, a folder, a chat pointer, …). The body is **client-encrypted ciphertext** — opaque to the server, which never decrypts it.

| Column | Type | Constraints | Default | Notes |
|--------|------|-------------|---------|-------|
| `id` | UUID | PK | `uuid4` | Server-side surrogate key |
| `user_id` | UUID | FK → `user.id` `ON DELETE CASCADE`, NOT NULL, indexed | — | Owning account; account deletion wipes all sync data |
| `collection` | VARCHAR(64) | NOT NULL, indexed | — | Logical bucket: `note`, `note_folder`, `job`, `job_folder`, `job_run`, `chat`, … Validated against a known set but new values are accepted (forward-compatible) |
| `client_entity_id` | VARCHAR(128) | NOT NULL | — | **Client-generated globally-unique UUID** (see §3.5). Minted once at creation on whichever device and carried forever. **Never** a device-local autoincrement/rowid — that would collide across devices. The client owns id generation so creates are offline-capable and idempotent |
| `seq` | BIGINT | NOT NULL, indexed | — | Per-user monotonic sequence assigned at write time. The sync cursor |
| `payload_ciphertext` | TEXT | nullable | NULL | **Client-encrypted** AEAD envelope (§12.3) stored **verbatim**; the server cannot decrypt it. `NULL` for tombstones |
| `enc_umk_version` | INTEGER | NOT NULL | 1 | UMK generation this ciphertext was encrypted under; lets v(n)/v(n+1) rows coexist during key rotation (§12.7) |
| `payload_bytes` | INTEGER | NOT NULL | 0 | **Ciphertext** byte size, for quota accounting (server has only ciphertext) |
| `content_fingerprint` | VARCHAR(88) | nullable | NULL | **Client-supplied** keyed fingerprint `HMAC(HKDF(UMK,"fp"), canonical_plaintext)` — stable across re-encryptions, opaque/unforgeable to the server. The server compares it only for equality to skip no-op writes; it cannot reverse it or confirm a guessed plaintext. Replaces a server-computed plaintext hash (impossible here). `NULL` for tombstones |
| `deleted` | BOOLEAN | NOT NULL | `false` | Tombstone flag; a deleted record keeps its row (and `seq`) so the delete propagates |
| `client_updated_at` | TIMESTAMP (tz-naive UTC) | NOT NULL | — | Client logical clock; the **LWW comparison key** (cleartext metadata — the server needs no plaintext to resolve conflicts) |
| `server_updated_at` | TIMESTAMP | NOT NULL | `utcnow` | When the server last wrote this row |
| `created_at` | TIMESTAMP | NOT NULL | `utcnow` | First server write |
| `last_writer_client_id` | VARCHAR(64) | nullable | NULL | `external_client_id` of the desktop device that last wrote (from JWT); for attribution/debugging. `NULL` for web tokens |

**Constraints & indexes**

- `UNIQUE (user_id, collection, client_entity_id)` — the natural upsert key. A push targets this tuple.
- `INDEX ix_app_sync_record_user_seq (user_id, seq)` — the hot path for delta pulls (`WHERE user_id = ? AND seq > ? ORDER BY seq`).
- `INDEX ix_app_sync_record_user_collection (user_id, collection)` — for collection-scoped pulls and per-collection counts.
- `seq` is unique **per user** (not globally); see §3.3 for allocation.

**Lifecycle**

```
(absent) ──push upsert──► live (deleted=false, ciphertext set)
   live  ──push upsert──► live (new ciphertext, new seq, LWW may reject)
   live  ──push delete──► tombstone (deleted=true, payload_ciphertext=NULL, new seq)
 tombstone ─push upsert─► live again (undelete; e.g. desktop "restore from trash")
 tombstone ──GC (≥180d)─► (row hard-deleted)   # optional, see §9
```

### 3.2 `app_sync_state` table

**File:** `backend/app/models/app_sync/app_sync_state.py`

Per-user singleton holding the sequence counter and quota accounting. Lazily created on first sync.

| Column | Type | Constraints | Default | Notes |
|--------|------|-------------|---------|-------|
| `user_id` | UUID | PK, FK → `user.id` `ON DELETE CASCADE` | — | One row per account |
| `current_seq` | BIGINT | NOT NULL | 0 | Last allocated per-user sequence; incremented under row lock on every write |
| `total_records` | INTEGER | NOT NULL | 0 | Live (non-tombstone) record count, for quota |
| `total_bytes` | BIGINT | NOT NULL | 0 | Sum of `payload_bytes` (ciphertext) over live records, for quota |
| `active_umk_version` | INTEGER | NOT NULL | 0 | Current User-Master-Key generation (§12). `0` = E2E not yet initialised (no records may be pushed until the first device runs `POST /encryption/init`); `≥1` after setup |
| `e2e_initialized_at` | TIMESTAMP | nullable | NULL | When the account's E2E was first set up (the first device uploaded its envelopes) |
| `updated_at` | TIMESTAMP | NOT NULL | `utcnow` | — |

### 3.3 Sequence allocation (gap-free per-user ordering)

The cursor must be **gap-free per user** or a client can miss a record: if it pulls `seq=5` while a concurrent `seq=4` is still uncommitted, it will never re-request `seq=4`.

**Approach:** allocate `seq` from `app_sync_state.current_seq` inside the push transaction, holding a `SELECT ... FOR UPDATE` row lock on the user's `app_sync_state` row. This serialises a single user's writes (negligible contention — it's one person), guarantees `seq` is assigned in commit order, and is gap-free. A global `BIGSERIAL` is explicitly **not** used because its visibility gaps under concurrency reintroduce the missed-record problem.

### 3.4 Schema (Pydantic) classes

```
SyncRecordUpsert        — collection, client_entity_id,
                          payload_ciphertext (str|None),      # AEAD envelope (§12.3); None only when deleted
                          enc_umk_version (int),              # which UMK gen the ciphertext uses
                          content_fingerprint (str|None),     # client keyed HMAC; None only when deleted
                          deleted (bool=false), client_updated_at (datetime),
                          base_seq (int|None)            # client's last-known seq, optional optimistic check
SyncRecordPublic        — collection, client_entity_id,
                          payload_ciphertext (str|None), enc_umk_version (int),
                          deleted, seq, server_updated_at, last_writer_client_id
SyncPushResult          — collection, client_entity_id, status (Literal[
                              "applied","conflict","unchanged","rejected"]),
                          seq, server_record (SyncRecordPublic|None)   # set when status=="conflict"
SyncRequest             — cursor (int=0), changes (list[SyncRecordUpsert]=[]),
                          collections (list[str]|None), limit (int=500)
SyncResponse            — applied (list[SyncPushResult]),
                          changes (list[SyncRecordPublic]),   # pulled records, seq-ordered
                          next_cursor (int), has_more (bool),
                          server_time (datetime)
SyncStatePublic         — cursor (int), total_records, total_bytes,
                          quota_bytes, quota_records,
                          collection_counts (dict[str,int])
```

The wire payload is **opaque ciphertext** (`payload_ciphertext`), never plaintext — the server validates only its **size** and that it is well-formed base64/string, never its shape or content. `content_fingerprint` is compared only for equality. There is no plaintext `payload` field anywhere in the API.

### 3.5 Entity identity across devices — opaque client ids (critical)

> **Amendment (supersedes original UUID-only rule):** this section was updated to accept any well-formed opaque client id, not only UUIDs. See `drafts/app-sync-client-id-format-fix.md` for the rationale.

The same account is expected to be live on **multiple devices simultaneously** (e.g. desktop + mobile, or two desktops). The sync identity of an entity is `client_entity_id`, and it is what the upsert key `(user_id, collection, client_entity_id)` and **all intra-sync references** (a note's `folderId`, a job's `folderId`, a `job_run`'s `jobEntityId`, etc.) are built on. This forces two hard rules:

1. **`client_entity_id` MUST be a client-generated globally-unique opaque id — URL-safe `[A-Za-z0-9_-]`, 8–128 chars, and **not** a bare integer (the device-local-rowid footgun). UUIDs and nanoids both qualify.** It must be minted once at the moment the entity is created and never changed. It must **not** be a device-local autoincrement integer or SQLite `rowid`. If it were, device A and device B would each independently create a note with local id `5`; both would push `client_entity_id = "5"`; they would collide on the natural key, and LWW would silently destroy one device's note. A 21-char nanoid (the format Cinna Desktop already uses) carries ~126 bits of entropy — more than UUIDv4's 122 bits — so cross-device collision is as statistically impossible as with a UUID; collision-freedom is the client's responsibility either way.

2. **Every reference *between synced entities* inside a payload must use these ids, not local ids.** A note's `folderId` must be the note-folder's `client_entity_id`, not a local foreign key — otherwise the parent/child link breaks the moment the data lands on a second device. (References to *device-local, non-synced* resources — local agent/MCP/chat-mode ids — are a separate concern; those are allowed to dangle, see §6.)

**Server guardrail.** The server **validates that `client_entity_id` matches `^[A-Za-z0-9_-]{8,128}$` and is not a bare integer (`^\d+$`)** — anything else is rejected with `422`. This is a deliberate footgun-blocker: it makes it *impossible* for a client to accidentally sync device-local rowids. UUIDs and nanoids both pass the check.

**Client migration note (desktop).** Cinna Desktop already keys all syncable entities on nanoids, so **no local migration is needed** — existing nanoid ids satisfy the opaque-id rule and can be pushed as `client_entity_id` without any change to the desktop schema.

> Note the two id spaces are orthogonal and both needed: **`client_entity_id` (UUID)** is the stable *cross-device identity*; **`seq` (per-user incremental)** is only the *ordering cursor for delta pulls*. `seq` being incremental is fine — it is assigned by the server and never used as an identity, so it cannot cause cross-device collisions.

---

## 4. Security Architecture

### 4.1 Authentication & authorization

- All endpoints require `CurrentUser`. They work transparently with both web JWTs and Desktop OAuth access tokens.
- The Desktop OAuth **live-revocation check** in `get_current_user` already runs for `client_kind="desktop"` tokens: disconnecting a device from Settings → Security blocks its next sync call with `401 Desktop session has been revoked`. No extra work needed.
- **Strict ownership:** every query is filtered by `user_id == current_user.id`. There is no concept of sharing in this store; a user can only ever read/write their own records. No record id is ever accepted without the `user_id` filter.

### 4.2 End-to-end encryption — the only mode (zero-knowledge server)

There is **one** encryption mode: **mandatory E2E**. The server never holds a key that can open a payload and performs **no** cryptography on payloads — it stores client ciphertext verbatim and returns it verbatim. There is deliberately **no server-readable fallback**; even a fully compromised server (or its operator, or a backup thief, or a subpoena) yields only undecryptable blobs.

- Payloads are encrypted on-device with a key derived from the per-account **User Master Key (UMK)**, which exists only on the user's devices. The full key hierarchy, ciphertext envelope, key-sharing UX, rotation, and threat model are in **[§12 End-to-End Encryption & Key Management](#12-end-to-end-encryption--key-management)**.
- Because LWW resolves conflicts using only **cleartext metadata** (`client_updated_at`, `seq`) and the no-op check uses a **client-supplied keyed fingerprint**, the server needs *no* plaintext to do its entire job. That is what makes a zero-knowledge server practical here rather than a compromise.
- **Consequence — recovery is the user's, not the server's.** Lose every device *and* the recovery key *and* the passphrase, and the data is unrecoverable by anyone, including us. This is intrinsic to the guarantee and must be surfaced at setup (§12.6).
- **Gate:** until an account's E2E is initialised (`app_sync_state.active_umk_version == 0`), `push` is rejected — a device must run `POST /encryption/init` (first device) or unlock via pairing/recovery (subsequent devices) before it can write.

### 4.3 Privacy boundary — private-on-device vs deliberately shared

The product promise is: *what you do on your private device is yours alone.* The single, explicit exception is **data you deliberately hand to an external service**, because that service must read it to act on it. Concretely:

| Data | Visibility | Why |
|------|-----------|-----|
| Notes, job configs, folders, local-LLM chat history, orchestration context, job-run metadata | **Private (E2E)** — only the user's devices | Never leaves the device except as ciphertext the sync server can't read |
| The specific message(s) a user sends **to an A2A agent**, and that agent's replies | **Visible to that agent's owner** (and, for agents hosted on cinna-core, to the platform) | The agent literally cannot answer a prompt it cannot read. This is the user's deliberate act of contacting an external party |
| What a **local LLM provider** (the user's own Anthropic/OpenAI key) sees at inference | **Visible to that provider** | The device calls the provider directly; cinna-core is not in the loop and never sees it |

The architecture enforces this boundary cleanly (detail in §7):
- **Agent (A2A) conversations already live server-side** as cinna-core `Session`s — *by necessity*, because the agent produced/consumed them. The sync store does **not** re-store that content; it keeps only an **E2E-encrypted pointer** (`externalSessionId` + local metadata). So the agent-visible turns stay in the Session subsystem (the accepted exception) and the user's private wrapper/context stays E2E. Nothing private is doubly-exposed, and nothing exposed is doubly-stored.
- **Orchestrated multi-agent chats:** only the actual tool-call payload routed to a given agent is visible to that agent. The local conductor's reasoning, other turns, and attached notes are private context and, if synced, ride the E2E store.

> In short: the **sync server sees nothing**; **external agents see only what's sent to them**; **LLM providers see only what the device sends them**. There is no fourth party.

### 4.4 Input validation & limits

- **Max ciphertext size:** reject any single record whose `payload_ciphertext` exceeds `APP_SYNC_MAX_PAYLOAD_BYTES` (default **1 MiB**, measured on ciphertext) with `413`. Notes/jobs are kilobytes; this is a safety ceiling.
- **Max batch size:** reject a push with more than `APP_SYNC_MAX_RECORDS_PER_PUSH` (default **500**) changes with `422`.
- **Per-user quota:** `total_records ≤ APP_SYNC_QUOTA_RECORDS` (default **50,000**) and `total_bytes ≤ APP_SYNC_QUOTA_BYTES` (default **256 MiB**, ciphertext). Exceeding returns `413` with a structured `detail` so the client can surface "sync storage full." Checked against projected post-apply totals.
- **Ciphertext well-formedness:** on a non-tombstone upsert, `payload_ciphertext` must be present, valid base64/string, within size cap, and accompanied by `content_fingerprint` + `enc_umk_version`. The server **cannot** validate decryptability — by design. It rejects only structurally malformed input (`422`).
- **Collection name:** `^[a-z][a-z0-9_]{0,63}$`; unknown-but-valid names accepted (forward compatibility) but logged at debug.
- **`client_entity_id` format:** must be a well-formed opaque client id — URL-safe `[A-Za-z0-9_-]`, 8–128 chars, and **not** a bare integer (`^\d+$`); anything else → `422`. UUIDs and nanoids both qualify. This is the guardrail that makes it impossible to accidentally sync device-local rowids and cause cross-device collisions. *(Amended from UUID-only per `drafts/app-sync-client-id-format-fix.md`.)*
- **`client_updated_at` sanity:** values far in the future (> server time + 24 h) are clamped to server time to bound clock-skew abuse of LWW.

### 4.5 Sensitive-data handling

- Payloads are **never logged**. Log only `(user_id, collection, client_entity_id, seq, payload_bytes, status)`.
- Error responses never echo payload contents.
- Rate limiting: apply the platform's standard per-user/IP rate limit to `/api/v1/app-sync*` (sync is chatty but bounded; a misbehaving client should be throttled, not allowed to hammer the DB).

---

## 5. Backend Implementation

### 5.1 API routes

**File:** `backend/app/api/routes/app_sync.py` · **Prefix:** `/api/v1/app-sync` · **Tag:** `"App Sync"` · **Auth:** `CurrentUser` on all.

| Method | Path | Body | Response | Description |
|--------|------|------|----------|-------------|
| `POST` | `/` | `SyncRequest` | `SyncResponse` | **Primary bidirectional sync.** Applies `changes` (push), then returns records with `seq > cursor` (pull), paginated by `limit`. One round trip. |
| `POST` | `/pull` | `{cursor, collections?, limit?}` | `SyncResponse` (empty `applied`) | Pull-only (download). Used for the fresh-login bootstrap loop until `has_more == false`. |
| `POST` | `/push` | `{changes}` | `SyncResponse` (empty `changes`) | Push-only (upload). Returns `applied` + the new `next_cursor`. |
| `GET` | `/state` | — | `SyncStatePublic` | Lightweight bootstrap: current cursor, quota usage, per-collection counts. Lets a client decide whether a full pull is needed. |
| `DELETE` | `/` | `{collections?}` | `Message` | **Wipe** all of the caller's sync data (optionally scoped to collections). Hard-deletes rows and resets quota counters; bumps `current_seq` so other devices observe the wipe as tombstones on next pull. Privacy / "forget my synced data" control. |

In addition, the **key-management & device-pairing endpoints** (`/encryption*`, `/keys*`, `/devices*`, `/pairing*`) live under the same prefix and are **core, not optional** — fully specified in §12.5. They store and relay only opaque ciphertext.

**Route behaviour notes**

- `/pull` and `/push` are thin wrappers that call the same service method with an empty other half — they exist so clients with a strict download-then-upload phase ordering have clean verbs. `POST /` is the recommended default.
- Pagination: pull returns at most `limit` records ordered by `seq ASC`; `has_more` is true when more remain. The client loops, advancing `cursor = next_cursor`, until `has_more == false`.
- Push is **atomic per request**: all changes in one batch apply in a single transaction (seq allocation under the user's `app_sync_state` lock). A validation failure on any record fails the batch with no writes (client fixes and retries) — except per-record LWW "conflict"/"unchanged" outcomes, which are normal results, not errors.

### 5.2 Service layer

**File:** `backend/app/services/app_sync/app_sync_service.py`

Follows the project's service pattern with a domain exception hierarchy (mirroring `UserDashboardService`), converted to HTTP by a `_handle_service_error()` helper in the route module.

> **The server does no payload cryptography.** There are no `_encrypt`/`_decrypt` helpers — the service stores `payload_ciphertext` verbatim and returns it verbatim. This is the literal implementation of "zero-knowledge."

```python
class AppSyncError(Exception): ...            # base, 400
class PayloadTooLargeError(AppSyncError): ...  # 413
class QuotaExceededError(AppSyncError): ...    # 413
class BatchTooLargeError(AppSyncError): ...    # 422
class InvalidPayloadError(AppSyncError): ...   # 422 (malformed ciphertext / missing fingerprint)
class E2ENotInitializedError(AppSyncError): ...# 409 (push before POST /encryption/init)

class AppSyncService:

    @staticmethod
    def sync(session, user, *, cursor, changes, collections, limit,
             writer_client_id) -> SyncResponse:
        """Push `changes` then pull records with seq > cursor. Primary entry point."""

    @staticmethod
    def push(session, user, *, changes, writer_client_id) -> list[SyncPushResult]:
        """Apply a batch of upserts/deletes under the user's seq lock.
        Rejects with E2ENotInitializedError if active_umk_version == 0.
        Per record (NO decryption — ciphertext is opaque):
          - locate existing row by (user_id, collection, client_entity_id)
          - LWW: if existing wins by client_updated_at → status='conflict', return server_record
          - if content_fingerprint unchanged and not a delete → status='unchanged' (no seq burn)
          - else allocate next seq, store payload_ciphertext VERBATIM + enc_umk_version
                 + content_fingerprint, adjust quota (ciphertext bytes) → status='applied'
        Enforces MAX_RECORDS_PER_PUSH, MAX_PAYLOAD_BYTES (ciphertext), quota,
        ciphertext well-formedness."""

    @staticmethod
    def pull(session, user, *, cursor, collections, limit) -> tuple[list[SyncRecordPublic], int, bool]:
        """Return (records, next_cursor, has_more) for seq > cursor, seq-ordered.
        Returns payload_ciphertext as stored — the client decrypts. No server crypto."""

    @staticmethod
    def get_state(session, user) -> SyncStatePublic:
        """Cursor + quota + per-collection live counts (single GROUP BY query)."""

    @staticmethod
    def wipe(session, user, *, collections) -> int:
        """Hard-delete the caller's records (optionally per collection),
        reset quota counters, advance seq so peers see the change. Returns count."""

    # -- private --
    @staticmethod
    def _allocate_seq(session, user_id, n) -> int:
        """SELECT ... FOR UPDATE on app_sync_state; current_seq += n; return first allocated."""
```

Key-management methods (`init_encryption`, envelope CRUD, device register/revoke, pairing relay) live in the same service and are specified in §12.5; they too only store/relay opaque blobs.

**LWW comparison rule (the conflict heart):** an incoming upsert *wins* (is applied) iff `incoming.client_updated_at > existing.client_updated_at`, or they are equal and `incoming.content_fingerprint != existing.content_fingerprint` (tie-break by writing — idempotent for identical content via the `unchanged` short-circuit). When the existing row wins, the result is `status='conflict'` and `server_record` carries the authoritative state so the client overwrites its local copy. This makes sync **convergent**: all devices end at the same record for a given `(collection, client_entity_id)`. Note this entire rule runs on **cleartext metadata + an opaque fingerprint** — never plaintext — which is why a zero-knowledge server can still resolve conflicts.

**Idempotency:** because `client_entity_id` is the natural key and `content_fingerprint` short-circuits no-ops, re-sending the same batch (e.g. after a flaky network) is safe and burns no sequence numbers.

### 5.3 Background tasks

- **Tombstone GC (optional, low priority):** a periodic job hard-deletes tombstones older than `TOMBSTONE_RETENTION_DAYS` (default **180**). Retention must exceed the longest plausible "device offline" window so a returning device still learns of deletes. Until GC ships, tombstones simply accumulate (cheap — they carry no payload). Model the job on the existing app-data GC pattern (`agent_app_data`).
- No other background work — sync is request-driven.

---

## 6. Client Implementation (contract — implemented in `cinna-desktop`, out of scope here)

> The server is built to fit this client behaviour; the desktop/mobile code is **not** part of this plan. Documented so the API shape is justified.

**Collection ↔ desktop entity mapping**

| Collection | Desktop source | Payload contents (illustrative) | MVP? |
|------------|----------------|----------------------------------|------|
| `note` | `notes` table | `{title, body, folderId, position, deletedAt}` | ✅ Phase 1 |
| `note_folder` | `note_folders` table | `{name, collapsed, position}` | ✅ Phase 1 |
| `job` | `jobs` (+ `job_agents`, `job_mcp_providers` denormalised in) | `{title, description, prompt, type, priority, color, icon, folderId, position, cinnaAgentId, agentRefs[], mcpRefs[], modeRef, deletedAt}` | ✅ Phase 1 |
| `job_folder` | `job_folders` table | `{name, collapsed, position}` | ✅ Phase 1 |
| `job_run` | `job_runs` table | `{jobEntityId, type, status, cinnaTaskId, cinnaShortCode, localChatRef, error, startedAt, finishedAt}` | ⚠️ Phase 2 (cinna_task runs are portable; local runs depend on chat sync) |
| `chat` | `chats` table | `{title, kind, externalSessionId?, modeRef?, …, deletedAt}` — **pointer**, not message bodies | ⚠️ Phase 2 |
| `chat_message` | chat messages | full message content (E2E ciphertext) | ⛔ Phase 3 (raw-LLM chats only; heaviest by size) |

**Sync engine (desktop):** keeps a `sync_cursor` per Cinna-linked profile; on local mutation, marks the row dirty; on a debounce/interval (and on login), calls `POST /api/v1/app-sync` with dirty rows as `changes` and the stored `cursor`; applies the returned `changes` to local tables (upsert by `client_entity_id`, delete on tombstone); overwrites local rows for any `conflict` result; advances `cursor`. On fresh login it loops `/pull` from `cursor=0` until `has_more==false` to hydrate the empty profile.

**Dangling references are the client's problem (by design).** Jobs reference device-local agent/MCP/chat-mode ids (Default-Scope resources that do **not** sync — see the desktop "Settings Scope" feature). On a new device those ids won't resolve. The desktop already **drops stale references at run time** (`executeLocal` filters missing agents/MCPs) and surfaces `missing_dependency` on a hard run. The server stores the references verbatim; the client tolerates non-resolving ones. This is documented as an explicit non-goal of the server: **the server never validates cross-references inside payloads.**

---

## 7. The chats & job-runs question — and the privacy boundary

Chats and job runs are where the §4.3 privacy boundary becomes concrete. They split cleanly along "private-on-device" vs "deliberately-shared-with-an-external-service":

### 7.1 Agent (A2A / remote-agent) chats — content is *necessarily* server-side (the accepted exception)

When a desktop chat talks to a Cinna agent, the message the user sends **must** be readable by that agent to be answered — so it is, by the user's own deliberate act, shared with the agent's owner. That conversation **already persists as a cinna-core `Session`** (via the `external_agent_access` surface) and is restorable through `GET /api/v1/external/sessions` + `/sessions/{id}/messages`. This is the **one accepted exception** to "everything is private" (§4.3).

Therefore the sync store **does not** re-store that content (it's already server-side, and re-encrypting it E2E wouldn't restore privacy for turns the agent already read):

- Sync only a lightweight, **E2E-encrypted `chat` pointer** carrying `externalSessionId` (+ title, ordering, folder, soft-delete). The pointer is private (the *fact* that you have a thread, its title, its organisation) even though the agent turns themselves live in the Session subsystem.
- On a new device, the client lists `/external/sessions`, matches by `externalSessionId`, and rehydrates the agent message history from the existing endpoint.
- **Orchestration context stays private:** in an orchestrated multi-agent chat, only the specific tool-call payload routed to each agent is visible to that agent; the local conductor's reasoning, other turns, and attached notes are private context and, if synced, ride the E2E store.

This reuses an entire subsystem, keeps the sync store small, and **doesn't double-expose** anything. **Phase 2.**

### 7.2 Cinna Task jobs & runs — already on the server

`cinna_task` jobs run against cinna-core `POST /api/v1/tasks/`; the run record references a server-side task by `cinnaShortCode`, which resolves on any device. Syncing the `job` and `job_run` documents is fully portable with no extra duplication. **Phase 2** (bundled with job-run sync).

### 7.3 Local job runs — device-bound

A local run references a device-local spawned `chatId`. It is only meaningful if that chat is itself synced. Tie local-run sync to chat sync; until then, local run history is a Phase-2/3 best-effort (sync the run document, accept that `localChatRef` may dangle on another device, exactly like job→agent refs).

### 7.4 Raw-LLM chats — fully private, E2E end to end

A chat with a local LLM provider (the user's own key, no Cinna agent) lives **only** on the device; cinna-core is never in the loop. The only external party that sees these messages is the **LLM provider the device calls directly** (e.g. Anthropic) — that's the user's deliberate act, outside cinna-core's boundary. When synced, the full message bodies go into the E2E store as `chat_message` ciphertext, so the sync server stores them but **cannot read them** — exactly the privacy promise. Heaviest collection (size); **Phase 3**. The generic store already accommodates it (just more `collection`s); no schema change needed — the whole point of the opaque, E2E design.

**Net recommendation:** MVP = notes + jobs + folders (the explicit primary ask, fully self-contained, portable, and E2E from day one). Chats/runs follow in phases: agent chats via the encrypted pointer (content stays in the Session subsystem — the accepted exception), raw-LLM chats as fully-private E2E `chat_message` content.

---

## 8. Database Migrations

**File:** `backend/app/alembic/versions/<rev>_add_app_sync_tables.py`

Because E2E is the only mode, the key-management tables are **core** and ship in the same migration (not a later add-on).

**Upgrade**

1. Create `app_sync_state` (PK `user_id` FK → `user.id` `ON DELETE CASCADE`; `current_seq` BIGINT NOT NULL default 0; `total_records`/`total_bytes`; `active_umk_version` INT NOT NULL default 0; `e2e_initialized_at` nullable; `updated_at`).
2. Create `app_sync_record` with all columns from §3.1 (incl. `payload_ciphertext`, `enc_umk_version`, `content_fingerprint`).
3. Create `app_sync_device` (§12.4) — device public keys.
4. Create `app_sync_key_envelope` (§12.4) — wrapped UMK copies; FK `device_id` → `app_sync_device.id` CASCADE.
5. Indexes:
   - `UNIQUE ix_app_sync_record_natural (user_id, collection, client_entity_id)` — upsert key.
   - `ix_app_sync_record_user_seq (user_id, seq)` — pull hot path.
   - `ix_app_sync_record_user_collection (user_id, collection)` — counts / scoped pulls.
   - `ix_app_sync_record_user_id`, `ix_app_sync_device_user_id`, `ix_app_sync_key_envelope_user_id` — FK indexes.
   - `UNIQUE (user_id, wrap_method, umk_version, device_id)` on `app_sync_key_envelope`.
6. All FKs use `ondelete="CASCADE"` so account deletion removes all sync data **and** all key envelopes (privacy).

**Downgrade:** drop `app_sync_key_envelope`, `app_sync_device`, `app_sync_record`, `app_sync_state` (reverse dependency order). No data migration on upgrade (new tables only).

> Heads-up (from project memory): the repo has previously carried **multiple Alembic heads**. Check `alembic heads` before generating; set `down_revision` to the current single head, and if there are multiple, coordinate a merge rather than branching further.

---

## 9. Error Handling & Edge Cases

| Scenario | Server behaviour |
|----------|------------------|
| **Payload > 1 MiB** | `413 PayloadTooLargeError` with the offending `client_entity_id`; batch rejected, no writes. |
| **Batch > 500 records** | `422 BatchTooLargeError`; client splits and retries. |
| **Quota exceeded** | `413 QuotaExceededError` with `{total_bytes, quota_bytes, total_records, quota_records}`; client surfaces "sync storage full," can wipe or prune. |
| **Two devices create *different* entities offline** | Each mints its own UUID `client_entity_id` (§3.5), so they land as two distinct rows — no collision, both survive. This is the core reason identity is a UUID, not a local rowid. |
| **Two devices edit the *same* entity (same UUID)** | Serialised by the `app_sync_state` row lock; both get gap-free `seq`s; LWW decides the final value; the losing device receives `status='conflict'` + authoritative `server_record` and overwrites locally → convergence. |
| **Client pushes a malformed or bare-integer `client_entity_id`** (e.g. a leaked local rowid) | Rejected `422` (§4.3) before any write — the footgun-blocker for cross-device collisions. |
| **Re-push after network blip** | client `content_fingerprint` short-circuits to `status='unchanged'`; no duplicate, no seq burn → idempotent. |
| **Push before E2E initialised** (`active_umk_version == 0`) | `409 E2ENotInitializedError`; client must run `POST /encryption/init` (first device) or unlock via pairing/recovery first. |
| **Malformed ciphertext / missing fingerprint** | `422 InvalidPayloadError`; the server checks structure only, never decryptability. |
| **Clock skew (future `client_updated_at`)** | Clamped to server time + 24 h ceiling so a wrong clock can't permanently "win" LWW. |
| **Delete then re-create same `client_entity_id`** | Tombstone → live (undelete) with a new `seq`; peers pull the revival. |
| **Fresh login on empty device** | `cursor=0` pull loop until `has_more==false` hydrates everything (including tombstones, which the client may skip). |
| **Pull a tombstone for an entity the client never had** | Client ignores it (delete of nothing). |
| **Account deleted** | CASCADE wipes `app_sync_record` + `app_sync_state`. |
| **Desktop device revoked mid-sync** | Next call rejected `401 Desktop session has been revoked` (existing `get_current_user` check). |
| **Unknown collection name (valid format)** | Accepted and stored (forward compatibility); never rejected for being unrecognised. |
| **Payload not a JSON object** | `422 InvalidPayloadError`. |
| **`base_seq` provided and stale** (optional optimistic concurrency) | Treated as a hint only; LWW by `client_updated_at` remains authoritative. Documented so clients don't rely on rejection. |

---

## 10. UI/UX Considerations

- **No cinna-core SPA UI** for notes/jobs/chats content (explicit scope exclusion) — and by construction the SPA *couldn't* show it anyway, since the server can't decrypt it.
- **Optional, recommended:** a small **"Cloud Sync" / privacy** affordance in **Settings → Security** (next to Desktop Sessions), showing synced storage usage (`GET /app-sync/state`), a **trusted-devices** list (`GET/DELETE /app-sync/devices`), and a **"Delete synced data"** button (`DELETE /api/v1/app-sync`). A privacy control, not a data browser.
- **The E2E experience is overwhelmingly client-side.** Device pairing (QR scan), recovery-key display/export, passphrase entry, and unlock all live in the native clients (§12.6); cinna-core only stores/relays wrapped envelopes and ciphertext. The SPA's role is limited to the device list and the wipe control above.
- All user-facing sync feedback (progress, conflicts, "storage full", "enter your recovery key") lives in the **desktop/mobile** client, out of scope here.

---

## 11. Integration Points (summary)

- **Auth:** `CurrentUser` / Desktop OAuth tokens — no new auth surface; live-revocation check inherited.
- **External Agent Access:** agent-chat content reuse via `external_session_id` pointer (no message duplication).
- **Tasks:** `cinna_task` runs portable by `short_code`.
- **Security:** mandatory client-side **E2E** — the server holds only undecryptable ciphertext and wrapped keys it cannot open (§12). No server-side payload crypto, no `backend/app/core/security.py` Fernet on payloads.
- **Desktop Auth devices:** `app_sync_device` rows may link to `DesktopOAuthClient` via `external_client_id`; revoking a desktop session (Settings → Security) should also prompt a key-rotation so the revoked device's `device` envelope can no longer unlock future data (§12.7).
- **Client regen:** run `bash scripts/generate-client.sh` after adding the routes/models so the OpenAPI client stays valid. **Note:** the web SPA does not consume these endpoints (no UI); regen is for spec hygiene and any optional Settings → Security control. The desktop/mobile clients use their own HTTP layer (`cinnaApiService`), not the generated web client.
- **Nginx:** `/api/v1/app-sync*` is under `/api/` — already proxied; no new location block needed (unlike the origin-root `.well-known` desktop-auth case).

---

## 12. End-to-End Encryption & Key Management

> **E2E is the only mode.** The server stores ciphertext it **cannot** decrypt and wrapped keys it **cannot** open. Content (note bodies, job prompts, chat context) is readable only on the user's own devices. There is no server-readable fallback and no server-held key — even a fully compromised server yields nothing. This section specifies the key hierarchy, the **easy cross-device key sharing** (QR pairing + recovery key), rotation, and the threat model.

### 12.1 What the server can and cannot see

| Aspect | Server |
|--------|--------|
| `payload_ciphertext` | stores & returns **verbatim**; cannot decrypt (no key) |
| Wrapped UMK envelopes | stores & returns **verbatim**; cannot unwrap (no key) |
| `seq` cursor, delta pull | sees & uses (ordering metadata) |
| LWW conflict resolution | runs on `client_updated_at` + `seq` — **cleartext metadata, no plaintext needed** |
| No-op short-circuit | compares client-supplied `content_fingerprint` for equality only |
| Quota | counts **ciphertext** bytes |
| Plaintext, UMK, passphrase, recovery key, device private keys | **never** — none of these ever reach the server |

LWW was chosen over content-merge precisely because it resolves conflicts using **only metadata the server can see** — which is what lets a zero-knowledge server work at all. A CRDT/field-merge model (§13) would need plaintext server-side and is therefore a *client-side-only* future option.

### 12.2 Key hierarchy

A two-level scheme keeps re-keying cheap and multi-device onboarding easy.

```
   QR device pairing (sealed transfer) ─┐
 device priv key (OS keychain) ─unseal──┤
   recovery key  ──HKDF──► KEK_rec ──────┤ unwrap
   passphrase (optional) ─Argon2id─► KEK_pw┘
                                          ▼
                       ┌──────────────────────────────────┐
                       │  UMK  (User Master Key, 256-bit,  │  never leaves a device
                       │        random, generated once)    │  in plaintext, ever
                       └──────────────────────────────────┘
                                          │ HKDF(UMK, info=collection)
                                          ▼
                       per-collection subkey ──► AEAD(payload)
```

- **UMK (User Master Key)** — one random 256-bit key per account, the root that (via HKDF per-collection subkeys) encrypts every payload. Generated **once on the first device**; never transmitted or stored in plaintext.
- **Unlock methods** (each a wrapped copy of the UMK on the server — see `app_sync_key_envelope`):
  - **`device`** — UMK sealed to a device's X25519 public key (`crypto_box_seal`). The device unlocks silently from its OS-keychain private key (`safeStorage` on desktop, Keychain/Keystore on mobile). This is the steady-state, zero-friction unlock.
  - **`recovery`** — UMK wrapped under `KEK_rec` derived from a high-entropy **recovery key** (§12.6). The offline backup and the only way back if every device is lost. Because the recovery key is high-entropy, derive `KEK_rec` with HKDF (no slow Argon2id needed).
  - **`passphrase`** (optional) — UMK wrapped under `KEK_pw = Argon2id(passphrase, salt)`, for users who prefer typing a memorized secret. Argon2id because human passphrases are low-entropy. Works for Google-OAuth users too (no login password required).
- **Payload encryption** — per write: `subkey = HKDF(UMK, info=collection)`; `ct = XChaCha20-Poly1305(subkey, nonce=random192, plaintext, AAD)`, with `AAD = user_id ‖ collection ‖ client_entity_id ‖ umk_version`. The AAD **binds ciphertext to its identity**, so a malicious server can't swap blobs between records/users without the decrypt failing.

Primitives: libsodium (Electron/Node + iOS/Android) — `crypto_aead_xchacha20poly1305_ietf`, `crypto_box_seal` (X25519 sealed boxes), `crypto_pwhash` (Argon2id), `crypto_kdf`/HKDF. The server needs **no** crypto library; it only stores and relays blobs.

### 12.3 Ciphertext envelope format (client-defined, server-opaque)

```jsonc
{ "v": 1, "alg": "xchacha20poly1305", "umk": 2, "n": "<b64 nonce>", "ct": "<b64 ciphertext+tag>" }
```

`umk` is the UMK generation the record was encrypted under (rotation, §12.7). The server stores this verbatim and never parses it (beyond size accounting). The same envelope shape (different `info`/AAD) wraps the UMK copies in `app_sync_key_envelope.wrapped_key`.

### 12.4 Data models (core — see also §3)

The E2E columns are part of the **core** schema, not add-ons: `app_sync_record.payload_ciphertext`, `.enc_umk_version`, `.content_fingerprint` (§3.1) and `app_sync_state.active_umk_version`, `.e2e_initialized_at` (§3.2). Two more tables hold the key material the server blindly stores/relays:

**`app_sync_device`** — registered device public keys (a desktop device may also be a `DesktopOAuthClient`; link via `external_client_id`; mobile gets its own row):

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | |
| `user_id` | UUID | FK → `user.id` CASCADE, indexed | |
| `device_label` | VARCHAR(128) | NOT NULL | "Evgeny's MacBook", "Pixel 8" |
| `public_key` | TEXT | NOT NULL | X25519 public key (b64); private key never leaves the device |
| `external_client_id` | UUID | nullable | links to the `DesktopOAuthClient` device when applicable |
| `is_revoked` | BOOLEAN | NOT NULL default false | |
| `created_at` / `last_seen_at` | TIMESTAMP | | |

**`app_sync_key_envelope`** — wrapped copies of the UMK (one per unlock method × umk_version):

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | |
| `user_id` | UUID | FK → `user.id` CASCADE, indexed | |
| `wrap_method` | VARCHAR(16) | NOT NULL | `device` \| `recovery` \| `passphrase` |
| `umk_version` | INTEGER | NOT NULL | which UMK generation this envelope wraps |
| `wrapped_key` | TEXT | NOT NULL | the UMK ciphertext — **opaque to server** |
| `kdf` | VARCHAR(32) | nullable | `hkdf` (recovery) / `argon2id` (passphrase) |
| `kdf_params` | JSON | nullable | `{salt, mem, ops, parallelism}` as applicable |
| `device_id` | UUID | FK → `app_sync_device.id` CASCADE, nullable | set for `device` wraps |
| `created_at` | TIMESTAMP | NOT NULL | |

Constraint: `UNIQUE (user_id, wrap_method, umk_version, device_id)`.

**`app_sync_pairing`** — short-lived blind relay for QR device pairing (§12.6):

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | |
| `user_id` | UUID | FK → `user.id` CASCADE, indexed | both devices are the same account |
| `pairing_code_hash` | VARCHAR(64) | NOT NULL, indexed | SHA-256 of the pairing code; raw code only in the QR/screen |
| `new_device_pubkey` | TEXT | NOT NULL | the joining device's **ephemeral** X25519 public key |
| `sealed_umk` | TEXT | nullable | UMK sealed to `new_device_pubkey` by the existing device; `NULL` until completed |
| `status` | VARCHAR(16) | NOT NULL default `pending` | `pending` → `completed` → consumed; or `expired` |
| `expires_at` | TIMESTAMP | NOT NULL | short TTL (e.g. 5 min) |
| `created_at` | TIMESTAMP | NOT NULL | |

The server only relays `sealed_umk` (ciphertext sealed to a key whose private half lives only on the joining device). It never sees the UMK.
### 12.5 Key-management & pairing endpoints

Under `/api/v1/app-sync`, all `CurrentUser`-scoped. The server validates structure only (well-formed b64, size caps, known enums); it **cannot and must not** verify that any wrapped blob actually contains the UMK.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/encryption` | `{initialized, active_umk_version, has_recovery, has_passphrase, devices:[…]}` — tells a client how it can unlock |
| `POST` | `/encryption/init` | **First device only.** Body: initial `device` + `recovery` envelopes (+ optional `passphrase`) and the device registration. Sets `active_umk_version=1`, `e2e_initialized_at`. `409` if already initialised |
| `GET` | `/keys?umk_version=` | List wrapped envelopes for a UMK generation |
| `POST` | `/keys` | Add/replace an envelope (new device wrap, re-wrapped recovery/passphrase) |
| `DELETE` | `/keys/{id}` | Remove an envelope |
| `POST` | `/devices` | Register a device public key → `device_id` |
| `GET` | `/devices` | List devices (trusted-devices UI) |
| `DELETE` | `/devices/{id}` | Revoke a device: delete its `device` envelope(s), mark revoked → prompt rotation (§12.7) |
| `POST` | `/pairing/start` | Joining device: body `{new_device_pubkey, device_label}` → `{pairing_code, expires_at}` |
| `GET` | `/pairing/{code}` | Joining device polls → `{new_device_pubkey, status, sealed_umk?}` |
| `POST` | `/pairing/{code}/complete` | Existing unlocked device posts `{sealed_umk}` (UMK sealed to `new_device_pubkey`); marks `completed` |

### 12.6 Key sharing across devices (the easy paths)

The design goal is: **a second device should join with a single QR scan and no typed secret**, and every user should hold a **portable recovery key** as the safety net. Three mechanisms, in priority order:

#### (a) QR device pairing — primary, zero typing (blind server relay)

This is the Magic-Wormhole / passkey-style pattern: the QR is an *authenticated out-of-band channel* for a public key, and the server is a blind relay for the sealed secret.

```
 New device N (signed in, locked)            Existing device E (signed in, UNLOCKED → has UMK)
 ───────────────────────────────             ────────────────────────────────────────────────
 1. generate ephemeral (epk_N, esk_N)
 2. POST /pairing/start {epk_N,label}
        → {pairing_code}
 3. render QR = {pairing_code, epk_N}  ─────► 4. SCAN QR  (camera) → reads pairing_code + epk_N
                                              5. seal: blob = crypto_box_seal(epk_N, UMK)
                                              6. POST /pairing/{code}/complete {sealed_umk: blob}
 7. poll GET /pairing/{code}
        → {sealed_umk}
 8. UMK = crypto_box_seal_open(blob, esk_N)
 9. POST /devices + POST /keys  (add N's own
    long-term `device` envelope → silent next time)
```

- The **QR carries only public data** (a one-time code + an ephemeral public key). The secret (UMK) travels as `sealed_umk`, sealed to `epk_N`, which only `esk_N` on device N can open. The server relays ciphertext and learns nothing.
- **Direction:** the *joining* device shows the QR; the *existing unlocked* device scans it. Pick whichever device has a camera as the scanner; in practice the phone scans the desktop, or the desktop's webcam scans the phone.
- **Camera-less fallback (two desktops):** instead of a QR, device N shows a short human **pairing code**; the user types it into E, which fetches `epk_N` from `GET /pairing/{code}`. Because `epk_N` now comes via the server (not an authenticated channel), both devices display a **short authentication string** (e.g. 5 words / 6 digits derived from `hash(epk_N ‖ pairing_code)`) that the user compares to defend against a server-substituted key. (With the QR path this compare is unnecessary — the QR already authenticated `epk_N`.)
- TTL ~5 min, single-use; the row is consumed on success.

#### (b) Recovery key — mandatory offline backup

Generated at first setup; the only way back if **all** devices are lost. Best-practice presentation — the *same* high-entropy secret in three interchangeable forms:

- **Mnemonic** — a BIP39-style word list (e.g. 24 words with checksum). Easy to write on paper, transcribe, and error-detect. This is the canonical recoverable form (wallets, Proton, Apple, etc.).
- **Downloadable file** — `cinna-sync-recovery.txt` containing the mnemonic, the account email, dated instructions, and an embedded QR. The user stores it in a password manager / cloud drive / safe.
- **QR image** — to screenshot or print.

The client **forces the user to save it at setup** (download the file *or* confirm the words were written) before E2E activation completes — losing it after a total device loss means permanent, unrecoverable data loss, and that must be an explicit, acknowledged choice. Recovery flow: enter mnemonic / import file → `HKDF` → `KEK_rec` → `GET /keys` → unwrap the `recovery` envelope → UMK → then enrol this device (add a `device` envelope).

#### (c) Passphrase — optional convenience

For users who would rather memorize a phrase than scan or keep a file: `KEK_pw = Argon2id(passphrase, salt)` wraps the UMK. Offered, not required. Weak passphrases are the weakest link, so the UI nudges toward the recovery key as the real entropy anchor and applies strong Argon2id parameters.

#### Setup & unlock summary

- **First device (init):** generate UMK → create `device` + `recovery` (+ optional `passphrase`) envelopes → `POST /encryption/init` → force recovery-key backup → start pushing ciphertext.
- **New device, easy path:** QR-pair with an existing unlocked device (a) → silent thereafter.
- **New device, no other device available:** unlock with the recovery key (b) or passphrase (c) → enrol a `device` envelope.

> **UX truth to surface plainly:** with E2E, "log in again and data syncs back" requires **a previously-trusted device, the recovery key, or the passphrase** — the account password alone is not enough. This is inherent to zero-knowledge and the onboarding copy must say so.

### 12.7 UMK rotation (after device revocation or periodically)

The client generates UMK v(n+1), re-wraps it for all surviving unlock methods (`POST /keys` at v(n+1)), bumps `active_umk_version`, then re-encrypts records — lazily on next edit and/or via a background sweep that re-pushes rows with `enc_umk_version = n+1`. The server tolerates mixed `enc_umk_version` during the sweep (each record self-describes its generation). Rotation protects **future** confidentiality only — a revoked device already saw the old data. Revoking a desktop session (Settings → Security) should prompt this rotation so the revoked device's old `device` envelope is worthless going forward. (There is no "enable E2E on existing plaintext" migration — E2E is set up at first sync, so the store is never plaintext.)

### 12.8 Threat model

**Protects against:** server DB compromise, stolen backups/snapshots, a curious or malicious operator, and compelled disclosure — all yield only undecryptable ciphertext and unopenable wrapped keys.

**Does NOT hide (metadata the server still sees):** `collection` names, record counts, ciphertext **sizes**, `client_updated_at`/`seq` timestamps and edit cadence, the device list, and `client_entity_id` UUIDs. Stated honestly. (Sizes can later be bucket-padded; collection names could be hashed at the cost of server-side per-collection pulls.)

**The deliberate-sharing boundary (§4.3):** content the user sends to an **A2A agent** is, by design, visible to that agent's owner — it lives in the Session subsystem, not the E2E store. That is the user's explicit act of contacting an external party, not a leak. Everything else stays E2E.

**Tampering & rollback:** the AEAD + identity-binding AAD makes forged/swapped ciphertext fail to decrypt. A malicious server can still withhold or replay (roll back) records; monotonic `seq` lets a client spot regressions, and a future device-co-signed "max seq seen" could detect rollback. Out of scope for the first cut.

**Out of scope:** device/endpoint compromise (the OS keychain guards the device private key; a compromised device is game over, as in any E2E system) and weak passphrases (mitigated by Argon2id + steering users to the recovery key).

### 12.9 Recommendation

E2E is **mandatory and set up at first sync** — there is no server-readable mode to fall back to. Lead the onboarding with **QR device pairing** (one scan, no typing) and **mandate recovery-key backup** (mnemonic + downloadable file) before activation completes; offer a **passphrase** only as an optional convenience. Keep the model **account-wide** (one UMK, all collections) for simplicity, and make the "lose your keys = lose your data" trade explicit and acknowledged at setup — it is the unavoidable price of "no one but you can ever read it."

---

## 13. Future Enhancements (Out of Scope)
- **Field-level / CRDT merge:** replace whole-document LWW with per-field merge (e.g. note title and body edited on two devices both survive). Use a CRDT (e.g. Yjs/Automerge document blobs) per entity. The store stays opaque; only the client merge logic changes.
- **Full raw-LLM chat + message sync** (`chat_message` collection) — Phase 3; fully E2E like everything else (no schema change).
- **Server-side change notifications:** push a WebSocket/`realtime_events` "sync invalidate" so other online devices pull immediately instead of on interval.
- **Selective/partial sync & per-collection toggles:** let users choose which collections sync.
- **Compression** of large payloads before encryption.
- **Tombstone GC** scheduling (listed as optional in §5.3; promote to standard once retention policy is confirmed).
- **Admin/observability:** per-user sync storage metrics in the admin console.

---

## 14. Summary Checklist

### Backend (Phase 1 — MVP: notes + jobs + folders, E2E from day one)

- [ ] Create `backend/app/models/app_sync/` — `app_sync_record.py` (incl. `payload_ciphertext`, `enc_umk_version`, `content_fingerprint`), `app_sync_state.py` (incl. `active_umk_version`, `e2e_initialized_at`), `app_sync_device.py`, `app_sync_key_envelope.py`, `app_sync_pairing.py` + Pydantic schemas (§3.4); re-export from `models/__init__.py`.
- [ ] Implement `backend/app/services/app_sync/app_sync_service.py` — `sync`, `push`, `pull`, `get_state`, `wipe`, `_allocate_seq` (row-lock). **No `_encrypt`/`_decrypt`** — store/return `payload_ciphertext` verbatim. LWW + `content_fingerprint` no-op short-circuit; quota on ciphertext bytes; `E2ENotInitializedError` gate; domain exception hierarchy (§5.2).
- [ ] Implement key-management & pairing in the service: `init_encryption`, envelope CRUD, device register/list/revoke, `pairing_start`/`pairing_get`/`pairing_complete` — all relay opaque blobs only.
- [ ] Add `backend/app/api/routes/app_sync.py` — sync verbs (`POST /`, `/pull`, `/push`, `GET /state`, `DELETE /`) **plus** `GET /encryption`, `POST /encryption/init`, `GET|POST /keys`, `DELETE /keys/{id}`, `GET|POST /devices`, `DELETE /devices/{id}`, `POST /pairing/start`, `GET /pairing/{code}`, `POST /pairing/{code}/complete` (§5.1, §12.5) — `CurrentUser`, `_handle_service_error()`; read `external_client_id` from JWT for `last_writer_client_id` + device linking.
- [ ] Register the router in `backend/app/api/main.py` (prefix `/api/v1/app-sync`, tag `"App Sync"`).
- [ ] Add config constants to `core/config.py`: `APP_SYNC_MAX_PAYLOAD_BYTES`, `APP_SYNC_MAX_RECORDS_PER_PUSH`, `APP_SYNC_QUOTA_BYTES`, `APP_SYNC_QUOTA_RECORDS`, `APP_SYNC_TOMBSTONE_RETENTION_DAYS`, `APP_SYNC_PAIRING_TTL_SECONDS`.
- [ ] Alembic migration `add_app_sync_tables` — `app_sync_state`, `app_sync_record`, `app_sync_device`, `app_sync_key_envelope`, `app_sync_pairing` with all indexes + CASCADE FKs (§8); verify single Alembic head first; write downgrade.
- [ ] Apply migration (`make migrate`) and confirm (`alembic current`).
- [ ] Regenerate OpenAPI client (`bash scripts/generate-client.sh`) for spec hygiene.

### Backend (Phase 2 — chats pointers + cinna_task runs)

- [ ] Accept `job_run` and `chat` collections (no schema change — opaque store); document expected payload shapes for clients.
- [ ] Document the `chat` pointer ↔ `external_session_id` restore handshake against `external_agent_access`.

### Backend (Phase 3 — raw-LLM chat content + hardening)

- [ ] `chat_message` collection support (no schema change).
- [ ] Tombstone GC background job (model on `agent_app_data` GC).

### Client (cinna-desktop / mobile — out of scope here, the contract the server fits)

- [ ] libsodium key hierarchy: UMK generation, HKDF subkeys, XChaCha20-Poly1305 payload AEAD with identity-binding AAD, the §12.3 envelope format.
- [ ] Unlock methods: `device` (X25519 sealed box, private key in OS keychain), `recovery` (HKDF from a BIP39 mnemonic), optional `passphrase` (Argon2id).
- [ ] **QR device pairing** (§12.6a): ephemeral keypair, `/pairing/*` round trip, blind-relay sealed UMK, camera-less code+SAS fallback.
- [ ] **Recovery-key UX** (§12.6b): mnemonic + downloadable file + QR; force-save gate at setup; recovery/import flow.
- [ ] Per-record `content_fingerprint` = `HMAC(HKDF(UMK,"fp"), canonical_plaintext)`; UMK rotation sweep (§12.7).

### Optional UI (cinna-core SPA — privacy control only)

- [ ] Settings → Security: "Cloud Sync" card showing `GET /app-sync/state` usage + a confirm-gated "Delete synced data" (`DELETE /api/v1/app-sync`) button. No content browser.

### Testing & validation (API-only, per `backend/tests/README.md`)

- [ ] Push a record → pull from cursor 0 returns the **byte-identical ciphertext** (server stores/returns verbatim; it never decrypts — assert the stored column equals the pushed ciphertext).
- [ ] **Zero-knowledge:** there is no code path that decrypts a payload; `payload_ciphertext` and `wrapped_key` are opaque end to end.
- [ ] **E2E gate:** push before `POST /encryption/init` → `409`; after init → succeeds.
- [ ] **Key envelopes:** `init` stores `device` + `recovery` (+ optional `passphrase`) envelopes; `GET /keys` returns them verbatim; envelope CRUD; `DELETE` removes.
- [ ] **Device pairing relay:** `POST /pairing/start` → code; existing device `POST /pairing/{code}/complete {sealed_umk}`; joining device `GET /pairing/{code}` retrieves the sealed blob; expiry + single-use enforced; server never sees UMK.
- [ ] **Device revoke:** `DELETE /devices/{id}` removes its `device` envelopes + marks revoked.
- [ ] Delta pull: push A (seq 1), push B (seq 2); pull from cursor 1 returns only B.
- [ ] Tombstone: delete a record → pull surfaces it with `deleted=true` and null payload.
- [ ] LWW: two upserts to the same `(collection, client_entity_id)`; older `client_updated_at` loses with `status='conflict'` and authoritative `server_record`.
- [ ] Idempotent re-push: identical batch twice → second yields `status='unchanged'`, no new seq, count unchanged.
- [ ] **Cross-device identity:** two pushes with *different* `client_entity_id`s (UUID or nanoid) in the same collection both persist as distinct rows (no clobber); a push with a malformed or bare-integer `client_entity_id` → `422`.
- [ ] Ownership isolation: user A cannot see/pull/overwrite user B's records (no leakage; counts scoped).
- [ ] Limits: payload > max → 413; batch > max → 422; quota exceeded → 413 with structured detail.
- [ ] Pagination: push > `limit` records, pull loops with `has_more` until drained, cursor advances monotonically.
- [ ] `GET /state`: correct cursor, quota usage, per-collection counts.
- [ ] `DELETE /`: wipes (optionally per collection), resets counters, advances seq so a peer pull sees the removal.
- [ ] Desktop-token attribution: `last_writer_client_id` populated from `external_client_id`; null for web tokens.
- [ ] Account deletion cascades both tables.
- [ ] Fresh-login bootstrap: empty cursor pull hydrates a full dataset including folder structure.

---

*Plan author note:* Two architectural commitments carry this design. **(1)** The **opaque, collection-partitioned document store with a per-user gap-free sequence cursor and LWW conflict resolution** lets cinna-core be a durable sync substrate for an evolving client schema without ever shipping a migration when desktop/mobile add a field. **(2)** **Mandatory, zero-knowledge end-to-end encryption** — the server stores only ciphertext and unopenable wrapped keys, and LWW deliberately resolves conflicts on cleartext *metadata* so the server needs no plaintext to do its job. Together they deliver the north star: *what you do on your device is yours alone; the only thing anyone else sees is what you deliberately send them* (an A2A agent, your own LLM provider) — never the sync server.
```
