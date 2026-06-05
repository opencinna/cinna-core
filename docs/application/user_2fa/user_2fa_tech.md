# Two-Factor Authentication — Technical Reference

This document covers models, routes, service layer, configuration, frontend components, and test coverage for the 2FA feature. For user-facing flows and business rules see [user_2fa.md](user_2fa.md).

---

## Models

All models live under `backend/app/models/users/` and are re-exported from `backend/app/models/__init__.py`.

### `User` (extended — `backend/app/models/users/user.py`)

Three columns added to the existing `User` table:

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `two_factor_enabled` | `bool` | `False` | Master switch. `True` once the user has enrolled at least one factor and has not disabled 2FA. |
| `two_factor_enrolled_at` | `datetime \| None` | `None` | Timestamp of the first confirmed enrollment. Kept for historical reference even after disable. |
| `two_factor_last_used_at` | `datetime \| None` | `None` | Last successful second-factor verification. Displayed in the Security tab. Kept across disable. |

`UserPublic` exposes three derived flags:

| Field | Type | Note |
|-------|------|------|
| `two_factor_enabled` | `bool` | Direct column value. |
| `has_passkey` | `bool` | Computed by `MfaService.has_passkey` — `True` if any `UserPasskey` row exists. |
| `has_totp` | `bool` | Computed by `MfaService.has_totp` — `True` if a `UserTotpSecret` row exists. |

### `UserPasskey` (`user_passkey` table — `backend/app/models/users/user_passkey.py`)

One row per registered WebAuthn credential. Multiple per user.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id` `ON DELETE CASCADE`, indexed |
| `credential_id` | `bytea` | WebAuthn credential ID. Unique index. |
| `public_key` | `bytea` | COSE-encoded public key blob — opaque to the platform. |
| `sign_count` | `int` | Default 0. Monotonic counter updated on every assertion. |
| `transports` | `str` | JSON array of WebAuthn transports (`"usb"`, `"nfc"`, `"ble"`, `"internal"`, `"hybrid"`). |
| `aaguid` | `str \| None` | Authenticator AAGUID, best-effort device label. |
| `nickname` | `str` (max 64) | User-chosen label ("YubiKey 5", "iPhone Touch ID"). |
| `device_type` | `str` (max 32) | `"platform"` (biometric) or `"cross-platform"` (security key). |
| `backed_up` | `bool` | `True` when WebAuthn `flags.bs` indicates a synced/cloud-backed key (e.g. iCloud Keychain). |
| `created_at` | `datetime` | UTC now at insertion. |
| `last_used_at` | `datetime \| None` | Updated after each successful assertion. |

Public API schema: `UserPasskeyPublic` (omits `credential_id` and `public_key`). Rename schema: `UserPasskeyUpdate { nickname }`.

`MfaService.passkey_to_public(passkey)` converts a DB row to `UserPasskeyPublic`, decoding the JSON-encoded `transports` blob defensively — malformed rows degrade to an empty list.

### `UserTotpSecret` (`user_totp_secret` table — `backend/app/models/users/user_totp_secret.py`)

1:1 with `User`. Created only after enrollment is confirmed with a valid 6-digit code.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id` `ON DELETE CASCADE`, **unique** (enforces 1:1) |
| `secret_encrypted` | `Text` | Base32 TOTP secret, Fernet-encrypted. Never returned through any API. |
| `algorithm` | `str` | Default `"SHA1"`. |
| `digits` | `int` | Default `6`. |
| `period` | `int` | Default `30` (seconds). |
| `created_at` | `datetime` | UTC now. |
| `last_used_at` | `datetime \| None` | Updated on successful verification. |
| `last_used_step` | `bigint \| None` | Last accepted RFC-6238 time-step counter. Replay detection: a code whose step is `<= last_used_step` is rejected. `BigInteger` avoids Int32 overflow beyond ~2038. |

Public API schemas: `TotpEnrollResponse { secret_base32, otpauth_uri, qr_svg_data_uri, secret_token }` (begin response), `TotpFinishRequest { secret_token, code }` (finish body).

### `UserRecoveryCode` (`user_recovery_code` table — `backend/app/models/users/user_recovery_code.py`)

