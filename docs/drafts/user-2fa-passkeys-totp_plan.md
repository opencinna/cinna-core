# User 2FA — Passkeys & Authenticator Apps (Implementation Plan)

> **Status:** Draft architectural plan
> **Feature name:** `user-2fa-passkeys-totp`
> **Scope:** Optional, user-enabled two-factor authentication layered on top of the existing password and Google OAuth login flows. Two methods supported in MVP: **WebAuthn passkeys** (FIDO2 / platform & roaming authenticators) and **TOTP authenticator apps** (Google Authenticator, Authy, 1Password, etc.).

---

## 1. Overview

This feature adds optional second-factor authentication to the user account. After enabling 2FA from the profile settings, every subsequent login requires the user to complete a second step — either by presenting a registered **passkey** (WebAuthn) or by entering a **TOTP code** from an authenticator app. Recovery codes are issued at enrollment so users can regain access if all factors are lost.

**Core capabilities (MVP):**

- Optional per-user setting (`two_factor_enabled` on `User`); 2FA off by default for backwards compatibility.
- Enroll one or more **passkeys** (WebAuthn, resident-key preferred, platform & cross-platform authenticators allowed).
- Enroll **TOTP** (one secret per user; QR code shown once during enrollment).
- Login flow upgraded to a two-step "challenge" exchange: password / OAuth → short-lived `mfa_challenge_token` → second factor → final access token.
- Single-use **recovery codes** (8 codes, hashed at rest) generated at first enrollment and regenerable from settings.
- A new **Security** tab in User Settings to enroll/disable factors, rename passkeys, regenerate recovery codes, and audit recent logins.
- Audit trail of 2FA-relevant events (enroll / disable / challenge success/failure / recovery-code consumed) via existing `SecurityEvent` infrastructure.

**High-level flow:**

```
Login
 ┌────────────────────────────────────────────────────────────────────────┐
 │  1. Submit credentials (password or Google OAuth)                      │
 │     ──► UserService.authenticate / AuthService.google_callback         │
 │                                                                        │
 │  2.   if user.two_factor_enabled is False:                             │
 │           return Token (access_token)   ← unchanged path               │
 │       else:                                                            │
 │           return MfaChallenge { challenge_token, methods[] }           │
 │                                                                        │
 │  3.  Frontend shows 2FA step:                                          │
 │        - "Use passkey" button     → WebAuthn assertion                 │
 │        - "Enter 6-digit code"     → TOTP form                          │
 │        - "Use recovery code"      → recovery-code form                 │
 │                                                                        │
 │  4.  POST /login/mfa/verify  { challenge_token, method, payload }      │
 │     ──► MfaService.verify_challenge                                    │
 │     ──► Returns final Token (access_token)                             │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Architecture Overview

### Component diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              Frontend (React)                            │
│  ┌────────────────┐   ┌────────────────┐    ┌────────────────────────┐  │
│  │  Login page    │   │  Settings →    │    │  Mfa challenge page    │  │
│  │  Signup page   │   │  Security tab  │    │  (/login/mfa)          │  │
│  └────────────────┘   └────────────────┘    └────────────────────────┘  │
│         │                     │                        │                │
│         ▼                     ▼                        ▼                │
│  POST /login/...        Enroll/Disable          POST /login/mfa/verify  │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                            Backend (FastAPI)                             │
│  ┌──────────────────────────────┐    ┌────────────────────────────────┐ │
│  │ routes/login.py              │    │ routes/mfa.py (new)            │ │
│  │  • access-token (modified)   │    │  • enroll/begin (passkey)      │ │
│  │  • mfa/verify (new)          │    │  • enroll/finish (passkey)     │ │
│  └──────────────────────────────┘    │  • totp/begin                  │ │
│                                       │  • totp/finish                 │ │
│                                       │  • recovery-codes/regenerate  │ │
│                                       │  • factors  (list/delete)     │ │
│                                       │  • status                     │ │
│                                       └────────────────────────────────┘ │
│                                               │                          │
│                                               ▼                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ services/users/mfa_service.py (new)                                │ │
│  │   • issue_challenge / verify_challenge                             │ │
│  │   • begin_passkey_registration / finish_passkey_registration       │ │
│  │   • begin_passkey_authentication / verify_passkey_assertion        │ │
│  │   • begin_totp_enrollment / finish_totp_enrollment                 │ │
│  │   • verify_totp / consume_recovery_code                            │ │
│  │   • generate_recovery_codes / hash_recovery_code                   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│           │                          │                       │           │
│           ▼                          ▼                       ▼           │
│   webauthn library             pyotp library         SecurityEvent log  │
│   (Yubico py_webauthn)         (RFC 6238 TOTP)       audit trail        │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          PostgreSQL                                       │
│   User (extended)                                                         │
│   UserPasskey            (new — one row per registered authenticator)    │
│   UserTotpSecret         (new — one row per user, if enrolled)           │
│   UserRecoveryCode       (new — one row per code, hashed)                │
│   UserMfaChallenge       (new — short-lived, in-DB or Redis-style table) │
└──────────────────────────────────────────────────────────────────────────┘
```

### Data flow — login with 2FA

```
1. Password POST /login/access-token → UserService.authenticate
       └─ if two_factor_enabled is False → return Token
       └─ else → MfaService.issue_challenge(user, "password") → returns
                  MfaChallenge { challenge_token, allowed_methods }

2. Frontend stores challenge_token in memory (NOT localStorage) and pushes
   user to /login/mfa.

3a. (Passkey) POST /login/mfa/passkey/options { challenge_token }
        → MfaService.begin_passkey_authentication → WebAuthn options
    POST /login/mfa/verify { challenge_token, method="passkey",
                              payload: AuthenticationResponse }
        → MfaService.verify_challenge → User → Token

3b. (TOTP) POST /login/mfa/verify { challenge_token, method="totp",
                                     payload: { code: "123456" } }
        → MfaService.verify_challenge

3c. (Recovery) POST /login/mfa/verify { challenge_token, method="recovery",
                                         payload: { code: "xxxx-xxxx" } }
        → MfaService.consume_recovery_code → Token
```

