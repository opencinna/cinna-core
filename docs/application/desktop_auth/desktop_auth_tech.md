# Desktop App Authentication — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/desktop_auth/__init__.py` — Re-exports all desktop auth models
- `backend/app/models/desktop_auth/desktop_oauth_client.py` — DesktopOAuthClient (table), DesktopOAuthClientCreate, DesktopOAuthClientPublic
- `backend/app/models/desktop_auth/desktop_refresh_token.py` — DesktopRefreshToken (table)
- `backend/app/models/desktop_auth/desktop_auth_code.py` — DesktopAuthCode (table)
- `backend/app/models/desktop_auth/desktop_auth_request.py` — DesktopAuthRequest (table) — pending consent requests

### Backend — Routes

- `backend/app/api/routes/desktop_auth.py` — All OAuth endpoints under `/desktop-auth` prefix; also defines the shared request/response models (`ConsentRequest`, `ConsentResponse`, `TokenRequest`, `TokenResponse`, `UserInfoResponse`, `RevokeRequest`) and the `_parse_token_request` helper reused by the app surface
- `backend/app/api/routes/app_auth.py` — **Parallel mobile surface** under `/app-auth` prefix (tag `app-auth`). Mirrors every desktop endpoint but delegates to the same `DesktopAuthService` and reuses the desktop route's shared models/helpers; the only behavioural difference is that `authorize` redirects to `/app-auth/consent`. No new tables — writes to the same `desktop_*` store.
- `backend/app/main.py` — `/.well-known/cinna-desktop` and `/.well-known/cinna-app` discovery endpoints registered at app level (not under `/api/v1`)

### Backend — Services

- `backend/app/services/desktop_auth/desktop_auth_service.py` — DesktopAuthService: consent flow, client management, authorization code, token exchange, refresh rotation, revocation, cleanup
- `backend/app/services/desktop_auth/desktop_auth_crypto.py` — Crypto helpers: ID/token generation, SHA-256 hashing, PKCE S256 verification
- `backend/app/services/desktop_auth/desktop_auth_scheduler.py` — Background cleanup scheduler (every 15 minutes)

### Backend — Configuration

- `backend/app/core/config.py` — `DESKTOP_AUTH_ENABLED`, `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES`, `DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS`, `DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS` (default 60; controls the rotation reuse-grace window), `APP_AUTH_ENABLED` (mobile surface toggle; token lifetimes are shared with desktop)

### Backend — Migrations

- `backend/app/alembic/versions/d3e4f5a6b7c8_add_desktop_auth_tables.py` — Creates desktop_oauth_client, desktop_refresh_token, desktop_auth_code tables
- `backend/app/alembic/versions/d7e34bcff709_add_desktop_auth_request_table.py` — Adds desktop_auth_request table (consent-page nonce store)
- `backend/app/alembic/versions/e8f1a2b3c4d5_add_revoked_at_to_desktop_refresh_token.py` — Adds nullable `revoked_at` column to desktop_refresh_token (down_revision: ab55mcpprovider01); legacy NULL rows fall through to genuine-replay behaviour

### Backend — Tests

- `backend/tests/api/desktop_auth/test_desktop_auth.py` — Scenario-based integration tests covering the full consent flow, redirect-URI validation (incl. native mobile schemes + env gating), and `client_kind` metadata
- `backend/tests/api/app_auth/test_app_auth.py` — Parallel-surface tests: `/.well-known/cinna-app` discovery, full mobile PKCE flow, `client_kind="mobile"` metadata, redirect validation, refresh rotation, and cross-surface token interoperability (app token works on `/desktop-auth/userinfo`)
- `backend/tests/utils/desktop_auth.py` — Test helpers: PKCE pair generation, consent flow steps, token exchange

### Frontend

- `frontend/src/components/Auth/NativeAuthConsentPage.tsx` — Shared consent screen component for both native surfaces; parameterized by the service endpoints (`getRequest`/`submitConsent`) and renders "Cinna Mobile" vs "Cinna Desktop" copy from `client_kind`
- `frontend/src/routes/desktop-auth/consent.tsx` — Public SPA consent page at `/desktop-auth/consent?request={nonce}` (thin wrapper around `NativeAuthConsentPage` wired to `DesktopAuthService`)
- `frontend/src/routes/app-auth/consent.tsx` — Public SPA consent page at `/app-auth/consent?request={nonce}` (wrapper wired to `AppAuthService`)
- `frontend/src/components/UserSettings/DesktopSessionsCard.tsx` — Connected devices list + disconnect dialog
- `frontend/src/routes/_layout/settings.tsx` — DesktopSessionsCard added to Security tab
- `frontend/src/client/sdk.gen.ts` — `DesktopAuthService` + `AppAuthService` (auto-generated)