8 codes per user per generation batch.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id` `ON DELETE CASCADE`, indexed |
| `code_hash` | `str` | bcrypt hash of the normalised code. |
| `used_at` | `datetime \| None` | `None` = unused. Set when consumed (terminal). |
| `created_at` | `datetime` | UTC now. |
| `batch_id` | `UUID` | Groups all 8 codes from one generation event. Regeneration deletes by `user_id` (whole prior batch). |

Composite index `(user_id, used_at)`.

Public API schemas: `RecoveryCodeStatus { remaining_count, total_count, last_regenerated_at }`, `RecoveryCodesPlaintext { codes, generated_at, regenerate_warning=True }`.

`total_count` is populated by `MfaService.total_recovery_codes` and represents every code in the current batch (used and unused). The Settings UI renders this as an "N of M remaining" badge.

### `UserTrustedDevice` (`user_trusted_device` table — `backend/app/models/users/user_trusted_device.py`)

One row per trusted browser per user. Created when the user opts into "Do not ask on this device" at `/login/mfa`. Mirrors the `UserRecoveryCode` hashing pattern — **the plaintext token is never stored at rest**; only a bcrypt hash is persisted.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK, `default_factory=uuid.uuid4` |
| `user_id` | `UUID` | FK → `user.id` `ON DELETE CASCADE`, indexed |
| `token_hash` | `str` | bcrypt hash of the opaque plaintext token (`get_password_hash`). Never returned through any API. |
| `expires_at` | `datetime` (timezone-aware) | `created_at + remember_device_days`. The skip is rejected once `now >= expires_at`. |
| `created_at` | `datetime` (timezone-aware) | UTC now at insertion. |
| `last_used_at` | `datetime \| None` (timezone-aware) | Updated each time the token is used to skip a challenge. |
| `label` | `str \| None` (max 256) | Best-effort device label — truncated User-Agent captured at mint time. Display-only. |

Indexes:
- FK index on `user_id` (btree) — every lookup is "all live tokens for this user".
- Composite `ix_user_trusted_device_user_expires (user_id, expires_at)` — supports "live tokens for user" + cleanup sweep.

**Token shape:** `secrets.token_urlsafe(32)` (256-bit opaque random). No structure; no user id embedded. Only validated against the requesting user's own rows, so a stolen token cannot skip MFA for a different account.

**Resolution pattern:** Because bcrypt salts each hash, the token cannot be looked up directly by hash. `consume_trusted_device` iterates the user's live (unexpired) rows and bcrypt-verifies the candidate against each — identical to the `consume_recovery_code` pattern. Row count per user is tiny (one per device, expired rows swept hourly).

Public API schema: `TrustedDevicePublic { id, created_at, expires_at, last_used_at, label }` — defined for a future Settings device-management list; not used in MVP routes.

### `UserMfaChallenge` (`user_mfa_challenge` table — `backend/app/models/users/user_mfa_challenge.py`)

Short-lived, created after first-factor success when `two_factor_enabled=True`. Also used for step-up challenges and passkey registration challenges.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id` `ON DELETE CASCADE`, indexed |
| `challenge_token` | `str` (max 128) | `secrets.token_urlsafe(32)`. Unique index. Returned to client. |
| `webauthn_challenge` | `bytea \| None` | WebAuthn nonce. Null until `passkey/options` is called. Frozen once set — concurrent calls return the same nonce. |
| `first_factor` | `str` (max 32) | Valid values defined by `MfaFirstFactor` Literal (see below). |
| `attempts` | `int` | Default 0. Incremented on every failed verification. |
| `created_at` | `datetime` | UTC now. |
| `expires_at` | `datetime` | Created + `MFA_CHALLENGE_TTL_SECONDS`. Step-up challenges use `_STEP_UP_TTL_SECONDS=120`. |
| `consumed_at` | `datetime \| None` | Set once verification succeeds (terminal). |

Indexes: `unique(challenge_token)`, composite `(user_id, created_at)`.

Challenge lifecycle: `pending → consumed` (terminal) or `expired`. Rows older than 24 h are deleted by the cleanup scheduler.

### `MfaFirstFactor` Literal (`backend/app/models/users/user_mfa_challenge.py`)

```
MfaFirstFactor = Literal["password", "google_oauth", "step_up"]
```

Re-exported from `app.models` and `app.models.users.user_mfa_challenge`. This Literal is the single source of truth for the valid values of `UserMfaChallenge.first_factor`. The column comment in the model points here.

| Value | Origin |
|-------|--------|
| `"password"` | `/login/access-token` password login |
| `"google_oauth"` | `/auth/google/callback` OAuth login |
| `"step_up"` | Synthetic challenge for passkey enrollment and step-up assertions — never reaches the login challenge flow |

### Extended schemas in `user_mfa_challenge.py` (trusted-device additions)

`MfaVerifyRequest` gains:
```
remember_device_days: Literal[1, 7, 30] | None = None
```
`Literal` so the OpenAPI enum (and TS union) reject arbitrary durations at the edge. The service also re-checks against `MFA_TRUSTED_DEVICE_ALLOWED_DAYS` for non-route callers.

`LoginToken` gains:
```
trusted_device_token: str | None = None
```
Populated only by `/login/mfa/verify` when `remember_device_days` was set and the device was registered. `None` on every other `LoginToken` (plain login, skip-path login, no-duration verify). Adding an optional field keeps the `LoginResponse` union backward-compatible.

### API response schemas (in `user_mfa_challenge.py`)

| Schema | Kind | Description |
|--------|------|-------------|
| `MfaChallenge` | `Literal["mfa_challenge"]` | Second-factor prompt returned after first-factor success. |
| `LoginToken` | `Literal["token"]` | Access token returned when 2FA is off, after second-factor success, or on a trusted-device skip. Optionally carries `trusted_device_token` (once, on mint). |
| `LoginResponse` | union | `LoginToken \| MfaChallenge` — the discriminated union returned by login endpoints. |
| `MfaStatus` | — | Status response for the Security tab. |
| `StepUpProof` | — | Body for destructive mutations: exactly one of `password`, `totp_code`, or `passkey_assertion + passkey_challenge_token`. |
| `PasskeyAuthOptionsRequest` | — | Body of `POST /login/mfa/passkey/options`. |

---

## API Routes

### Login-time endpoints (`backend/app/api/routes/login.py`)

| Method | Path | Auth | Request | Response | Notes |
|--------|------|------|---------|----------|-------|
| `POST` | `/login/access-token` | none | `OAuth2PasswordRequestForm` + optional `X-Trusted-Device` header | `LoginResponse` | Returns `LoginToken` when 2FA off; `LoginToken` (skip) when a valid trusted-device token is presented and 2FA is on; `MfaChallenge` otherwise. |
| `POST` | `/login/mfa/passkey/options` | none | `PasskeyAuthOptionsRequest { challenge_token }` | `dict` (WebAuthn assertion options) | Loads the pending challenge, generates `PublicKeyCredentialRequestOptions`, writes `webauthn_challenge` if not already set. |
| `POST` | `/login/mfa/verify` | none | `MfaVerifyRequest { challenge_token, method, payload, remember_device_days? }` | `LoginToken` | Applies anonymous per-source rate limit first, then resolves the challenge, then applies per-user rate limit, then dispatches to passkey / TOTP / recovery handler. If `remember_device_days` is set, mints a `UserTrustedDevice` row and returns the plaintext token on `LoginToken.trusted_device_token` (once). |

