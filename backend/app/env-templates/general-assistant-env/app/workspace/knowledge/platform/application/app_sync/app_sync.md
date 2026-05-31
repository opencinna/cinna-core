# App Sync (Native Client Data Sync)

## Overview

App Sync is the server-side sync substrate that lets authenticated native clients (Cinna Desktop today, Cinna Mobile in future) back up and synchronise their profile-scoped data — notes, jobs, folders, and more — across devices and across log-in/log-out cycles.

The canonical scenario: a user creates jobs and notes in Cinna Desktop, logs out (the local cache is wiped), then logs back in on the same or a different device. App Sync restores the full dataset seamlessly. A second device signed into the same Cinna account sees the same data immediately.

**The design's north star is zero-knowledge privacy.** The server stores only ciphertext it cannot decrypt. There is no server-readable mode, no fallback, and no server-held key. Even a fully compromised server or a compelled disclosure yields only undecryptable blobs.

---

## Core Capabilities

- **Generic opaque document store** — records are partitioned by `collection` but the server never reads, validates, or interprets payload contents. Client schema changes never require a cinna-core migration.
- **Mandatory end-to-end encryption** — all payloads are encrypted on-device before upload; the server stores and returns ciphertext verbatim.
- **Delta sync protocol** — clients carry a cursor (the highest `seq` they have applied locally) and pull only what changed. Push and pull can happen in one round trip or independently.
- **Last-Writer-Wins (LWW) conflict resolution** — based on `client_updated_at` and a client-supplied `content_fingerprint`, resolved entirely on cleartext metadata. No plaintext required.
- **Tombstones** — deletes propagate as flagged rows; peers learn of deletions on their next delta pull.
- **Per-user quotas** and per-record size limits, computed on ciphertext bytes.
- **Frictionless cross-device key sharing** — QR device pairing (blind server relay, no typing required) plus a mandatory recovery key and an optional passphrase as unlock fallbacks.
- **No new auth surface** — reuses `CurrentUser` with Desktop OAuth tokens; the existing Desktop Auth live-revocation check applies automatically.

---

## Privacy Boundary — Private-on-Device vs Deliberately Shared

The product promise is: what you do on your private device is yours alone. The single explicit exception is data you deliberately hand to an external service, because that service must read it to respond.