### Integration points

- **`auth`** — login routes augmented to branch on `two_factor_enabled`; Google OAuth callback follows the same branch.
- **`user_roles` / `desktop_auth`** — 2FA is enforced at the same chokepoint that issues web JWTs. Desktop OAuth (`/desktop-auth/...`) MUST also branch through MFA when the user has it enabled (it currently relies on a logged-in browser session, so the existing browser-side challenge covers it). Document this explicitly in `desktop_auth` afterwards.
- **`agent_credentials` / encryption** — TOTP secrets and WebAuthn private-key material we never see, but we DO encrypt the TOTP secret at rest using the existing `encrypt_field` / `decrypt_field` helpers from `backend/app/core/security.py`.
- **`events` / `SecurityEvent`** — every enroll/disable/challenge result is logged via the existing security-event table for the audit trail.
- **`realtime_events`** — no WebSocket integration needed for MVP; status reflected via REST refresh.

---

## 3. Data Models

All new models live under `backend/app/models/users/` and are re-exported from `app/models/__init__.py`.

### 3.1 `User` (existing — extended)

Add to `backend/app/models/users/user.py`:

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `two_factor_enabled` | `bool` | `False` | Master switch — true when at least one factor (passkey OR TOTP) is enrolled AND the user has confirmed enabling 2FA. |
| `two_factor_enrolled_at` | `datetime \| None` | `None` | Timestamp the first factor was confirmed. |
| `two_factor_last_used_at` | `datetime \| None` | `None` | Last successful second-factor verification (for the Security tab UI). |

`UserPublic` exposes:

| Field | Type |
|-------|------|
| `two_factor_enabled` | `bool` |
| `has_passkey` | `bool` (derived count > 0) |
| `has_totp` | `bool` (derived `UserTotpSecret` exists) |

### 3.2 `UserPasskey` (new — `user_passkey` table)

Stores one row per registered WebAuthn credential. Multiple per user allowed.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id`, `ON DELETE CASCADE`, indexed |
| `credential_id` | `bytes` (`BLOB`/`bytea`) | WebAuthn credential ID; **unique index** (raw bytes — store base64url string OR `bytea`; recommend `bytea`) |
| `public_key` | `bytes` (`BLOB`/`bytea`) | COSE public key — opaque blob |
| `sign_count` | `int` | Monotonic counter (anti-clone heuristic) |
| `transports` | `str` | JSON list of WebAuthn transports (`"usb"`, `"nfc"`, `"ble"`, `"internal"`, `"hybrid"`) |
| `aaguid` | `str \| None` | Authenticator AAGUID (best-effort device label) |
| `nickname` | `str` (max 64) | User-chosen label ("YubiKey 5", "iPhone Touch ID") |
| `device_type` | `str` | `"platform"` or `"cross-platform"` (resident-key hint) |
| `backed_up` | `bool` | Flag from `flags.bs` — useful to warn about syncing/cloud-backed keys |
| `created_at` | `datetime` | Default `now(UTC)` |
| `last_used_at` | `datetime \| None` | Updated after each successful assertion |

**Indexes:** `(user_id)`, `unique(credential_id)`.
**Cascade:** `ON DELETE CASCADE` from `user_id` — passkeys go away with the user.

### 3.3 `UserTotpSecret` (new — `user_totp_secret` table)

Single row per user (1:1). Created only after TOTP enrollment confirmation succeeds.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id`, `ON DELETE CASCADE`, **unique index** (1:1) |
| `secret_encrypted` | `str` (`Text`) | Base32 TOTP secret, **encrypted via `encrypt_field`** (Fernet). Never returned to API. |
| `algorithm` | `str` | Default `"SHA1"` (RFC 6238 standard); future-proofing only |
| `digits` | `int` | Default `6` |
| `period` | `int` | Default `30` |
| `created_at` | `datetime` | Default `now(UTC)` |
| `last_used_at` | `datetime \| None` | Updated after each successful TOTP verification |
| `last_used_step` | `int \| None` | Last accepted RFC-6238 time-step counter — used to reject replay within the validity window |

**Indexes:** `unique(user_id)`.

### 3.4 `UserRecoveryCode` (new — `user_recovery_code` table)

8 codes generated at first enrollment (or regeneration); each row tracks a single code's lifecycle.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id`, `ON DELETE CASCADE`, indexed |
| `code_hash` | `str` | Bcrypt hash of the code (same `get_password_hash`); plaintext shown ONCE on generation and never again |
| `used_at` | `datetime \| None` | Null until consumed |
| `created_at` | `datetime` | Default `now(UTC)` |
| `batch_id` | `UUID` | Identifies which `regenerate` batch produced this code; regeneration invalidates older batches |

**Indexes:** `(user_id, used_at)`.
**Cascade:** `ON DELETE CASCADE` from `user_id`.
**Lifecycle:** `created → used (terminal)` — codes are immutable and either used or not. Regeneration deletes all rows for the user and writes a fresh batch.

### 3.5 `UserMfaChallenge` (new — `user_mfa_challenge` table)

Short-lived row created when a user passes the first factor but still owes the second. Acts as a server-side handle that ties the WebAuthn challenge nonce to the user.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | PK |
| `user_id` | `UUID` | FK → `user.id`, `ON DELETE CASCADE`, indexed |
| `challenge_token` | `str` | URL-safe random token returned to client (256-bit) — **indexed unique** |
| `webauthn_challenge` | `bytes \| None` | WebAuthn assertion challenge nonce (for passkey method); null until `passkey/options` is called |
| `first_factor` | `str` | `"password"` or `"google_oauth"` |
| `attempts` | `int` | Counter — incremented on every failed verification; locked at 5 |
| `created_at` | `datetime` | Default `now(UTC)` |
| `expires_at` | `datetime` | Default `created_at + 5 minutes` |
| `consumed_at` | `datetime \| None` | Set once a successful verification has issued an access token |