`MfaVerifyRequest.method` is `Literal["passkey", "totp", "recovery"]`. `payload` shape:
- passkey: full WebAuthn `AuthenticationResponseJSON` dict
- totp: `{"code": "123456"}`
- recovery: `{"code": "xxxx-xxxx"}`

#### Security / abuse protection on `/login/mfa/verify`

The route enforces two independent rate-limit layers before delegating to `MfaService.verify_challenge`:

1. **Anonymous per-source** (`MfaService.check_anonymous_verify_rate_limit`) — 20 attempts per 5 min per client IP, checked before any challenge resolution. A spray attacker supplying random tokens hits this limit immediately.
2. **Per-user** (`MfaService.check_verify_rate_limit`) — 10 attempts per 5 min per `user_id`, checked after the challenge resolves to a user.

Bad-token probes (`challenge_not_found`, `challenge_expired`, `challenge_consumed`) and orphaned-challenge probes (challenge found but user row missing) are logged at `WARNING` level (`mfa_verify_bad_challenge code=… source=…` / `mfa_verify_orphan_challenge user_id=… source=…`). `SecurityEvent` rows are not written for these because there is no `user_id` to attach them to.

### Google OAuth callback (`backend/app/api/routes/oauth.py`)

`POST /auth/google/callback` returns the same `LoginResponse`. `GoogleCallbackRequest` is a JSON body model with fields `code`, `state`, and the new optional `trusted_device_token: str | None`. The `AuthService.authenticate_with_google()` result carries a `requires_mfa` flag and `mfa_challenge` handle. When `requires_mfa` is true, the route first attempts the trusted-device skip (via `MfaService.consume_trusted_device`): on a match it mints the access token inline via `AuthService.create_access_token(result.user.id)` and returns `LoginToken` directly; otherwise it returns the `MfaChallenge`.

### MFA management endpoints (`backend/app/api/routes/mfa.py`)

All require `CurrentUser`. Prefix: `/users/me/mfa`.

| Method | Path | Request body | Response | Notes |
|--------|------|-------------|----------|-------|
| `GET` | `/status` | — | `MfaStatus` | Security tab header. |
| `GET` | `/passkeys` | — | `UserPasskeysPublic` | Lists registered passkeys, newest first. |
| `POST` | `/passkeys/begin` | `PasskeyBeginRequest` (empty) | `{ challenge_token, options }` | Generates and stores a transient challenge. Options nested under `options` key — client passes it straight to `@simplewebauthn/browser`. |
| `POST` | `/passkeys/finish` | `PasskeyFinishRequest { challenge_token, credential, nickname }` | `{ passkey: UserPasskeyPublic, recovery_codes: RecoveryCodesPlaintext \| null }` | Verifies attestation; `recovery_codes` non-null only on first enrollment. Minting the first-time recovery batch is owned by the service, not the route. |
| `PATCH` | `/passkeys/{passkey_id}` | `UserPasskeyUpdate { nickname }` | `UserPasskeyPublic` | Rename. |
| `DELETE` | `/passkeys/{passkey_id}` | — | `Message` | If this is the user's last 2FA factor, 2FA is automatically turned off (wipe-and-flag, same as `POST /disable`). |
| `POST` | `/totp/begin` | — | `TotpEnrollResponse` | Returns QR + signed handle; nothing persisted yet. Raises `totp_already_enrolled` (409) if TOTP is already enrolled — checked in the service. |
| `POST` | `/totp/finish` | `TotpFinishRequest { secret_token, code }` | `{ message, recovery_codes: RecoveryCodesPlaintext \| null }` | Persists secret only if code verifies. Minting the first-time recovery batch is owned by the service, not the route. |
| `DELETE` | `/totp` | `StepUpProof` | `Message` | Requires fresh factor. Idempotent — succeeds even when TOTP isn't enrolled. If this is the user's last 2FA factor, 2FA is automatically turned off. |
| `GET` | `/recovery-codes` | — | `RecoveryCodeStatus` | Never returns plaintext codes. Returns `remaining_count` and `total_count`. |
| `POST` | `/recovery-codes/regenerate` | `StepUpProof` | `RecoveryCodesPlaintext` | Wipes prior batch; requires factor enrollment + fresh proof. |
| `POST` | `/step-up/passkey/options` | — | `StepUpPasskeyOptions { challenge_token, options }` | Issues a step-up passkey challenge for destructive actions. |
| `POST` | `/disable` | `StepUpProof` | `Message` | Wipes all factors; requires fresh proof. |

**Route layer purity:** `routes/mfa.py` delegates all business logic to `MfaService` and `UserService`. It contains no raw SQL queries, no JSON-decode of passkey transports, no enrollment-precondition checks, and no `challenge_token` extraction from option dicts. The route layer is a pure serialiser — validate input, call service, serialize output.

### Error translation (`backend/app/api/routes/_mfa_errors.py`)

All `MfaService` `ValueError(code)` exceptions are translated by `translate_mfa_error`:

| Error code | HTTP status |
|-----------|-------------|
| `invalid_code` | 400 |
| `invalid_assertion` | 400 |
| `invalid_secret_token` | 400 |
| `invalid_method` | 400 |
| `invalid_trust_duration` | 400 |
| `challenge_not_found` | 404 |
| `passkey_not_found` | 404 |
| `factor_not_enrolled` | 404 |
| `challenge_expired` | 410 |
| `challenge_consumed` | 410 |
| `attempt_limit_exceeded` | 429 |
| `rate_limited` | 429 |
| `totp_already_enrolled` | 409 |
| `step_up_required` | 401 |

Note: `invalid_trust_duration` is only reachable via direct service calls; the `Literal[1,7,30]` on `MfaVerifyRequest.remember_device_days` causes the API edge to return 422 for out-of-allowlist values.