| Data | Visibility | Why |
|------|-----------|-----|
| Notes, job configs, folders, local-LLM chat history, orchestration context, job-run metadata | **Private (E2E)** — only the user's devices | Never leaves the device except as ciphertext the sync server cannot read |
| The specific message(s) a user sends to an A2A agent, and that agent's replies | **Visible to that agent's owner** (and to the platform for hosted agents) | The agent literally cannot answer a prompt it cannot read. This is the user's deliberate act of contacting an external party |
| What a local LLM provider (the user's own Anthropic/OpenAI key) sees at inference | **Visible to that provider** | The device calls the provider directly; cinna-core is not in the loop |

The architecture enforces this boundary:

- **Agent (A2A) conversations already live server-side** as cinna-core `Session` rows — by necessity, because the agent produced and consumed them. The sync store does **not** re-store message bodies. It keeps only an E2E-encrypted pointer (`external_session_id` + local metadata like title and folder organisation). The agent-visible turns stay in the Session subsystem (the accepted exception); the user's private wrapper and context stay E2E. Nothing private is doubly-exposed.
- **Orchestrated multi-agent chats** — only the actual tool-call payload routed to a given agent is visible to that agent. The local conductor's reasoning, other turns, and attached notes are private context and, if synced, ride the E2E store.

In short: the sync server sees nothing; external agents see only what is sent to them; LLM providers see only what the device sends them. There is no fourth party.

---

## Delta Sync Protocol

### Cursor

Each client persists the highest server `seq` it has applied locally (the `sync_cursor`). A fresh login starts with `cursor = 0`.

### Pull (download)

The client requests all records with `seq > cursor`, ordered by `seq` ascending, paginated. It applies each to its local store and advances `cursor` to `next_cursor`. It loops until `has_more == false`.

### Push (upload)

The client sends its local mutations (upserts and deletes) tagged with `client_updated_at`. The server allocates new `seq` values, applies LWW, and returns the authoritative record for any conflict so the client can overwrite its local copy.

The `next_cursor` in a `/push`-only response is informational (the post-push global max seq) and is NOT a safe pull cursor. Concurrent writes from other devices may have filled gaps that the push caller does not yet know about. To advance the real sync cursor, clients must pull via `POST /` or `POST /pull`.

### Combined (primary)

`POST /api/v1/app-sync` does push-then-pull in one round trip, which is what an offline-first client needs: "here are my changes, give me yours."

### Push batch atomicity

All changes in one push request are applied in a single transaction under a `SELECT ... FOR UPDATE` row lock on the user's `app_sync_state` row. A validation failure on any record aborts the entire batch with no writes (the client fixes and retries). Per-record `conflict` and `unchanged` outcomes are normal results inside a successful batch, not errors.

### Idempotency

Because `client_entity_id` is the natural key and `content_fingerprint` short-circuits no-ops, re-sending the same batch after a network blip is safe and burns no sequence numbers.

---

## LWW Conflict Resolution

When two devices edit the same entity (same `collection` + `client_entity_id`), the server resolves the conflict using cleartext metadata only — no plaintext required.

**Incoming wins** if:
- `incoming.client_updated_at > existing.client_updated_at`, OR
- the timestamps are equal and `incoming.content_fingerprint != existing.content_fingerprint` (tie-break by writing — idempotent for identical content via the `unchanged` short-circuit).

When the existing row wins, the result is `status='conflict'` and `server_record` carries the authoritative state so the client can overwrite its local copy. All devices converge to the same record for a given `(collection, client_entity_id)`.

**Clock skew protection** — `client_updated_at` values more than 24 hours in the future are clamped to server time so a misconfigured client clock cannot permanently "win" LWW.

---

## Entity Identity Across Devices

The sync identity of an entity is `client_entity_id`, which is a **client-generated globally-unique UUID (v4)**, minted once at creation on whichever device and carried forever. It must never be a device-local autoincrement integer or rowid — if it were, two devices would independently create a note with local id `5`, both push `client_entity_id = "5"`, and LWW would silently destroy one device's data.

The server validates that `client_entity_id` is a well-formed UUID string and rejects anything else with `422`. This is a deliberate footgun-blocker.

All cross-references between synced entities inside a payload (e.g. a note's `folderId`) must also use these UUIDs, not local foreign keys, so parent-child links are valid on every device.

---

## Collection Phasing

The opaque store accepts any validly formatted collection name. The planned phases are:

| Collection | Payload represents | Phase |
|------------|--------------------|-------|
| `note` | Note body, title, folder reference | Phase 1 (MVP) |
| `note_folder` | Folder name and position | Phase 1 (MVP) |
| `job` | Job config: title, prompt, type, priority, agent references | Phase 1 (MVP) |
| `job_folder` | Folder name and position | Phase 1 (MVP) |
| `job_run` | Job execution record; requires chat sync for local runs | Phase 2 |
| `chat` | E2E-encrypted pointer: `external_session_id` + metadata; message bodies stay in Session subsystem | Phase 2 |
| `chat_message` | Full raw-LLM chat message bodies (fully E2E; heaviest by size) | Phase 3 |

Phase 2 and Phase 3 collections require no schema change — the opaque store already accommodates them. This is the core benefit of the document-store design.

---

## End-to-End Encryption & Key Management

### The Zero-Knowledge Invariant

The server holds no key that can open a payload. It performs no cryptography on payloads — ciphertext is stored verbatim and returned verbatim. LWW resolves conflicts on cleartext metadata, and the no-op short-circuit compares a client-supplied keyed fingerprint for equality only. These two design choices together are what make a zero-knowledge server practical.

**What this means for recovery:** if a user loses every device and both the recovery key and the passphrase, the data is unrecoverable by anyone, including the platform. This is intrinsic to the guarantee and must be surfaced explicitly at setup.

### Key Hierarchy

A two-level scheme keeps re-keying cheap and multi-device onboarding easy.

```
 device priv key (OS keychain) ──────────────────────────────► unseal
 recovery key ──HKDF──► KEK_rec ──────────────────────────────► unwrap    } UMK
 passphrase (optional) ──Argon2id──► KEK_pw ──────────────────► unwrap
                              ▼
               UMK (User Master Key, 256-bit, random, per-account)
                              │ HKDF(UMK, info=collection)
                              ▼
               per-collection subkey ──► AEAD(payload)
```

- **UMK (User Master Key)** — one random 256-bit key per account, the root that (via HKDF per-collection subkeys) encrypts every payload. Generated once on the first device; never transmitted or stored in plaintext.
- **`device` envelope** — UMK sealed to the device's X25519 public key. The device unlocks silently from its OS keychain private key. This is the steady-state, zero-friction unlock.
- **`recovery` envelope** (mandatory) — UMK wrapped under `KEK_rec` derived from a high-entropy recovery key (see below). The offline backup and the only way back if every device is lost.
- **`passphrase` envelope** (optional) — UMK wrapped under `KEK_pw = Argon2id(passphrase, salt)`, for users who prefer typing a memorised secret.

Payload encryption per write: `subkey = HKDF(UMK, info=collection)`; `ct = XChaCha20-Poly1305(subkey, nonce=random192, plaintext, AAD)`, with `AAD = user_id ‖ collection ‖ client_entity_id ‖ umk_version`. The AAD binds the ciphertext to its identity, so a malicious server cannot swap blobs between records or users without the decrypt failing.

### E2E Gate

Until an account's E2E is initialised (`app_sync_state.active_umk_version == 0`), push is rejected with `409`. A device must run `POST /api/v1/app-sync/encryption/init` (first device only) or unlock via pairing or recovery before it can write.

### Cross-Device Key Sharing

#### QR Device Pairing (primary — zero typing)

The joining device generates an ephemeral X25519 keypair and calls `POST /pairing/start` to create a short-lived relay row; the server returns a one-time pairing code. The joining device renders a QR carrying `{pairing_code, ephemeral_public_key}`. An existing unlocked device scans the QR, seals the UMK to the ephemeral public key (`crypto_box_seal`), and calls `POST /pairing/{code}/complete`. The joining device polls `GET /pairing/{code}`, receives the sealed blob, and opens it with its ephemeral private key. The server relays ciphertext sealed to a key whose private half never existed on the server.

The pairing row has a 5-minute TTL and is consumed after the joining device retrieves the `sealed_umk` (single-use delivery).

Camera-less fallback (two desktops): the joining device shows a short human pairing code; the user types it into the existing device. Both devices display a short authentication string (derived from `hash(ephemeral_pubkey ‖ pairing_code)`) for the user to compare, guarding against a server-substituted key.

#### Recovery Key (mandatory offline backup)

Generated at first setup; the only way back if all devices are lost. Presented as a BIP39-style mnemonic (e.g. 24 words), a downloadable file, and a QR image. The client forces the user to save it at setup before E2E activation completes.

Recovery flow: enter mnemonic / import file → HKDF → `KEK_rec` → `GET /keys` → unwrap the `recovery` envelope → UMK → enrol this device.

#### Passphrase (optional convenience)

Offered at setup for users who prefer typing a memorised phrase. `KEK_pw = Argon2id(passphrase, salt)` — strong parameters applied because human passphrases are low-entropy. The UI steers users toward the recovery key as the primary entropy anchor.

### UMK Rotation

After a device is revoked, the client should rotate the UMK: generate UMK v(n+1), re-wrap it for all surviving unlock methods (`POST /keys`), bump `active_umk_version`, then lazily re-encrypt records (re-pushing rows with `enc_umk_version = n+1`). The server tolerates mixed `enc_umk_version` during the sweep — each record self-describes its generation. Rotation protects future confidentiality only; a revoked device already saw the old data.

---

## Error Handling

| Scenario | Status |
|----------|--------|
| Push before E2E initialised (`active_umk_version == 0`) | `409` |
| `POST /encryption/init` when already initialised | `409` |
| `POST /encryption/init` without a `device` envelope or without a `recovery` envelope | `422` |
| `POST /pairing/{code}/complete` when status is not `pending` | `409` |
| Pairing request expired or not found | `410` (expired) / `404` (not found) |
| Single record ciphertext exceeds 1 MiB | `413` with `{client_entity_id, payload_bytes, max_payload_bytes}` |
| Push batch exceeds 500 records | `422` |
| Per-user quota exceeded | `413` with `{total_bytes, quota_bytes, total_records, quota_records}` |
| Non-UUID `client_entity_id` | `422` (footgun-blocker) |
| Missing `payload_ciphertext` / `content_fingerprint` on non-tombstone | `422` |
| Device / envelope / pairing not found (or owned by another user) | `404` |
| Desktop device revoked mid-sync | `401 Desktop session has been revoked` (existing `get_current_user` check) |

---

## Settings UI (Optional Privacy Control)

There is no cinna-core SPA UI for browsing sync content — the server cannot decrypt it anyway. An optional "Cloud Sync" card in Settings → Security may surface:

- Sync storage usage from `GET /api/v1/app-sync/state`
- A trusted-devices list from `GET /api/v1/app-sync/devices` with per-device revoke
- A confirm-gated "Delete synced data" button (`DELETE /api/v1/app-sync`)

All user-facing sync feedback (progress, conflicts, "storage full", "enter your recovery key") lives in the native client, not in the SPA.

---

## Integration Points

- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — provides Desktop OAuth access tokens with `client_kind="desktop"` and `external_client_id` JWT claims. The live-revocation check in `get_current_user` applies to all sync endpoints automatically. `external_client_id` is stored as `last_writer_client_id` for attribution. `app_sync_device` rows may link to a `DesktopOAuthClient` via `external_client_id`.
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — for agent chat threads, `Session` rows are already server-side and restorable via `GET /api/v1/external/sessions`. The sync store keeps only an E2E-encrypted pointer (`external_session_id`) rather than duplicating message bodies.
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — the `Session` model that backs agent-chat history restore for Phase 2 chat pointers.
- **[Tasks](../input_tasks/input_tasks.md)** — `cinna_task` job runs reference server-side tasks by `short_code`; run records are portable as-is for Phase 2.

---

*Last updated: 2026-05-31*