**Indexes:** `unique(challenge_token)`, `(user_id, created_at)`.
**Lifecycle:** `pending → consumed (terminal)` or `expired`. A background cleanup task (or query-time filter) drops rows older than 24h.

### 3.6 Lifecycle states

```
                  ┌──────────────────────────────────────┐
                  │            User.two_factor_*         │
                  └──────────────────────────────────────┘
                  off → enrolling-passkey → enrolled (passkey)
                  off → enrolling-totp    → enrolled (totp)
                  enrolled → (regenerate recovery) → enrolled
                  enrolled → disable (requires factor) → off
```

---

## 4. Security Architecture

### 4.1 Encryption

- **TOTP secret** — encrypted with the existing Fernet cipher via `encrypt_field` from `backend/app/core/security.py` (the same approach used for AI credentials, OAuth tokens, and email server passwords). Never returned through any API; only `MfaService` ever decrypts.
- **Recovery codes** — stored as **bcrypt hashes** using the existing `get_password_hash` / `verify_password`. Plaintext is shown exactly once at generation.
- **WebAuthn keys** — only public keys are stored; the private key never leaves the authenticator. No encryption needed for that column.
- **Challenge tokens** — generated with `secrets.token_urlsafe(32)`, single-use, server-bound; comparison uses `secrets.compare_digest`.

### 4.2 Access control

- All `/api/v1/users/me/mfa/*` enrollment, listing, and disable endpoints require `CurrentUser`. No admin override for self-service flows.
- A user can list/delete only their own passkeys (filter by `user_id = current_user.id`).
- Superusers do NOT have a "disable 2FA on another user" endpoint in MVP — only documented as a future enhancement; the current escape hatch is the recovery-code flow.
- The Google OAuth callback issues the access token only after 2FA succeeds when the user has 2FA enabled. Same is true for the password-login path.
- Desktop OAuth (`/desktop-auth/authorize`) inherits the browser session, so 2FA is satisfied transitively — no extra work, but verify it explicitly during implementation.
- A2A access tokens (`backend/app/services/a2a/...`) and other long-lived programmatic tokens are **out of scope** for 2FA — those are pre-authenticated machine credentials; document this clearly.

### 4.3 Input validation & sanitization

- TOTP codes must match `^[0-9]{6}$`; reject anything else with `400`.
- Recovery codes are normalized: stripped, uppercased, dashes removed before bcrypt verification.
- WebAuthn payloads validated by the `webauthn` library (Yubico py_webauthn); RP-ID, origin, and challenge are server-checked.
- Passkey nickname max length 64; HTML-escape on display (frontend default).
- Challenge tokens validated for shape and existence before any work.

### 4.4 Rate limiting & lockout

- Per-challenge `attempts` counter caps at **5 failed verifications**; further attempts return `429 Too Many Requests` and force the user back to step 1.
- Per-user soft throttle on `POST /login/mfa/verify`: at most **10 verifications per 5 minutes** (in-memory token bucket keyed by `user_id`, or DB count — pick simplest). Logs `MFA_RATE_LIMITED` event.
- Recovery codes are single-use; consuming a code logs `MFA_RECOVERY_CODE_USED`.
- `UserMfaChallenge` rows expire after **5 minutes**; expired rows return `410 Gone`.

### 4.5 Sensitive data handling

- Never log challenge tokens, TOTP secrets, or recovery-code plaintext. Log only `user_id`, `event_type`, and severity in `SecurityEvent`.
- Never return TOTP secret on subsequent GETs — only during the one-shot enrollment finish response (alongside the QR PNG/otpauth URI).
- Recovery codes returned exactly once in plaintext (on generation/regeneration). The API response should call this out via a `regenerate_warning` flag — UI displays a "Save these codes now" modal.

---

## 5. Backend Implementation

### 5.1 New API Routes

All under `/api/v1`. Two new route files: `backend/app/api/routes/mfa.py` and additions to `backend/app/api/routes/login.py`.

#### 5.1.1 Login flow (modified — `routes/login.py`)

| Method | Path | Body | Returns | Notes |
|--------|------|------|---------|-------|
| `POST` | `/login/access-token` | `OAuth2PasswordRequestForm` | `Token` OR `MfaChallenge` | Returns `Token` if `two_factor_enabled=False`; otherwise returns `MfaChallenge` with HTTP 200 (status discriminated by response shape — see notes below) |
| `POST` | `/login/mfa/passkey/options` | `{ challenge_token }` | `WebAuthnAssertionOptions` | Loads challenge, generates `PublicKeyCredentialRequestOptions`, persists `webauthn_challenge` |
| `POST` | `/login/mfa/verify` | `MfaVerifyRequest` | `Token` | One of `method="passkey" \| "totp" \| "recovery"`, with corresponding `payload` |

`MfaChallenge` schema:
```
{
  challenge_token: str,
  expires_at: datetime,
  allowed_methods: list[str]   # subset of ["passkey", "totp", "recovery"]
}
```

**Response shape discrimination:** rather than collapsing both into a `Union` (cumbersome in OpenAPI client codegen), keep `POST /login/access-token` returning `LoginResponse = Token | MfaChallenge` as a discriminated union with a `kind` literal field (`"token"` vs `"mfa_challenge"`). The generated TS client gets `LoginResponse` and the frontend branches on `kind`.

#### 5.1.2 OAuth callback (modified — `routes/oauth.py`)

`POST /api/v1/auth/google/callback` returns the same discriminated `LoginResponse`. Same branch logic.

#### 5.1.3 MFA enrollment / management (new — `routes/mfa.py`)

All require `CurrentUser`.