Response body: `{ "detail": { "code": "<error_code>", "message": "<error_code>" } }`. The frontend keys on `error.detail.code`.

---

## Service Layer

### `MfaService` (`backend/app/services/users/mfa_service.py`)

Stateless — all methods are `@staticmethod` and accept an explicit `session`. Raises `ValueError(code)` on failure; the route layer translates to `HTTPException`.

**Challenge lifecycle:**

| Method | Description |
|--------|-------------|
| `issue_challenge(*, session, user, first_factor)` | Creates a `UserMfaChallenge` row, logs `MFA_CHALLENGE_ISSUED`. |
| `get_challenge(*, session, challenge_token)` | Loads and validates a challenge — raises on not-found, expired, consumed, or attempt-limit. |
| `_consume_challenge(session, challenge)` | Marks `consumed_at`. |
| `_record_failed_attempt(session, challenge)` | Increments `attempts` and commits. |
| `verify_challenge(*, session, challenge_token, method, payload, remember_device_days=None, user_agent=None)` | Dispatches to passkey / TOTP / recovery; bumps attempts + logs failure atomically on error; updates `two_factor_last_used_at` + logs `MFA_CHALLENGE_SUCCESS` on success. If `remember_device_days` is set, calls `register_trusted_device` inside the same transaction. Returns `tuple[User, str \| None]` (user, plaintext trusted-device token or None). |

**WebAuthn registration:**

| Method | Description |
|--------|-------------|
| `begin_passkey_registration(*, session, user)` | Generates `PublicKeyCredentialCreationOptions` (excludes already-registered credentials). Returns `(challenge_row, options_dict)`. |
| `finish_passkey_registration(*, session, user, challenge_token, credential, nickname)` | Verifies attestation via `webauthn.verify_registration_response`; persists `UserPasskey`; calls `_mark_factor_enrolled`; mints recovery codes when this is the first factor. Returns `(passkey, recovery_codes_or_None)`. |

**WebAuthn authentication:**

| Method | Description |
|--------|-------------|
| `begin_passkey_authentication(*, session, challenge)` | Generates `PublicKeyCredentialRequestOptions` bound to the challenge. Freezes `webauthn_challenge` on first call — concurrent calls return the same nonce. |
| `_verify_passkey_login(*, session, challenge, user, payload)` | Verifies assertion; updates `sign_count` / `last_used_at`; logs `MFA_SIGN_COUNT_REGRESSION` if count regresses; logs `MFA_PASSKEY_INVALID_ORIGIN` on origin mismatch. |

**TOTP:**

| Method | Description |
|--------|-------------|
| `begin_totp_enrollment(*, session, user)` | Checks `totp_already_enrolled` first. Generates secret; builds Fernet-encrypted envelope `{user_id, secret, exp, nonce}` as `secret_token`; returns `{secret_base32, otpauth_uri, qr_svg_data_uri, secret_token}`. Nothing written to DB. |
| `finish_totp_enrollment(*, session, user, secret_token, code)` | Decrypts and validates envelope; verifies 6-digit code with ±1 window; persists `UserTotpSecret` with `last_used_step` set to the enrollment code's step; mints recovery codes when this is the first factor. Returns `(row, recovery_codes_or_None)`. |
| `verify_totp(*, session, user, code)` | Decrypts secret; accepts current ±1 step; rejects replay via `last_used_step`; flushes `last_used_step` immediately. Returns `bool`. |

**Recovery codes:**

| Method | Description |
|--------|-------------|
| `generate_recovery_codes(*, session, user, require_enrolled=False)` | Wipes prior batch; mints `MFA_RECOVERY_CODE_COUNT` codes from `_RECOVERY_CODE_ALPHABET` (no ambiguous glyphs); stores bcrypt hashes; logs `MFA_RECOVERY_CODES_REGENERATED`. Returns plaintext list. |
| `consume_recovery_code(*, session, user, code)` | Normalises code (strip, uppercase, remove non-alnum); verifies bcrypt against unused rows; marks `used_at`; logs `MFA_RECOVERY_CODE_USED`. Returns `bool`. |
| `remaining_recovery_codes(*, session, user)` | Count of unused codes. |
| `total_recovery_codes(*, session, user)` | Count of all codes in the current batch (used and unused). Used by the Settings UI "N of M" badge and `RecoveryCodeStatus.total_count`. |
| `last_recovery_batch_at(*, session, user)` | `created_at` of the most recent code row. |
| `regenerate_recovery_codes_with_step_up(*, session, user, proof)` | Enforces enrollment precondition, then calls `require_recent_factor`, then delegates to `generate_recovery_codes`. Owned by the service so the precondition order is always correct. |

**Trusted devices:**

| Method | Description |
|--------|-------------|
| `register_trusted_device(*, session, user, days, label)` | Validates `days ∈ MFA_TRUSTED_DEVICE_ALLOWED_DAYS` (raises `"invalid_trust_duration"` otherwise). Generates `secrets.token_urlsafe(32)`; inserts `UserTrustedDevice` with `token_hash = get_password_hash(token)`, `expires_at = now + timedelta(days=days)`, `label` (truncated to 256 chars). Flushes (not commits) so the row gets its `id` for the audit detail; the caller (`verify_challenge`) owns the commit. Logs `MFA_TRUSTED_DEVICE_REGISTERED` (details: `{days, device_id}`). Returns the **plaintext** token. |
| `consume_trusted_device(*, session, user, token)` | Returns `False` immediately if `token` is falsy. Loads the user's rows where `expires_at > now(UTC)`; bcrypt-verifies `token` against each `token_hash`. On match: sets `last_used_at = now`, logs `MFA_TRUSTED_DEVICE_USED` (details: `{device_id}`), commits, returns `True`. On no match: returns `False` (silent — no error, no oracle). |
| `purge_expired_trusted_devices(*, session)` | `DELETE FROM user_trusted_device WHERE expires_at < now()`. Returns the number of rows deleted. Called by the hourly cleanup job. |