## Database Schema

### desktop_oauth_client

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| client_id | VARCHAR(64) | unique, indexed (ix_desktop_oauth_client_client_id) |
| user_id | UUID | FK -> user.id CASCADE, indexed (ix_desktop_oauth_client_user_id) |
| device_name | VARCHAR(200) | not null |
| platform | VARCHAR(50) | nullable |
| app_version | VARCHAR(50) | nullable |
| is_revoked | BOOLEAN | default false |
| last_used_at | TIMESTAMP WITH TZ | nullable |
| created_at | TIMESTAMP WITH TZ | default now |

### desktop_refresh_token

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| client_id | UUID | FK -> desktop_oauth_client.id CASCADE, indexed (ix_desktop_refresh_token_client_id) |
| user_id | UUID | FK -> user.id CASCADE |
| token_hash | VARCHAR | unique, indexed (ix_desktop_refresh_token_hash) |
| token_family | UUID | not null, indexed (ix_desktop_refresh_token_family) |
| is_revoked | BOOLEAN | default false |
| revoked_at | TIMESTAMP WITH TZ | nullable; stamped when a token is rotated out (normal rotation path). NULL for hard-revoked tokens (client disconnect / family revocation), so those rows can never qualify for grace re-rotation |
| expires_at | TIMESTAMP WITH TZ | not null |
| created_at | TIMESTAMP WITH TZ | default now |

### desktop_auth_code

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| code_hash | VARCHAR | unique, indexed (ix_desktop_auth_code_hash) |
| user_id | UUID | FK -> user.id CASCADE |
| client_id | VARCHAR(64) | not null |
| code_challenge | VARCHAR(128) | not null |
| redirect_uri | VARCHAR(255) | not null |
| is_used | BOOLEAN | default false |
| expires_at | TIMESTAMP WITH TZ | not null (5-minute TTL) |
| created_at | TIMESTAMP WITH TZ | default now |

### desktop_auth_request

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| nonce_hash | VARCHAR | unique, indexed (ix_desktop_auth_request_nonce_hash) |
| device_name | VARCHAR(200) | nullable |
| platform | VARCHAR(50) | nullable |
| app_version | VARCHAR(50) | nullable |
| client_id | VARCHAR(64) | nullable (null = lazy registration) |
| code_challenge | VARCHAR(128) | not null |
| redirect_uri | VARCHAR(255) | not null |
| state | VARCHAR(255) | not null |
| is_used | BOOLEAN | default false |
| expires_at | TIMESTAMP WITH TZ | not null (5-minute TTL), indexed (ix_desktop_auth_request_expires_at) |
| created_at | TIMESTAMP WITH TZ | default now |

## API Endpoints

### Discovery (root level, no auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/.well-known/cinna-desktop` | Instance metadata: `instance_name`, `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `version`, `desktop_auth_enabled` — field names follow RFC 8414 (OAuth 2.0 Authorization Server Metadata) |
| GET | `/.well-known/cinna-app` | Same shape as `cinna-desktop` but `authorization_endpoint`/`token_endpoint`/`userinfo_endpoint` point at `/api/v1/app-auth/*`, plus `app_auth_enabled`. Used by Cinna Mobile for instance discovery |

### OAuth Flow (under `/api/v1/desktop-auth`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/clients` | CurrentUser | List user's active clients |
| DELETE | `/clients/{client_id}` | CurrentUser | Revoke client + all its tokens |
| GET | `/authorize` | None (public) | Store consent request, redirect to SPA consent page |
| GET | `/requests/{nonce}` | None (public) | Return display metadata for a pending consent request |
| POST | `/consent` | CurrentUser | Approve or deny a pending consent request; returns redirect_to URL |
| POST | `/token` | None (public) | Exchange code or refresh token for token pair (includes client_id). Accepts both `application/x-www-form-urlencoded` (OAuth 2.0 RFC 6749 §3.2) and `application/json` request bodies |
| GET | `/userinfo` | CurrentUser | Return `{sub, email, full_name, username}` for the bearer token holder |
| POST | `/revoke` | CurrentUser | Revoke client or specific refresh token |

Note: `POST /clients` (explicit client registration) has been removed. Clients are created lazily on first consent approval.

### OAuth Flow (under `/api/v1/app-auth`)

The same eight endpoints exist under `/api/v1/app-auth` (tag `app-auth`) with identical contracts — they delegate to the same `DesktopAuthService` and share the `desktop_*` tables. The only difference: `GET /app-auth/authorize` redirects the browser to `/app-auth/consent` instead of `/desktop-auth/consent`. Because storage is shared, a token minted on either surface is accepted by either `/userinfo`, and a client registered through one is listed by the other.