| Method | Path | Body | Returns | Notes |
|--------|------|------|---------|-------|
| `GET`  | `/users/me/mfa/status` | — | `MfaStatus` | `{ enabled, has_passkey, has_totp, has_recovery_codes, last_used_at, passkey_count }` |
| `GET`  | `/users/me/mfa/passkeys` | — | `list[UserPasskeyPublic]` | Public schema omits `public_key` and raw `credential_id`; exposes nickname, transports, created/last-used |
| `POST` | `/users/me/mfa/passkeys/begin` | `{ nickname? }` | `WebAuthnRegistrationOptions` | Server-generated challenge; stores in a transient `UserMfaChallenge` (or `UserPasskeyChallenge` if we keep them separate) |
| `POST` | `/users/me/mfa/passkeys/finish` | `{ challenge_token, credential, nickname? }` | `UserPasskeyPublic` | Verifies attestation, persists `UserPasskey`, sets `User.two_factor_enabled=True` on first factor |
| `PATCH`| `/users/me/mfa/passkeys/{passkey_id}` | `{ nickname }` | `UserPasskeyPublic` | Rename |
| `DELETE`| `/users/me/mfa/passkeys/{passkey_id}` | — | `Message` | Refuses if it's the last factor and TOTP is also off; user must disable 2FA first or remove TOTP last |
| `POST` | `/users/me/mfa/totp/begin` | — | `TotpEnrollResponse` | Returns `{ secret_base32, otpauth_uri, qr_svg_data_uri }`; **does not persist secret yet** — caller must POST `/finish` with a valid code first |
| `POST` | `/users/me/mfa/totp/finish` | `{ secret_token, code }` | `Message` | Stores the encrypted secret only if the supplied code verifies; `secret_token` is an HMAC-signed handle returned by `/begin` so we don't trust the client to echo the raw secret back |
| `DELETE`| `/users/me/mfa/totp` | `{ password? \| totp_code? \| passkey_assertion? }` | `Message` | Requires a fresh factor proof (re-auth) |
| `GET`  | `/users/me/mfa/recovery-codes` | — | `RecoveryCodeStatus` | Returns `{ remaining_count, last_regenerated_at }` — never plaintext |
| `POST` | `/users/me/mfa/recovery-codes/regenerate` | `{ password? \| totp_code? \| passkey_assertion? }` | `RecoveryCodesPlaintext` | Invalidates previous batch, returns 8 fresh codes (one-shot) |
| `POST` | `/users/me/mfa/disable` | `{ password? \| totp_code? \| passkey_assertion? }` | `Message` | Wipes all factors + flag; requires a step-up re-auth (NOT just the access token) |

**Dependencies for all `/users/me/mfa/*` routes:** `SessionDep`, `CurrentUser`.

#### 5.1.4 Step-up re-auth

Mutations that disable or weaken 2FA (delete factor, disable 2FA, regenerate recovery codes) MUST verify a fresh factor — they accept one of `password`, `totp_code`, or a fresh `passkey_assertion` (with its own challenge round-trip). This is a generic helper `MfaService.require_recent_factor(user, proof)` reused across routes.

### 5.2 Service Layer

#### `MfaService` — `backend/app/services/users/mfa_service.py`

```text
class MfaService:
    # Challenge lifecycle (post-first-factor)
    issue_challenge(session, user, first_factor) -> UserMfaChallenge
    get_challenge(session, challenge_token) -> UserMfaChallenge   # raises if expired / consumed / locked
    consume_challenge(session, challenge) -> None                  # marks consumed
    verify_challenge(session, challenge_token, method, payload)
        -> User                                                    # raises ValueError on failure

    # WebAuthn (registration)
    begin_passkey_registration(session, user, nickname) -> dict    # PublicKeyCredentialCreationOptions
    finish_passkey_registration(session, user, registration, nickname) -> UserPasskey

    # WebAuthn (authentication / login)
    begin_passkey_authentication(session, challenge) -> dict       # PublicKeyCredentialRequestOptions
    verify_passkey_assertion(session, challenge, assertion) -> UserPasskey
        # updates sign_count, last_used_at

    # TOTP
    begin_totp_enrollment(user) -> dict
        # generates secret, returns { secret_token, otpauth_uri, qr_svg_data_uri }
        # secret encoded as HMAC-signed JWT-ish handle so we don't keep server-side
        # state for un-confirmed enrollments
    finish_totp_enrollment(session, user, secret_token, code) -> UserTotpSecret
    verify_totp(session, user, code) -> bool
        # uses pyotp.TOTP.verify with valid_window=1; rejects last_used_step replay

    # Recovery codes
    generate_recovery_codes(session, user) -> list[str]
        # deletes existing batch, creates 8 new rows with bcrypt hashes,
        # returns plaintext (called by enroll-finish flows AND regenerate)
    consume_recovery_code(session, user, code) -> bool

    # Step-up re-auth
    require_recent_factor(session, user, proof) -> None
        # raises ValueError("step_up_required") if proof missing / invalid

    # Audit
    _log_event(session, user, event_type, severity, details) -> None
        # thin wrapper over existing SecurityEvent insert
```

**Library choices:**

- **WebAuthn** — `webauthn` (Yubico `py_webauthn`, pure-Python, well-maintained, FIDO2 conformant). Configure relying-party (RP) ID from `settings.FRONTEND_HOST` (host portion only) and `origin` from the full `FRONTEND_HOST` URL.
- **TOTP** — `pyotp`.
- **QR** — `qrcode[pil]` to emit an SVG data URI returned alongside `otpauth://...` for clients that prefer raw URIs.

Add to `backend/pyproject.toml`:
```
webauthn>=2.3.0
pyotp>=2.9.0
qrcode[pil]>=7.4.2
```

**Error pattern:** All `MfaService` methods raise `ValueError` (consistent with `UserService`); the routes translate to specific HTTP errors:
- `ValueError("invalid_code")` → `400`
- `ValueError("challenge_expired")` → `410`
- `ValueError("challenge_not_found")` → `404`
- `ValueError("attempt_limit_exceeded")` → `429`
- `ValueError("last_factor_protected")` → `409`
- `ValueError("step_up_required")` → `401`