**Step-up re-auth:**

| Method | Description |
|--------|-------------|
| `require_recent_factor(*, session, user, proof)` | Accepts `StepUpProof`; dispatches to password verify, TOTP verify, or passkey assertion (via a step-up challenge row). Raises `"step_up_required"` if proof is missing or invalid. |
| `begin_step_up_passkey(*, session, user)` | Creates a step-up `UserMfaChallenge` (TTL 120 s) and returns `(challenge_row, options_dict)`. |

**Helpers:**

| Method | Description |
|--------|-------------|
| `has_passkey(*, session, user_id)` / `has_totp(*, session, user_id)` | Cheap existence checks for `UserPublic` derived flags. |
| `list_passkeys(*, session, user)` | All passkeys for `user`, newest first. |
| `passkey_to_public(passkey)` | Converts a `UserPasskey` DB row to `UserPasskeyPublic`, decoding `transports` JSON defensively. |
| `check_verify_rate_limit(*, session, user)` | In-memory token bucket keyed by `user.id`. 10 attempts / 5 min. Writes `MFA_RATE_LIMITED` event on hit. Opportunistic sweep when `_verify_rate_limit_log` exceeds `_RATE_LIMIT_SWEEP_THRESHOLD=1024` entries. |
| `check_anonymous_verify_rate_limit(*, source_key)` | Per-IP rate limit for bad-token branch of `/login/mfa/verify`. 20 attempts / 5 min. No DB write (no user to attach). Same opportunistic sweep when `_anonymous_verify_rate_limit_log` exceeds threshold. |
| `allowed_methods_for_user(*, session, user)` | Returns subset of `["passkey", "totp", "recovery"]` the user can actually use. |
| `rename_passkey(*, session, user, passkey_id, nickname)` | Owner-scoped rename. |
| `delete_passkey(*, session, user, passkey_id)` | Owner-scoped delete with last-factor auto-disable. |
| `disable_totp(*, session, user)` | Idempotent TOTP removal with last-factor auto-disable. |
| `_mark_factor_enrolled(session, user)` | Flips `two_factor_enabled=True` and sets `two_factor_enrolled_at` on first enrollment. Idempotent. |
| `_log_event(*, session, user, event_type, severity, details)` | Writes a `SecurityEvent` row inside the same transaction. |

**Recovery code character set:** `_RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"` (excludes `0`, `O`, `1`, `I`, `l` to avoid visual confusion). Codes formatted as `XXXX-XXXX...` (4-char hyphen-separated groups).

### `UserService.disable_all_factors` (`backend/app/services/users/user_service.py`)

Accepts a `reason` parameter (default `"user_initiated"`). Deletes `UserPasskey`, `UserTotpSecret`, `UserRecoveryCode`, `UserMfaChallenge`, and `UserTrustedDevice` rows for the user in a single transaction; sets `two_factor_enabled=False`; calls `session.refresh(user)` so post-call reads see the fresh state; preserves `two_factor_enrolled_at` and `two_factor_last_used_at` for audit reference; logs `MFA_DISABLED` with `details={"reason": reason}`.

The inclusion of `UserTrustedDevice` in the wipe ensures that any live "Do not ask on this device" tokens become inert immediately when 2FA is disabled. This covers all three disable entry points: the explicit `POST /mfa/disable`, last-factor passkey delete, and last-factor TOTP removal — all route through this method.

Called with `reason="last_factor_removed"` by `MfaService.delete_passkey` and `MfaService.disable_totp` when the removed factor is the user's last one. Called with `reason="user_initiated"` (default) by the `POST /mfa/disable` route.

---

## Encryption Details

| Data | Storage | Mechanism |
|------|---------|-----------|
| TOTP secret | `UserTotpSecret.secret_encrypted` | Fernet `encrypt_field` from `app.core.security`. Decrypted only inside `MfaService`. Never returned through any API. |
| `secret_token` (TOTP enrollment handle) | Client memory only | Fernet-encrypted JSON `{user_id, secret, exp, nonce}`. 10-minute TTL. User binding prevents use on a different account. |
| Recovery codes | `UserRecoveryCode.code_hash` | bcrypt via `get_password_hash` / `verify_password`. Plaintext returned once at generation time. |
| Challenge tokens | `UserMfaChallenge.challenge_token` | `secrets.token_urlsafe(32)`. Constant-time comparison via `secrets.compare_digest` inside `get_challenge`. |
| WebAuthn private key | Authenticator only | Never leaves the device. The platform stores only the public key. |

---

## Background Job

`backend/app/services/users/mfa_cleanup_service.py` — APScheduler `BackgroundScheduler` with a single `interval` job.

- **Schedule:** every 1 hour.
- **Tasks (both in one `run_cleanup()` call, separate transactions):**
  1. `DELETE FROM user_mfa_challenge WHERE created_at < now() - 24h` — removes stale challenge rows.
  2. `MfaService.purge_expired_trusted_devices(session)` — removes `user_trusted_device` rows where `expires_at < now()`. Expired device rows are also rejected at read time in `consume_trusted_device`, so this is purely housekeeping.
- **Pattern:** mirrors `desktop_auth_scheduler` and `cli_setup_token_scheduler`. Idempotent; failures on one sweep are logged and ignored without masking the other sweep; the next run picks up missed rows.
- **Startup / shutdown:** `start_scheduler()` / `shutdown_scheduler()` registered in `app.main` lifespan.

---

## Configuration (`backend/app/core/config.py`)

