# Desktop App Authentication — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/desktop_auth/__init__.py` — Re-exports all desktop auth models
- `backend/app/models/desktop_auth/desktop_oauth_client.py` — DesktopOAuthClient (table), DesktopOAuthClientCreate, DesktopOAuthClientPublic
- `backend/app/models/desktop_auth/desktop_refresh_token.py` — DesktopRefreshToken (table)
- `backend/app/models/desktop_auth/desktop_auth_code.py` — DesktopAuthCode (table)
- `backend/app/models/desktop_auth/desktop_auth_request.py` — DesktopAuthRequest (table) — pending consent requests

### Backend — Routes

- `backend/app/api/routes/desktop_auth.py` — All OAuth endpoints under `/desktop-auth` prefix
- `backend/app/main.py` — `/.well-known/cinna-desktop` endpoint registered at app level (not under `/api/v1`)

### Backend — Services

- `backend/app/services/desktop_auth/desktop_auth_service.py` — DesktopAuthService: consent flow, client management, authorization code, token exchange, refresh rotation, revocation, cleanup
- `backend/app/services/desktop_auth/desktop_auth_crypto.py` — Crypto helpers: ID/token generation, SHA-256 hashing, PKCE S256 verification
- `backend/app/services/desktop_auth/desktop_auth_scheduler.py` — Background cleanup scheduler (every 15 minutes)

### Backend — Configuration

- `backend/app/core/config.py` — `DESKTOP_AUTH_ENABLED`, `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES`, `DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS`

### Backend — Migrations

- `backend/app/alembic/versions/d3e4f5a6b7c8_add_desktop_auth_tables.py` — Creates desktop_oauth_client, desktop_refresh_token, desktop_auth_code tables
- `backend/app/alembic/versions/d7e34bcff709_add_desktop_auth_request_table.py` — Adds desktop_auth_request table (consent-page nonce store)

### Backend — Tests

- `backend/tests/api/desktop_auth/test_desktop_auth.py` — 17 scenario-based integration tests covering full consent flow
- `backend/tests/utils/desktop_auth.py` — Test helpers: PKCE pair generation, consent flow steps, token exchange

### Frontend

- `frontend/src/routes/desktop-auth/consent.tsx` — Public SPA consent page at `/desktop-auth/consent?request={nonce}`
- `frontend/src/components/UserSettings/DesktopSessionsCard.tsx` — Connected devices list + disconnect dialog
- `frontend/src/routes/_layout/settings.tsx` — DesktopSessionsCard added to Channels tab
- `frontend/src/client/sdk.gen.ts` — `DesktopAuthService` (auto-generated)

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

## Services & Key Methods

### DesktopAuthService (`backend/app/services/desktop_auth/desktop_auth_service.py`)

All methods are `@staticmethod`:

**Client management:**
- `list_clients(session, user_id) -> list[DesktopOAuthClientPublic]` — Non-revoked clients for user
- `revoke_client(session, user_id, client_id_str) -> None` — Soft-revoke + cascade revoke all tokens

**Consent flow:**
- `create_auth_request(session, device_name?, platform?, app_version?, client_id?, code_challenge, redirect_uri, state) -> str` — Store pending request row; returns raw nonce
- `get_auth_request(session, nonce) -> dict | None` — Returns display metadata or None if not found/used/expired
- `process_consent(session, user_id, nonce, action) -> dict` — Returns `{"redirect_to": "..."}`. On approve: resolves or lazily creates client, issues auth code, marks nonce used. On deny: marks nonce used, returns redirect with error=access_denied.

**Token flow:**
- `create_authorization_code(session, user_id, client_id_str, code_challenge, redirect_uri) -> str` — Issue auth code for existing client (used by process_consent internally)
- `exchange_code(session, code, client_id_str, redirect_uri, code_verifier) -> dict` — Validate code + PKCE, issue token pair; dict includes `client_id`
- `refresh_tokens(session, refresh_token_value, client_id_str) -> dict` — Validate + rotate, replay detection, issue new pair; dict includes `client_id`

**Revocation:**
- `revoke_token_family(session, family_id) -> None` — Revoke all tokens in a rotation chain
- `revoke_by_refresh_token(session, user_id, refresh_token_value) -> None` — Revoke specific token + family

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
- Renders: device name, platform, app version from the request metadata
- Approve button: `POST /consent` with `action="approve"` → receives `redirect_to` → `window.location.href = redirect_to`
- Deny button: `POST /consent` with `action="deny"` → receives `redirect_to` with `error=access_denied` → navigates there
- After redirect, attempts to close the browser tab (works for script-opened tabs)

## Security Notes

- Redirect URI restricted to loopback HTTP: `http://localhost:{1024-65535}{path}` or `http://127.0.0.1:{1024-65535}{path}`. Path is unrestricted (RFC 8252 §7.3) so apps can use `/callback`, `/oauth/callback`, etc.
- All token values stored as SHA-256 hashes; raw values are never persisted
- Consent nonces stored as SHA-256 hashes; raw nonce appears only in the browser URL during the consent flow
- Access tokens are standard JWTs (same `create_access_token()` as web login) — `CurrentUser` dependency works unchanged
- `GET /authorize` is now public — authentication happens at `POST /consent` via the SPA's localStorage JWT
- Replay detection: reusing a revoked refresh token triggers `revoke_token_family()`, revoking the entire rotation chain (RFC 9700 §4.14.2)
- `code_challenge_method` must be `S256`; other methods rejected with 400
- Cross-user protection: if a `client_id` is provided in the authorize request, `POST /consent` validates that the client belongs to the consenting user (HTTP 403 if not)