#### `UserService` — light additions

- `disable_all_factors(session, user) -> None` — wipes passkeys, TOTP, recovery codes, flips `two_factor_enabled=False`. Called by `POST /mfa/disable`.

#### `AuthService` (Google OAuth) — `backend/app/services/users/auth_service.py`

- Existing `authenticate_with_google()` returns either `Token` (no 2FA) OR an `MfaChallenge` — refactor to return a single discriminated `LoginResult` object. Same pattern as the password-login route.

### 5.3 Background Tasks

No periodic cron needed for MVP. **Optional cleanup:** add an APScheduler job (`backend/app/services/users/mfa_cleanup_service.py`) running once an hour that deletes `UserMfaChallenge` rows where `expires_at < now() - 24h`. Plug into the existing scheduler bootstrap (see `agent_schedulers` for the pattern). Idempotent (just `DELETE WHERE ...`); no recovery needed if it fails — next run picks up.

### 5.4 Configuration (`backend/app/core/config.py`)

Add to `Settings`:

| Name | Default | Purpose |
|------|---------|---------|
| `MFA_CHALLENGE_TTL_SECONDS` | `300` | Lifetime of an `UserMfaChallenge` row |
| `MFA_MAX_ATTEMPTS_PER_CHALLENGE` | `5` | Per-challenge attempt cap |
| `MFA_RECOVERY_CODE_COUNT` | `8` | How many codes to generate |
| `MFA_RECOVERY_CODE_LENGTH` | `10` | Length of one code (xxxx-xxxx) |
| `MFA_WEBAUTHN_RP_NAME` | `"Cinna"` | Relying-party display name |
| `MFA_WEBAUTHN_RP_ID` | `None` | Override RP ID; falls back to `urlparse(FRONTEND_HOST).hostname` |
| `MFA_TOTP_ISSUER` | `"Cinna"` | Otpauth URI issuer |

---

## 6. Frontend Implementation

### 6.1 UI Components

#### Routes (TanStack Router)

- **`frontend/src/routes/login/mfa.tsx`** — new public route hosting the 2FA challenge UI (passkey button + TOTP form + recovery-code link). Reached via `navigate({ to: "/login/mfa" })` after step-1 returns an `MfaChallenge`. The `challenge_token` is held in-memory only (Zustand-less; just route loader state or a thin `MfaChallengeContext`).
- **`frontend/src/routes/_layout/settings.tsx`** — add a new tab `"security"` titled "Security" between `"my-profile"` and `"interface"`. Tab content rendered by `<SecurityTab />`.

#### Components — `frontend/src/components/UserSettings/Security/`

```
Security/
  ├── SecurityTab.tsx                  ← orchestrator; reads /mfa/status
  ├── PasskeySection.tsx
  │     ├── PasskeyList.tsx           ← lists registered passkeys w/ rename + delete
  │     └── AddPasskeyDialog.tsx      ← runs WebAuthn create() ceremony
  ├── TotpSection.tsx
  │     ├── EnrollTotpDialog.tsx      ← shows QR + secret + code-confirm field
  │     └── DisableTotpDialog.tsx
  ├── RecoveryCodesSection.tsx
  │     ├── RecoveryCodesDialog.tsx   ← one-shot plaintext display + copy/print
  │     └── RegenerateRecoveryDialog.tsx
  ├── DisableTwoFactorDialog.tsx       ← global "Turn off 2FA" with step-up factor
  └── SecurityActivityList.tsx         ← optional: last 10 SecurityEvent rows (MFA-only)
```

Re-use the existing dialog primitives from `frontend/src/components/ui/dialog.tsx`, and follow the card pattern used by `MailServerSettings.tsx` / `OAuthAccounts.tsx`.

#### Components — `frontend/src/components/Auth/`

```
TwoFactorChallenge.tsx           ← shown on /login/mfa
  ├── PasskeyButton.tsx          ← invokes WebAuthn navigator.credentials.get()
  ├── TotpForm.tsx               ← 6-digit numeric input with auto-submit
  └── RecoveryCodeForm.tsx       ← single text field
```

#### Form validation (react-hook-form + zod)

- **TOTP code:** `z.string().regex(/^[0-9]{6}$/)`
- **Recovery code:** `z.string().min(8).max(32).transform(s => s.toUpperCase().replace(/[\s-]/g, ""))`
- **Passkey nickname:** `z.string().min(1).max(64)`

#### WebAuthn browser glue