| Setting | Type | Default | Purpose |
|---------|------|---------|---------|
| `MFA_CHALLENGE_TTL_SECONDS` | `int` | `300` | Lifetime of a login `UserMfaChallenge` (5 min). |
| `MFA_MAX_ATTEMPTS_PER_CHALLENGE` | `int` | `5` | Per-challenge attempt cap before `429`. |
| `MFA_RECOVERY_CODE_COUNT` | `int` | `8` | Recovery codes generated per batch. |
| `MFA_RECOVERY_CODE_LENGTH` | `int` | `8` | Raw character count per code (formatted as `XXXX-XXXX`). |
| `MFA_WEBAUTHN_RP_NAME` | `str` | `"Cinna"` | Relying-party display name in WebAuthn ceremonies. |
| `MFA_WEBAUTHN_RP_ID` | `str \| None` | `None` | Override RP ID; defaults to `urlparse(FRONTEND_HOST).hostname`. |
| `MFA_TOTP_ISSUER` | `str` | `"Cinna"` | Issuer field in the `otpauth://` URI. |
| `MFA_TRUSTED_DEVICE_ALLOWED_DAYS` | `list[int]` | `[1, 7, 30]` | Allowlist of valid `remember_device_days` values. The service re-checks against this list for non-route callers. Keep in sync with the `Literal[1,7,30]` on `MfaVerifyRequest.remember_device_days`. |

Computed properties (not environment variables):
- `settings.mfa_webauthn_rp_id` — returns `MFA_WEBAUTHN_RP_ID` if set, otherwise the hostname of `FRONTEND_HOST`.
- `settings.mfa_webauthn_expected_origin` — returns `FRONTEND_HOST` (full URL) used in `verify_registration_response` / `verify_authentication_response`.

---

## Security Events

All events are written via `MfaService._log_event` inside the same database transaction as the triggering action.

| Constant | Trigger | Severity |
|----------|---------|---------|
| `MFA_ENROLLED` | Passkey or TOTP successfully enrolled. Details: `factor`, `first_factor` (bool). | medium |
| `MFA_DISABLED` | All factors wiped. Details: `reason` — `"user_initiated"` (via `POST /mfa/disable`) or `"last_factor_removed"` (via `DELETE /passkeys/{id}` or `DELETE /totp` when it was the last factor). Written by `UserService.disable_all_factors`. | medium |
| `MFA_CHALLENGE_ISSUED` | Login or step-up challenge created. Details: `first_factor`. | low |
| `MFA_CHALLENGE_SUCCESS` | Second factor verified; access token about to be issued. Details: `method`, `first_factor`. | low |
| `MFA_CHALLENGE_FAILED` | Verification attempt failed (wrong code, bad assertion, challenge lifecycle error). Details: `method`, `reason`. | medium |
| `MFA_RECOVERY_CODE_USED` | Single recovery code consumed. Details: `recovery_code_id`. | medium |
| `MFA_RECOVERY_CODES_REGENERATED` | Recovery code batch regenerated. Details: `count`. | medium |
| `MFA_RATE_LIMITED` | Per-user rate limit exceeded on `POST /login/mfa/verify`. Details: `window_seconds`, `max_attempts`. | medium |
| `MFA_SIGN_COUNT_REGRESSION` | Passkey sign count regressed (possible clone). Still authenticates but logged. Details: `passkey_id`, `old_sign_count`, `new_sign_count`. | high |
| `MFA_PASSKEY_INVALID_ORIGIN` | WebAuthn assertion failed due to RP ID / origin mismatch. Details: `error`. | high |
| `MFA_TRUSTED_DEVICE_REGISTERED` | A trusted-device token was minted at verify time. Details: `days`, `device_id`. The plaintext token is never logged. | medium |
| `MFA_TRUSTED_DEVICE_USED` | A valid trusted-device token was used to skip the MFA challenge on login. Details: `device_id`. | low |

Note: bad-token and orphaned-challenge probes on `/login/mfa/verify` are logged at `WARNING` level in server logs only (no `SecurityEvent` row — there is no `user_id` to attribute them to).

---

## Database Migrations

### Initial 2FA tables

File: `backend/app/alembic/versions/538667612c3d_add_user_2fa_tables.py`

Revision: `538667612c3d` (down: `dd4ef5a6b7c8`)

**Upgrade:** Adds three columns to `user` + creates `user_mfa_challenge`, `user_passkey`, `user_totp_secret`, `user_recovery_code` tables with the indexes and FKs described in the Models section above.

**Downgrade:** Drops the four tables in reverse order, then drops the three `user` columns.

**Backfill:** none — all existing users default to `two_factor_enabled=False`.

### Trusted-device table

File: `backend/app/alembic/versions/465c41b435ab_add_user_trusted_device.py`

Revision: `465c41b435ab` (down: `581dd9e44be1`)

**Upgrade:** Creates `user_trusted_device` table with columns `id`, `user_id` (FK → `user.id` ON DELETE CASCADE), `token_hash`, `expires_at` (timezone-aware), `created_at` (timezone-aware), `last_used_at` (nullable, timezone-aware), `label` (nullable, max 256). Creates `ix_user_trusted_device_user_expires (user_id, expires_at)` composite index and `ix_user_trusted_device_user_id` btree index on `user_id`.

**Downgrade:** Drops the composite index, drops the `user_id` index, drops the table.

**Backfill:** none — feature is opt-in per device; no existing rows.

---

## Dependencies Added

`backend/pyproject.toml`:
- `webauthn` (Yubico `py_webauthn`) — WebAuthn registration/authentication ceremonies.
- `pyotp` — RFC-6238 TOTP generation and verification.
- `qrcode[pil]` — SVG QR code generation for the TOTP enrollment modal.

`frontend/package.json`:
- `@simplewebauthn/browser` — browser-side WebAuthn `navigator.credentials.create()` / `.get()` wrapper.

---

## Frontend

### Files