## Services & Key Methods

### DesktopAuthService (`backend/app/services/desktop_auth/desktop_auth_service.py`)

All methods are `@staticmethod`:

**Client management:**
- `list_clients(session, user_id) -> list[DesktopOAuthClientPublic]` — Non-revoked clients for user
- `revoke_client(session, user_id, client_id_str) -> None` — Soft-revoke + cascade revoke all tokens
- `verify_active_or_raise(session, external_client_id) -> DesktopOAuthClient` — Used by `get_current_user` to enforce immediate revocation of desktop-issued access tokens; raises `DesktopAuthError("client_missing" | "client_invalid" | "revoked", ...)` and stamps `last_used_at` (throttled). See [Live Access Token Revocation Check](#live-access-token-revocation-check)

**Consent flow:**
- `create_auth_request(session, device_name?, platform?, app_version?, client_id?, code_challenge, redirect_uri, state) -> str` — Store pending request row; returns raw nonce
- `get_auth_request(session, nonce) -> dict | None` — Returns display metadata or None if not found/used/expired
- `process_consent(session, user_id, nonce, action) -> dict` — Returns `{"redirect_to": "..."}`. On approve: resolves or lazily creates client, issues auth code, marks nonce used. On deny: marks nonce used, returns redirect with error=access_denied.

**Token flow:**
- `create_authorization_code(session, user_id, client_id_str, code_challenge, redirect_uri) -> str` — Issue auth code for existing client (used by process_consent internally)
- `exchange_code(session, code, client_id_str, redirect_uri, code_verifier) -> dict` — Validate code + PKCE, issue token pair; dict includes `client_id`
- `refresh_tokens(session, refresh_token_value, client_id_str) -> dict` — Validate + rotate, replay detection with reuse-grace window, issue new pair; dict includes `client_id`

**Revocation:**
- `revoke_token_family(session, family_id) -> None` — Revoke all tokens in a rotation chain (does NOT stamp `revoked_at`; theft-response / hard revocations use this path)
- `revoke_by_refresh_token(session, user_id, refresh_token_value) -> None` — Revoke specific token + family
- `_revoke_live_family_tokens(session, family_id, now) -> None` — Private; stamps `revoked_at = now` on every still-live token in the family. Used by the grace re-rotation path to collapse the family back to a single live token before issuing the fresh pair

**Cleanup:**
- `cleanup_expired(session) -> int` — Delete expired auth codes, expired consent requests, and old revoked/expired refresh tokens

### DesktopAuthCrypto (`backend/app/services/desktop_auth/desktop_auth_crypto.py`)

- `generate_client_id() -> str` — 32-char URL-safe random string
- `generate_auth_code() -> str` — 48-char URL-safe random string (also used as nonce)
- `generate_refresh_token() -> str` — 64-char URL-safe random string
- `hash_token(value) -> str` — SHA-256 hex digest
- `verify_pkce(verifier, challenge) -> bool` — S256 PKCE verification with constant-time comparison

## Token Response

The `TokenResponse` model (both code exchange and refresh) includes:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer",
  "expires_in": 900,
  "client_id": "abc123..."
}
```

The `client_id` field allows desktop apps using lazy registration to discover their assigned client_id after the first token exchange. Subsequent refresh calls must include this `client_id`.

## Frontend Consent Page

Route: `/desktop-auth/consent?request={nonce}` (file: `frontend/src/routes/desktop-auth/consent.tsx`)

- Public route (not under `_layout/`), but `beforeLoad` redirects unauthenticated users to `/login`
- On mount: `GET /requests/{nonce}` to fetch display metadata
- Renders: a "Signed in as" block with the current user's name/email (lightweight `GET /users/me` query sharing the `["currentUser"]` cache key) so the user can confirm which account they're authorizing; plus device name, platform, app version from the request metadata
- Approve button: `POST /consent` with `action="approve"` → receives `redirect_to` → `window.location.href = redirect_to`
- Deny button: `POST /consent` with `action="deny"` → receives `redirect_to` with `error=access_denied` → navigates there
- "Use another account" link (footer): calls `redirectToLoginPreservingTarget()` — clears the stored `access_token` (logout) and redirects to `/login?redirect=<this consent URL>`, so a user who authenticated as the wrong account can sign in as a different one and land back on the same consent request
- After redirect, attempts to close the browser tab (works for script-opened tabs)

## Security Notes

- Redirect URI validation (`_validate_redirect_uri`) accepts four native-client forms per RFC 8252: (1) **loopback HTTP** `http://localhost:{1024-65535}{path}` / `http://127.0.0.1:{1024-65535}{path}` — desktop, path unrestricted (§7.3); (2) **mobile app scheme** `cinna-mobile://...` plus hyphenated dev/preview variants `cinna-mobile-dev://...` (`_APP_SCHEME_RE`, regex `^cinna-mobile(-[a-z0-9]+)*://`) — private-use URI scheme (§7.1), accepted in all environments; (3) **iOS bundle scheme** `io.opencinna.ios://...` and dotted dev/staging variants `io.opencinna.ios.dev://...` (`_IOS_SCHEME_RE`, regex `^io\.opencinna\.ios(\.[a-z0-9]+)*://`) — Apple's bundle-id-as-URL-scheme convention (§7.1), accepted in all environments; (4) **Expo Go dev** `exp://{host}:{port}/...` (`_EXPO_DEV_RE`) — accepted only when `settings.ENVIRONMENT != "production"`. Anything else → HTTP 400 `invalid_redirect_uri`. The same validation runs at both `authorize` and `create_authorization_code` call sites; token exchange compares the presented `redirect_uri` against the stored one by exact string match (`auth_code.redirect_uri != redirect_uri`).
- All token values stored as SHA-256 hashes; raw values are never persisted
- Consent nonces stored as SHA-256 hashes; raw nonce appears only in the browser URL during the consent flow
- Access tokens are standard JWTs (same `create_access_token()` as web login) — `CurrentUser` dependency works unchanged, but now performs an extra `DesktopOAuthClient.is_revoked` lookup when the JWT carries `client_kind="desktop"` so disconnects propagate immediately (see [Live Access Token Revocation Check](#live-access-token-revocation-check) below)
- `GET /authorize` is now public — authentication happens at `POST /consent` via the SPA's localStorage JWT
- Replay detection with rotation reuse-grace window (RFC 9700 §4.14.2): when a revoked token is re-presented, `refresh_tokens()` checks whether `revoked_at` is set AND `now - revoked_at <= DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS` (default 60 s). If within the window, the token is treated as a benign lost-rotation-response retry: the token is re-validated (must not be expired, client must be active), any still-live successor in the family is revoked via `_revoke_live_family_tokens()` (family collapses to one live token), and a fresh pair is issued from the same `token_family`. Outside the window, or when `revoked_at` is NULL (legacy rows, or tokens hard-revoked by `revoke_token_family()` / `revoke_client()`), the full-family revocation path runs and returns 400 `invalid_grant`. Hard-revocation paths deliberately do not stamp `revoked_at`, ensuring that tokens revoked for security reasons (theft detection, explicit disconnect) can never enter the grace path.
- `code_challenge_method` must be `S256`; other methods rejected with 400
- Cross-user protection: if a `client_id` is provided in the authorize request, `POST /consent` validates that the client belongs to the consenting user (HTTP 403 if not)

## Live Access Token Revocation Check

`backend/app/api/deps.py::get_current_user` inspects the decoded JWT payload for the `client_kind` claim. When the value equals `"desktop"`, it delegates to `DesktopAuthService.verify_active_or_raise(session, external_client_id)` which:

1. Parses `external_client_id` from the JWT as a UUID — raises `DesktopAuthError("client_missing" | "client_invalid", ...)` if absent or malformed.
2. Loads the `DesktopOAuthClient` row by PK. Missing row OR `is_revoked=True` → `DesktopAuthError("revoked", "Desktop session has been revoked")`.
3. Throttled stamping: if `last_used_at` is `NULL` or older than `DESKTOP_LAST_USED_THROTTLE_SECONDS` (60s), sets `last_used_at = now()` and commits. The throttle keeps write amplification low for chatty clients while still giving the Settings UI a near-live "last active" timestamp.

The dep catches `DesktopAuthError` and re-raises as `HTTPException(401, detail=e.message)` — the service stays HTTP-agnostic so it can be reused from WS deps or other callers later (same pattern as `CLIAuthError` / `_resolve_cli_context`).

The check fires for every authenticated request from a desktop client (not just `/api/v1/external/...`), so `/api/v1/users/me`, `/api/v1/desktop-auth/userinfo`, etc. all reject revoked tokens. Web-session JWTs lack `client_kind`, so they short-circuit without the extra DB hit.

Test coverage lives in `backend/tests/api/desktop_auth/test_desktop_auth.py`:
- `test_revoked_desktop_client_blocks_access_token` — revocation invalidates `/users/me`
- `test_revoked_desktop_client_blocks_external_a2a_endpoints` — same, for `/api/v1/external/agents`
- `test_desktop_token_updates_last_used_at` — successful calls update the stamp
- `test_revoked_desktop_client_rejects_userinfo` — `/desktop-auth/userinfo` is covered too