A thin helper file `frontend/src/utils/webauthn.ts` wrapping `navigator.credentials.create()` / `.get()` and using the `@simplewebauthn/browser` library (matches Yubico's Python server). Add to `frontend/package.json`:

```
@simplewebauthn/browser@^11
```

Helper exposes:
- `startRegistration(options) -> RegistrationResponseJSON`
- `startAuthentication(options) -> AuthenticationResponseJSON`
- `isWebAuthnSupported()` — used to grey out passkey buttons on unsupported browsers

### 6.2 State Management

React Query keys:
- `["mfa", "status"]` — invalidated after any factor add/remove
- `["mfa", "passkeys"]` — list of registered passkeys
- `["mfa", "recovery"]` — remaining count

Mutations:
- `useEnrollPasskeyMutation()` — chains `begin → browser create → finish`; invalidates `["mfa", "status"]` and `["mfa", "passkeys"]`
- `useEnrollTotpMutation()` — chains `begin → user types code → finish`
- `useVerifyMfaMutation()` — used on the challenge page; on success, stores `access_token` in `localStorage` and navigates to post-login target via the existing `navigateToPostAuthTarget` helper from `useAuth`
- `useRegenerateRecoveryCodesMutation()` — returns `RecoveryCodesPlaintext`
- `useDisableTwoFactorMutation()`

**Challenge state:** held in a small `MfaChallengeContext` (provider mounted by `/login/mfa.tsx`) — `{ challenge_token, allowed_methods, expires_at }`. Never persisted to `localStorage` or `sessionStorage`.

### 6.3 User Flows

**Enable passkey (first factor):**
1. Settings → Security → "Add passkey" → opens `AddPasskeyDialog`.
2. User enters a nickname; client posts `/mfa/passkeys/begin` → receives WebAuthn options.
3. Browser shows native authenticator prompt (Touch ID, Windows Hello, YubiKey tap).
4. Client posts `/mfa/passkeys/finish` with the assertion.
5. **If this is the FIRST factor**, server immediately generates 8 recovery codes and returns them in the response. UI pops `RecoveryCodesDialog` and forces the user to confirm "I've saved these codes" before closing.
6. `["mfa", "status"]` invalidated; SecurityTab refreshes with the new passkey row.

**Enable TOTP:**
1. Settings → Security → "Set up authenticator app" → opens `EnrollTotpDialog`.
2. Client posts `/mfa/totp/begin` → receives QR data URI + otpauth URI + signed `secret_token`.
3. UI shows QR + manual-entry secret + 6-digit input.
4. User scans the QR with Google Authenticator / 1Password / Authy and enters the displayed code.
5. Client posts `/mfa/totp/finish { secret_token, code }`.
6. Same first-factor logic — if this enables 2FA for the first time, recovery codes are shown.

**Login with 2FA:**
1. User submits password → backend returns `{ kind: "mfa_challenge", ... }`.
2. Frontend navigates to `/login/mfa` and shows the challenge UI.
3. **Preferred path:** passkey button — calls `startAuthentication()` and posts `/login/mfa/verify`.
4. **Fallback:** TOTP form, or "Use a recovery code" link.
5. On success, store the returned `access_token` and continue with the existing post-login redirect chain.

**Disable 2FA:**
1. Settings → Security → "Turn off two-factor authentication" → opens `DisableTwoFactorDialog`.
2. Dialog requires a fresh factor — user picks passkey OR TOTP OR password (password ALWAYS allowed as the password-login step-up).
3. On confirm, calls `POST /users/me/mfa/disable` with the proof.
4. All factors wiped; SecurityTab returns to the empty state.

**Empty / loading / error states:**
- SecurityTab empty state: card with explainer copy, "Add a passkey" primary button, "Set up authenticator app" secondary button, and a "Learn more about 2FA" link.
- Loading: skeleton rows in the passkey list.
- WebAuthn unsupported browser: passkey button greyed out with tooltip "Your browser doesn't support passkeys; try Chrome, Safari, Firefox, or Edge".
- WebAuthn user cancellation (`NotAllowedError`): non-destructive toast "Cancelled — no changes made".

---

## 7. Database Migrations

Single Alembic migration: `backend/app/alembic/versions/XXXX_add_user_2fa_tables.py`.

**Upgrade:**

```text
- ALTER TABLE user
    ADD COLUMN two_factor_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN two_factor_enrolled_at TIMESTAMP WITH TIME ZONE NULL,
    ADD COLUMN two_factor_last_used_at TIMESTAMP WITH TIME ZONE NULL;

- CREATE TABLE user_passkey (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    credential_id BYTEA NOT NULL UNIQUE,
    public_key BYTEA NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports TEXT NOT NULL DEFAULT '[]',
    aaguid VARCHAR(64) NULL,
    nickname VARCHAR(64) NOT NULL,
    device_type VARCHAR(32) NOT NULL,
    backed_up BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE NULL
  );
  CREATE INDEX ix_user_passkey_user_id ON user_passkey (user_id);
  CREATE UNIQUE INDEX ix_user_passkey_credential_id ON user_passkey (credential_id);

- CREATE TABLE user_totp_secret (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL UNIQUE REFERENCES "user"(id) ON DELETE CASCADE,
    secret_encrypted TEXT NOT NULL,
    algorithm VARCHAR(16) NOT NULL DEFAULT 'SHA1',
    digits INTEGER NOT NULL DEFAULT 6,
    period INTEGER NOT NULL DEFAULT 30,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE NULL,
    last_used_step BIGINT NULL
  );

- CREATE TABLE user_recovery_code (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    batch_id UUID NOT NULL
  );
  CREATE INDEX ix_user_recovery_code_user_used ON user_recovery_code (user_id, used_at);

- CREATE TABLE user_mfa_challenge (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    challenge_token VARCHAR(128) NOT NULL,
    webauthn_challenge BYTEA NULL,
    first_factor VARCHAR(32) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consumed_at TIMESTAMP WITH TIME ZONE NULL
  );
  CREATE UNIQUE INDEX ix_user_mfa_challenge_token ON user_mfa_challenge (challenge_token);
  CREATE INDEX ix_user_mfa_challenge_user_created ON user_mfa_challenge (user_id, created_at);
```

**Downgrade:** Drop the four new tables in reverse order, then `ALTER TABLE user DROP COLUMN ...` (3 columns).

**Backfill:** none — all existing users default to `two_factor_enabled=False`.

---

## 8. Knowledge Repository Format

Not applicable — this feature does not use the knowledge-source system.

---

## 9. Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| User enables 2FA but only enrolls TOTP, then loses phone | Recovery codes (issued at first enrollment) let them in; if they also lost those, admin can offer manual recovery via a database operation (out of MVP scope to expose a UI). |
| User enrolls passkey on a synced platform (iCloud Keychain) — `backed_up=True` | Stored as such; UI shows "Synced" badge on the passkey row so the user understands the key is portable. |
| Passkey `sign_count` regresses (cloned device) | Verification still succeeds, but a `MFA_SIGN_COUNT_REGRESSION` `SecurityEvent` row at severity `"high"` is written for monitoring. |
| TOTP clock skew | `pyotp.TOTP.verify(code, valid_window=1)` allows ±1 step (30s). Wider windows rejected. |
| TOTP replay within validity window | `last_used_step` tracking — reject the same step twice. |
| Challenge expired (5min) | `410 Gone`, user redirected to `/login`. |
| Too many failed attempts on one challenge | `429 Too Many Requests`, challenge invalidated; user redirected to `/login`. |
| WebAuthn assertion fails (`origin` mismatch) | `400`, log `MFA_PASSKEY_INVALID_ORIGIN` at severity `"high"`. |
| Deleting the last passkey while TOTP is also off | `409 Conflict` — user must disable 2FA explicitly. |
| Disabling 2FA without step-up factor | `401 Unauthorized` with `detail="step_up_required"`. |
| Concurrent challenges (user opens two tabs) | Both rows valid until one is consumed; first-to-consume wins, the other receives `404` on next attempt. |
| OAuth user with no password who has 2FA | Must use TOTP/passkey on every Google login; step-up re-auth flows allow `totp_code` or `passkey_assertion` as proof (password proof not available). |
| Email recovery flow | Existing `password-recovery` flow continues to bypass 2FA (since the user is presumed locked out of their account) — **but** if `two_factor_enabled=True`, the new password alone is NOT enough; UI shows "After reset you'll still need your authenticator". Document this explicitly. Out of MVP: an emergency disable-2FA email path. |
| User deletes own account | `ON DELETE CASCADE` cleans up everything. |

**Generic API error model:** route layer translates `ValueError(code)` → `HTTPException(status, detail={"code": code, "message": user_facing})`. Frontend keys on `error.detail.code` for branching.

---

## 10. UI/UX Considerations

- **Status indicators:** SecurityTab shows a single status header — "Two-factor authentication is **ON** / **OFF**" with green/grey badge. Each card (Passkeys, Authenticator App, Recovery Codes) carries an independent status pill.
- **Empty states:** Passkey card empty → illustration + explainer + "Add passkey" CTA. TOTP card empty → "Set up authenticator app" CTA.
- **One-shot disclosure modals:**
  - Recovery codes shown in a modal with: monospace grid (4×2), per-code copy buttons, "Copy all", "Download .txt", "Print". Closes only when user clicks "I've saved these codes".
  - TOTP secret: same pattern — QR + base32 secret + copy.
- **Help text:** Inline `<Tooltip>` on each card explaining the tradeoff (passkeys = phishing-resistant; TOTP = works offline; recovery codes = single-use). Link to a future docs page.
- **Onboarding nudge:** On the dashboard (post-login), show a one-time dismissable banner suggesting 2FA enrollment to users who don't have it. Stored as a per-user dismissal in `userdashboard.dismissed_hints` (existing pattern from "Rotating Hints").
- **Accessibility:**
  - All dialogs use existing shadcn `Dialog` (focus trap, ESC to close, ARIA roles built-in).
  - TOTP input uses `inputMode="numeric"` and `autocomplete="one-time-code"` so mobile keyboards and password managers offer codes.
  - Passkey button labelled `<button aria-label="Sign in with passkey">`.
  - Color is never the sole status signal — pills carry both color and text.
- **Mobile:** The challenge page is fully responsive; passkey button triggers the platform's native authenticator UI on iOS Safari / Android Chrome.

---

## 11. Integration Points

- **Auth (`backend/app/api/routes/login.py`, `oauth.py`)** — return `LoginResponse` discriminated union. Both password and Google paths converge into `MfaService.issue_challenge` when `two_factor_enabled=True`.
- **Encryption (`backend/app/core/security.py`)** — TOTP secrets use the existing Fernet `encrypt_field` / `decrypt_field` helpers — no new key, no new salt.
- **SecurityEvent (`backend/app/models/events/security_event.py`)** — extend the `event_type` constants module with: `MFA_ENROLLED`, `MFA_DISABLED`, `MFA_CHALLENGE_ISSUED`, `MFA_CHALLENGE_SUCCESS`, `MFA_CHALLENGE_FAILED`, `MFA_RECOVERY_CODE_USED`, `MFA_RATE_LIMITED`, `MFA_SIGN_COUNT_REGRESSION`, `MFA_PASSKEY_INVALID_ORIGIN`. All logged via the existing security-events service.
- **Desktop OAuth (`backend/app/api/routes/desktop_auth.py`)** — verify that the consent flow re-uses the browser session, so 2FA is satisfied transitively. No new code there, but call this out in the desktop-auth tech doc afterwards.
- **A2A access tokens** — explicitly out of scope; document in `a2a_access_tokens.md` that the new 2FA toggle does not apply to pre-issued machine tokens (they are scoped credentials, not user sessions).
- **OpenAPI client regeneration** — after the new routes ship, run `bash scripts/generate-client.sh` to regenerate `frontend/src/client/`. The frontend MUST consume the regenerated `LoginService`, `MfaService` (named after the route's `tags` parameter — pick `tags=["mfa"]`).
- **Workspaces** — 2FA is user-level, not workspace-level; nothing to wire into the workspace machinery.
- **Bundles / Installs / Sharing** — guest-share tokens and bundle install flows are unaffected (guest sessions carry a `chat-guest` role JWT and never traverse the MFA branch).

---

## 12. Future Enhancements (Out of Scope)

- **Trusted devices / "Remember this browser for 30 days"** — a `UserTrustedDevice` table keyed by a cookie hash, with auto-expiry and a Settings tab to revoke individual devices.
- **SMS / email OTP** — explicitly excluded; passkeys + TOTP cover modern threat models and avoid the SIM-swap risk of SMS.
- **Admin "disable 2FA on another user"** — escape hatch for support; gated by admin role + audit logging. Out of MVP to keep the threat model tight.
- **Required-2FA workspace policy** — workspace admins force all members to enroll 2FA within N days. Future once workspaces gain the multi-tenant-admin surface.
- **WebAuthn conditional UI (autofill of passkeys at the login page)** — supported by `@simplewebauthn/browser` but excluded from MVP to keep the login page logic simple.
- **Passkey-only login (no password)** — the next step after passkey enrollment is widespread; not MVP.
- **Hardware-attestation-required policies** — restrict the kinds of authenticators an admin will accept (e.g., FIDO Level 2 attested only). Future once the admin policy surface exists.

---

## 13. Summary Checklist

### Backend tasks

- [ ] Add three columns to `User` (`two_factor_enabled`, `two_factor_enrolled_at`, `two_factor_last_used_at`) in `backend/app/models/users/user.py`.
- [ ] Create `UserPasskey`, `UserTotpSecret`, `UserRecoveryCode`, `UserMfaChallenge` models under `backend/app/models/users/` and re-export from `app/models/__init__.py`.
- [ ] Add corresponding Public / Create / Update schemas (`UserPasskeyPublic`, `MfaStatus`, `MfaChallenge`, `MfaVerifyRequest`, `TotpEnrollResponse`, `RecoveryCodesPlaintext`, `LoginResponse` discriminated union).
- [ ] Write Alembic migration `add_user_2fa_tables.py` — three new `User` columns + four new tables with indexes and FKs.
- [ ] Add MFA settings to `backend/app/core/config.py` (`MFA_*`).
- [ ] Add dependencies to `backend/pyproject.toml`: `webauthn`, `pyotp`, `qrcode[pil]`.
- [ ] Create `backend/app/services/users/mfa_service.py` implementing all methods listed in §5.2.
- [ ] Extend `backend/app/models/events/security_event.py` with new `MFA_*` event-type constants.
- [ ] Create `backend/app/api/routes/mfa.py` with all `/users/me/mfa/*` endpoints (tags `["mfa"]`).
- [ ] Modify `backend/app/api/routes/login.py` `POST /login/access-token` to return `LoginResponse` (Token | MfaChallenge).
- [ ] Add `POST /login/mfa/passkey/options` and `POST /login/mfa/verify` to `routes/login.py`.
- [ ] Modify `backend/app/api/routes/oauth.py` Google callback to branch through the MFA challenge when `two_factor_enabled=True`.
- [ ] Refactor `AuthService` to return a discriminated `LoginResult` instead of bare `Token` so callers can branch.
- [ ] Register `mfa` router in `backend/app/api/main.py`.
- [ ] Add the optional MFA-challenge cleanup APScheduler job under `backend/app/services/users/mfa_cleanup_service.py` and wire into the existing scheduler bootstrap.
- [ ] Update `UserPublic` to expose `two_factor_enabled`, `has_passkey`, `has_totp`.

### Frontend tasks

- [ ] Run `bash scripts/generate-client.sh` after backend lands; ensure `MfaService` is generated.
- [ ] Add `@simplewebauthn/browser` to `frontend/package.json`.
- [ ] Create `frontend/src/utils/webauthn.ts` thin wrapper (`startRegistration`, `startAuthentication`, `isWebAuthnSupported`).
- [ ] Create `frontend/src/routes/login/mfa.tsx` (public route) hosting `<TwoFactorChallenge />`.
- [ ] Update `useAuth` / `loginMutation` to branch on `LoginResponse.kind` — on `mfa_challenge`, navigate to `/login/mfa` carrying the challenge in `MfaChallengeContext`.
- [ ] Update Google OAuth callback handling in `useAuth` (`GoogleLoginButton.tsx`) for the same branch.
- [ ] Add a `"security"` tab to `frontend/src/routes/_layout/settings.tsx`.
- [ ] Create `frontend/src/components/UserSettings/Security/` directory with all components listed in §6.1.
- [ ] Create `frontend/src/components/Auth/TwoFactorChallenge.tsx`, `PasskeyButton.tsx`, `TotpForm.tsx`, `RecoveryCodeForm.tsx`.
- [ ] React Query hooks: `useMfaStatus`, `useMfaPasskeys`, `useEnrollPasskeyMutation`, `useEnrollTotpMutation`, `useDisableTwoFactorMutation`, `useRegenerateRecoveryCodesMutation`, `useVerifyMfaMutation`.
- [ ] Dashboard nudge banner for un-enrolled users (one-time, dismissable).

### Testing & validation tasks

- [ ] Password login with `two_factor_enabled=False` continues to return `Token` (regression test).
- [ ] Password login with `two_factor_enabled=True` returns an `MfaChallenge` and never an access token until step 2.
- [ ] Google OAuth login follows the same branch.
- [ ] Enrolling a passkey turns 2FA on, issues recovery codes, and reflects in `/users/me`.
- [ ] Enrolling TOTP requires a valid 6-digit code; wrong codes during enroll do NOT persist a secret.
- [ ] TOTP verification accepts current step and ±1 step but rejects replay of `last_used_step`.
- [ ] WebAuthn assertion succeeds for an enrolled passkey and fails for an unknown credential ID.
- [ ] WebAuthn assertion increments `sign_count` and updates `last_used_at`.
- [ ] Recovery code is consumed (one-shot) and unusable on second attempt.
- [ ] Regenerating recovery codes invalidates the previous batch.
- [ ] Challenge expires after 5 minutes (`410 Gone`).
- [ ] 5 failed verifications lock the challenge (`429`).
- [ ] Deleting the last factor is blocked while 2FA is still enabled.
- [ ] Disabling 2FA requires a step-up factor.
- [ ] All MFA endpoints require `CurrentUser` (no anonymous access).
- [ ] `SecurityEvent` rows are written for: enroll, disable, challenge success, challenge fail, recovery-code use, rate-limit hit, sign-count regression.
- [ ] Frontend: passkey button greyed out on unsupported browsers.
- [ ] Frontend: cancelling the WebAuthn dialog (`NotAllowedError`) surfaces a non-destructive toast.
- [ ] Frontend: recovery-codes modal cannot be dismissed without confirmation.
- [ ] Frontend: TOTP input auto-submits at 6 digits and exposes `autocomplete="one-time-code"`.
- [ ] Desktop OAuth flow still works for 2FA-enabled users (browser session already MFA-verified).
- [ ] Account deletion cascades to all `user_passkey`, `user_totp_secret`, `user_recovery_code`, `user_mfa_challenge` rows.