| File | Role |
|------|------|
| `frontend/src/utils/webauthn.ts` | Thin wrapper over `@simplewebauthn/browser`. Exports `startRegistration`, `startAuthentication`, `isWebAuthnSupported`, `isWebAuthnUserCancellation`. |
| `frontend/src/utils/trustedDevice.ts` | Centralized helpers for the trusted-device localStorage slot. Exports `TRUSTED_DEVICE_KEY = "mfa.trusted_device_token"`, `getTrustedDeviceToken()`, `setTrustedDeviceToken(token)`, `clearTrustedDeviceToken()`. All three are try/catch-wrapped (non-fatal in private mode / quota-exceeded scenarios). |
| `frontend/src/components/Auth/MfaChallengeContext.tsx` | React context holding `{ challenge, redirectTo }` in memory. Provider mounted in `__root.tsx`. Never persisted to storage. |
| `frontend/src/routes/login/mfa.tsx` | Public TanStack route — renders `<TwoFactorChallenge />` inside `<AuthLayout>`. |
| `frontend/src/components/Auth/TwoFactorChallenge.tsx` | Challenge page orchestrator — reads from `MfaChallengeContext`, renders passkey button, TOTP form, or recovery form based on `allowed_methods`. Manages structured `TotpErrorKind` state (`"invalid_code"` \| `"attempt_limit_exceeded"`); passes `autoClearOnInvalid` to `TotpForm` only on `invalid_code`. Also renders a full-width shadcn `Select` (outside the method-specific blocks, visible in both primary and recovery modes) for "Do not ask on this device" with values `off / 1 / 7 / 30`; passes `remember_device_days: RememberDays` into every `verify()` call. On `onSuccess`, calls `setTrustedDeviceToken(data.trusted_device_token)` before `window.location.assign(target)` (and deliberately does NOT call `clearChallenge()` to avoid the race with the "no challenge → /login" effect). |
| `frontend/src/components/Auth/PasskeyButton.tsx` | Calls `fetchLoginPasskeyOptions` then `startAuthentication`; dispatches `useVerifyMfaMutation`. |
| `frontend/src/components/Auth/TotpForm.tsx` | 6-digit OTP input with `inputMode="numeric"` and `autocomplete="one-time-code"`. Accepts `label: string \| null` (hides the label when `null`, using `aria-label` instead), `errorMessage: string \| null` (override helper text), and `autoClearOnInvalid: boolean` (triggers wave auto-clear animation on a rising `invalid` edge). |
| `frontend/src/index.css` | Defines `@keyframes shake-x` (whole-row shake) and `@keyframes shake-slot` (per-slot shake used in the wave), exposed as Tailwind utilities `animate-shake-x` / `animate-shake-slot`. Suppressed by `prefers-reduced-motion`. |
| `frontend/src/hooks/useMfa.ts` | All React Query hooks and the `runStepUpPasskey` helper. |
| `frontend/src/hooks/useAuth.ts` | Exports `isMfaChallengeResponse` guard (used by `GoogleLoginButton`). The `login()` function passes `xTrustedDevice: getTrustedDeviceToken() ?? undefined` to `LoginService.loginAccessToken`; the OpenAPI-generated header field is named `xTrustedDevice` (derived from `X-Trusted-Device`). |
| `frontend/src/components/Auth/GoogleLoginButton.tsx` | In the `mutationFn`, adds `trusted_device_token: getTrustedDeviceToken() ?? undefined` to the `GoogleCallbackRequest` body. The `onSuccess` branch handles the skip-path `LoginToken` normally (the backend never returns a `trusted_device_token` from the Google callback — minting only happens at `/login/mfa/verify`). |
| `frontend/src/components/UserSettings/Security/SecurityTab.tsx` | Orchestrator for the Security settings tab. Passes `mfaStatus` down to `PasskeySection` and `DisableTotpDialog`. |
| `frontend/src/components/UserSettings/Security/PasskeySection.tsx` | Passkey card — uses `useMfaPasskeys`; passes `mfaStatus` to `PasskeyList`. |
| `frontend/src/components/UserSettings/Security/PasskeyList.tsx` | Passkey rows with rename and delete. Receives `mfaStatus` to detect when a deletion would remove the last factor and switch dialog copy accordingly. |
| `frontend/src/components/UserSettings/Security/AddPasskeyDialog.tsx` | Nickname input + `useEnrollPasskeyMutation` ceremony. |
| `frontend/src/components/UserSettings/Security/TotpSection.tsx` | TOTP card — status + enroll/disable buttons. |
| `frontend/src/components/UserSettings/Security/EnrollTotpDialog.tsx` | QR display + code input + `useFinishTotpEnrollmentMutation`. |
| `frontend/src/components/UserSettings/Security/DisableTotpDialog.tsx` | Step-up proof form for TOTP removal. Receives `mfaStatus`; detects `isLastFactor` condition and switches title, description, and button label to last-factor-aware copy ("Remove and turn off 2FA"). |
| `frontend/src/components/UserSettings/Security/RecoveryCodesSection.tsx` | Recovery-codes card — remaining count + regenerate button. |
| `frontend/src/components/UserSettings/Security/RecoveryCodesDialog.tsx` | One-shot plaintext display — copy per-code, copy-all, download `.txt`. Closes only after "I've saved these codes". |
| `frontend/src/components/UserSettings/Security/RegenerateRecoveryDialog.tsx` | Step-up proof + confirmation before regeneration. |
| `frontend/src/components/UserSettings/Security/StepUpProofForm.tsx` | Shared proof form used by disable / regenerate / delete-factor dialogs. Offers password, TOTP, or passkey buttons depending on availability. |
| `frontend/src/components/UserSettings/Security/DisableTwoFactorDialog.tsx` | Global "Turn off 2FA" dialog with step-up proof. |
| `SecurityActivityList` (planned, not yet created) | Placeholder component for MFA-filtered security events. Full list endpoint not yet implemented. |
| `frontend/src/components/UserSettings/Security/EnableTwoFactorBanner.tsx` | Dashboard nudge banner for users without 2FA. Dismissal stored at `localStorage["mfa.enable_banner.dismissed.{user_id}"]`. |

### React Query Keys

| Key | Hook | Invalidated by |
|-----|------|----------------|
| `["mfa", "status"]` | `useMfaStatus` | Any factor add/remove, disable, currentUser change. |
| `["mfa", "passkeys"]` | `useMfaPasskeys` | Passkey enroll, rename, delete. |
| `["mfa", "recovery"]` | `useRecoveryCodesStatus` | Recovery code regeneration. |

### Hooks in `useMfa.ts`

| Hook / helper | Description |
|---------------|-------------|
| `useMfaStatus()` | Query `GET /users/me/mfa/status`. |
| `useMfaPasskeys()` | Query `GET /users/me/mfa/passkeys`. |
| `useRecoveryCodesStatus()` | Query `GET /users/me/mfa/recovery-codes`. |
| `useEnrollPasskeyMutation()` | Chains begin → browser create → finish. Returns `{ passkey, recovery_codes }`. |
| `useBeginTotpEnrollmentMutation()` | `POST /mfa/totp/begin`. |
| `useFinishTotpEnrollmentMutation()` | `POST /mfa/totp/finish`. |
| `useEnrollTotpMutation()` | Convenience wrapper returning `{ begin, finish }`. |
| `useRenamePasskeyMutation()` | `PATCH /mfa/passkeys/{id}`. |
| `useDeletePasskeyMutation()` | `DELETE /mfa/passkeys/{id}`. |
| `useDisableTotpMutation()` | `DELETE /mfa/totp` with step-up proof. |
| `useRegenerateRecoveryCodesMutation()` | `POST /mfa/recovery-codes/regenerate` with step-up proof. |
| `useDisableTwoFactorMutation()` | `POST /mfa/disable` with step-up proof. |
| `useVerifyMfaMutation()` | `POST /login/mfa/verify` — login-time MFA. |
| `runStepUpPasskey()` | Async helper: calls `MfaService.beginStepUpPasskey()` then `startAuthentication`; returns the `StepUpProof` fields. |
| `fetchLoginPasskeyOptions(challengeToken)` | Async helper: calls `POST /login/mfa/passkey/options`; returns typed WebAuthn options. |

### `useAuth` / `loginMutation` branch

After `POST /login/access-token` or the Google OAuth callback returns `LoginResponse`, `useAuth` checks `response.kind`:
- `"token"` → store `access_token` in `localStorage`, navigate to post-login target (existing path).
- `"mfa_challenge"` → call `MfaChallengeContext.setChallenge(challenge, redirectTo)`, then navigate to `/login/mfa`.

---

## Tests

### Files

| File | Coverage |
|------|---------|
| `backend/tests/api/users/test_mfa_totp_login.py` | Login regression (no 2FA), login MFA branch, TOTP enroll success/failure, TOTP verify ±1 step, replay rejection, recovery code one-shot + regeneration, challenge expiry (monkeypatched clock), attempt lockout, per-user rate limit, last-factor auto-disable (TOTP), disable 2FA step-up, auth-required guard, security event assertions, `secret_token` binding + expiry, `UserPublic` derived flags. |
| `backend/tests/api/users/test_mfa_passkeys.py` | Passkey enrollment (mocked WebAuthn library), passkey login assertion (mocked), sign-count update, last-factor auto-disable (passkey), passkey rename / delete, WebAuthn assertion failure handling. |
| `backend/tests/utils/mfa.py` | Shared test helpers — `signup_user`, `login`, `enroll_totp`, `enroll_passkey` (with `unittest.mock.patch` on `verify_registration_response` / `verify_authentication_response`), challenge helpers, security-event assertion. |

### WebAuthn test strategy

`webauthn.verify_registration_response` and `verify_authentication_response` are patched at the service-layer import path (`app.services.users.mfa_service.verify_registration_response`) using `unittest.mock.patch`. Fake `VerifiedRegistration` / `VerifiedAuthentication` `MagicMock` objects expose exactly the attributes `MfaService` reads. Tests that do not touch WebAuthn (TOTP, recovery codes, challenge lifecycle, rate-limit) run against the real library paths.

### Challenge expiry tests

Monkeypatch `app.services.users.mfa_service.datetime` to return a far-future `datetime.now(UTC)` — avoids real clock manipulation or sleeps.

---

## Known Gaps

| Gap | Notes |
|-----|-------|
| Google-OAuth MFA full-flow test | The IdP is not contacted during tests; the Google-OAuth branch is stub-tested via unit paths. A full end-to-end test requires an IdP mock. The Google-callback trusted-device skip path therefore also has no automated test. |
| `MFA_SIGN_COUNT_REGRESSION` and `MFA_PASSKEY_INVALID_ORIGIN` test coverage | Both events are wired in the service and manually verified, but lack dedicated test scenarios. |
| `SecurityActivityList` | The component is a placeholder; it renders an empty state. The `GET /security-events/` endpoint exists but a filtered list endpoint scoped to MFA events only is not yet implemented. |
| Admin disable-2FA override | No endpoint for a superuser to disable 2FA on another user's account. Current escape hatch is a direct database operation. |
| Trusted-device list and per-device revoke | There is currently no `GET /users/me/mfa/trusted-devices` list endpoint and no `DELETE /users/me/mfa/trusted-devices/{id}` single-revoke endpoint in Settings. `TrustedDevicePublic` is defined in the model for when this is added. The current revocation mechanism is wipe-on-disable (all devices are revoked when 2FA is turned off). |

---

*Last updated: 2026-06-05*
