# Desktop App Authentication (cinna-desktop)

## Overview

Server-side OAuth 2.0 infrastructure that allows the Cinna Desktop application to authenticate against any Cinna instance — self-hosted or cloud (opencinna.io). The desktop app acts as a public OAuth client: it opens the user's browser for login, receives an authorization code via a local callback, and exchanges it for access + refresh tokens.

**Core capabilities:**
- Instance discovery — user provides a domain (e.g. `my-company.cinna.io`) or selects "Cloud" (resolves to `opencinna.io`)
- Browser-based login — standard OAuth authorization code flow with PKCE
- Token pair — short-lived access token + long-lived refresh token
- Silent refresh — desktop app renews access tokens without user interaction
- Multi-instance — user can be logged into multiple instances simultaneously

**High-level flow:**

```
Desktop App                     Browser                      Cinna Backend
    │                              │                              │
    │── 1. GET /.well-known/cinna-desktop ──────────────────────►│
    │◄─────── instance metadata (auth endpoint, name) ───────────│
    │                              │                              │
    │── 2. Open browser ─────────►│                              │
    │   (authorize URL + PKCE)     │── 3. User logs in ────────►│
    │                              │◄── 4. Redirect w/ code ────│
    │◄─ 5. localhost callback ────│                              │
    │   (authorization code)       │                              │
    │                              │                              │
    │── 6. POST /token (code + verifier) ──────────────────────►│
    │◄─────── { access_token, refresh_token, expires_in } ──────│
    │                              │                              │
    │── 7. API calls with access_token ────────────────────────►│
    │                              │                              │
    │── 8. POST /token (refresh) ──────────────────────────────►│
    │◄─────── { new access_token, new refresh_token } ──────────│
```

---

## Architecture Overview

### System Components

```
┌──────────────────┐       ┌─────────────┐       ┌──────────────────────┐
│  Cinna Desktop   │       │   Browser    │       │    Cinna Backend     │
│                  │       │              │       │                      │
│  - Instance mgr  │──────►│  Login page  │──────►│  OAuth endpoints     │
│  - Token store   │       │  (existing)  │       │  ├─ /authorize       │
│  - API client    │       │              │       │  ├─ /token            │
│  - Auto-refresh  │       └──────────────┘       │  └─ /revoke          │
│                  │                               │                      │
│  localhost:CB    │◄──── redirect with code ──────│  Desktop OAuth       │
│  (callback srv)  │                               │  Service             │
└──────────────────┘                               │                      │
                                                   │  Refresh Token Store │
                                                   │  (DB table)          │
                                                   └──────────────────────┘
```

### Data Flow

1. **Discovery**: Desktop app fetches `GET /.well-known/cinna-desktop` to confirm the instance is reachable and get metadata (instance name, OAuth authorize URL, supported features).
2. **Authorization**: Desktop opens browser to `/api/v1/desktop-auth/authorize?...` with PKCE challenge, client_id, redirect_uri, state.
3. **Login**: User authenticates via existing login page (password or Google OAuth). Backend creates an authorization code.
4. **Callback**: Browser redirects to `http://localhost:{port}/callback?code=...&state=...`. Desktop app's ephemeral HTTP server captures the code.
5. **Token Exchange**: Desktop POSTs to `/api/v1/desktop-auth/token` with the code + PKCE verifier. Backend returns access token + refresh token.
6. **API Usage**: Desktop uses the access token as a standard Bearer token for all API calls (same `CurrentUser` dependency — no changes needed to existing endpoints).
7. **Refresh**: When the access token expires, desktop POSTs refresh token to `/api/v1/desktop-auth/token` (grant_type=refresh_token) to get a new pair.

### Why OAuth + PKCE (not simple password login)

- Desktop apps are public clients — they cannot securely store a client secret
- PKCE prevents authorization code interception attacks
- Refresh tokens avoid storing the user's password on disk
- Consistent with industry standard for native/desktop apps (RFC 7636, RFC 8252)
- Works with Google OAuth users who have no password

---

## Data Models

### desktop_oauth_client (registration table)

Each desktop app installation registers itself as an OAuth client. This enables per-device session management and revocation.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | UUID | PK | Client record ID |
| `client_id` | VARCHAR(64) | unique, indexed | Public client identifier (generated, URL-safe random) |
| `user_id` | UUID | FK -> user.id, CASCADE | Owner of this client registration |
| `device_name` | VARCHAR(200) | not null | User-friendly device name (e.g. "MacBook Pro - Work") |
| `platform` | VARCHAR(50) | nullable | OS identifier (macos, windows, linux) |
| `app_version` | VARCHAR(50) | nullable | Desktop app version at registration |
| `is_revoked` | BOOLEAN | default false | Soft revocation flag |
| `last_used_at` | TIMESTAMP WITH TZ | nullable | Last successful token operation |
| `created_at` | TIMESTAMP WITH TZ | default now | Registration time |

Indexes: `ix_desktop_oauth_client_client_id` (unique), `ix_desktop_oauth_client_user_id`

### desktop_refresh_token

Stores hashed refresh tokens. One client may have one active refresh token at a time (token rotation).

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | UUID | PK | Token record ID |
| `client_id` | UUID | FK -> desktop_oauth_client.id, CASCADE | Owning client |
| `user_id` | UUID | FK -> user.id, CASCADE | Token owner |
| `token_hash` | VARCHAR | unique, indexed | SHA-256 hash of the refresh token value |
| `token_family` | UUID | not null | Groups tokens in the same rotation chain (for replay detection) |
| `is_revoked` | BOOLEAN | default false | Revoked by user or rotation |
| `expires_at` | TIMESTAMP WITH TZ | not null | Absolute expiry (30 days from creation) |
| `created_at` | TIMESTAMP WITH TZ | default now | Creation time |

Indexes: `ix_desktop_refresh_token_hash` (unique), `ix_desktop_refresh_token_client_id`, `ix_desktop_refresh_token_family`

### desktop_auth_code (ephemeral)

Short-lived authorization codes. Cleaned up by background scheduler.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | UUID | PK | Code record ID |
| `code_hash` | VARCHAR | unique, indexed | SHA-256 hash of the authorization code |
| `user_id` | UUID | FK -> user.id, CASCADE | Authenticated user |
| `client_id` | VARCHAR(64) | not null | Desktop client_id that initiated the flow |
| `code_challenge` | VARCHAR(128) | not null | PKCE S256 challenge |
| `redirect_uri` | VARCHAR(255) | not null | The redirect_uri used in the request |
| `is_used` | BOOLEAN | default false | Single-use enforcement |
| `expires_at` | TIMESTAMP WITH TZ | not null | 5-minute TTL |
| `created_at` | TIMESTAMP WITH TZ | default now | |

Indexes: `ix_desktop_auth_code_hash` (unique)

---

## Security Architecture

### PKCE (Proof Key for Code Exchange)

- Desktop generates a random `code_verifier` (43-128 chars, URL-safe)
- Computes `code_challenge = BASE64URL(SHA256(code_verifier))` with method `S256`
- Challenge sent in authorize request; verifier sent in token exchange
- Backend verifies `SHA256(verifier) == stored_challenge` before issuing tokens
- Prevents authorization code interception by malicious apps on the same machine

### Token Security

| Token | Lifetime | Storage (Desktop) | Storage (Backend) |
|-------|----------|-------------------|-------------------|
| Access token | 15 minutes | In-memory only | Not stored (stateless JWT) |
| Refresh token | 30 days | OS keychain (encrypted) | SHA-256 hash in DB |
| Authorization code | 5 minutes | Ephemeral (in callback) | SHA-256 hash in DB |

### Refresh Token Rotation

- Every refresh token use issues a new refresh token and invalidates the old one
- Tokens share a `token_family` UUID — if a revoked token is reused (replay attack), the entire family is revoked, forcing re-authentication
- This follows OAuth 2.0 Security Best Current Practice (RFC 9700)

### Access Control

- Authorization endpoint requires an authenticated session (user must log in via the existing web login page)
- Token endpoint is public (no auth required) but requires valid code+PKCE or valid refresh token
- Revocation endpoint requires valid access token (CurrentUser)
- Users can only manage their own clients and tokens

### Redirect URI Validation

- Only `http://localhost:{port}/callback` and `http://127.0.0.1:{port}/callback` are accepted
- Port is dynamic (desktop app picks an available port) — validated as numeric, range 1024-65535
- No other schemes or hosts are accepted (prevents open redirect)

### Rate Limiting

- Token endpoint: 10 requests per minute per IP (prevents brute-force on authorization codes)
- Failed refresh attempts: 5 per minute per client_id (prevents token guessing)

---

## Backend Implementation

### API Routes

**File**: `backend/app/api/routes/desktop_auth.py`

**Router**: `APIRouter(prefix="/desktop-auth", tags=["desktop-auth"])`

#### Instance Discovery (no auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/.well-known/cinna-desktop` | Instance metadata for desktop app discovery. Registered at app level in `main.py`, not under `/api/v1` |

**Response**:
```
{
  "instance_name": str,          # PROJECT_NAME from settings
  "auth_url": str,               # Full URL to /api/v1/desktop-auth/authorize
  "token_url": str,              # Full URL to /api/v1/desktop-auth/token
  "version": str,                # Backend version / API version
  "desktop_auth_enabled": bool   # Feature flag
}
```

#### OAuth Flow Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/desktop-auth/clients` | CurrentUser | Register a new desktop client (returns client_id) |
| GET | `/api/v1/desktop-auth/clients` | CurrentUser | List user's registered desktop clients |
| DELETE | `/api/v1/desktop-auth/clients/{client_id}` | CurrentUser | Revoke a desktop client and all its tokens |
| GET | `/api/v1/desktop-auth/authorize` | Session (cookie/redirect) | Authorization endpoint — shows consent, issues code |
| POST | `/api/v1/desktop-auth/token` | None (public) | Token endpoint — exchange code or refresh token |
| POST | `/api/v1/desktop-auth/revoke` | CurrentUser | Revoke a specific refresh token |

#### Endpoint Details

**POST `/api/v1/desktop-auth/clients`** (CurrentUser)
- Request: `{ device_name: str, platform?: str, app_version?: str }`
- Response: `{ client_id: str, device_name: str, created_at: datetime }`
- Creates a new `desktop_oauth_client` record with generated client_id

**GET `/api/v1/desktop-auth/authorize`** (Browser session)
- Query params: `client_id`, `redirect_uri`, `code_challenge`, `code_challenge_method=S256`, `state`
- If user is not logged in: redirects to login page with `?next=` pointing back to authorize
- If user is logged in: generates authorization code, redirects to `redirect_uri?code=...&state=...`
- Validates: client_id exists and is not revoked, redirect_uri is localhost, code_challenge_method is S256

**POST `/api/v1/desktop-auth/token`** (Public)
- For `grant_type=authorization_code`:
  - Request: `{ grant_type, code, client_id, redirect_uri, code_verifier }`
  - Validates code (not used, not expired), PKCE verifier matches challenge
  - Returns: `{ access_token, refresh_token, token_type: "bearer", expires_in: 900 }`
- For `grant_type=refresh_token`:
  - Request: `{ grant_type, refresh_token, client_id }`
  - Validates refresh token (not revoked, not expired, belongs to client)
  - Rotates: revokes old refresh token, issues new pair
  - Returns: same shape as above

**POST `/api/v1/desktop-auth/revoke`** (CurrentUser)
- Request: `{ client_id: str }` — revokes all tokens for that client
- Or: `{ refresh_token: str }` — revokes specific token + family

### Service Layer

**File**: `backend/app/services/desktop_auth/desktop_auth_service.py`

**Class**: `DesktopAuthService`

Key methods:

| Method | Signature | Description |
|--------|-----------|-------------|
| `register_client` | `(session, user_id, device_name, platform?, app_version?) -> DesktopOAuthClientPublic` | Create client record with random client_id |
| `list_clients` | `(session, user_id) -> list[DesktopOAuthClientPublic]` | List active (non-revoked) clients |
| `revoke_client` | `(session, user_id, client_id) -> None` | Soft-revoke client + cascade revoke all tokens |
| `create_authorization_code` | `(session, user_id, client_id, code_challenge, redirect_uri) -> str` | Generate code, store hash + challenge |
| `exchange_code` | `(session, code, client_id, redirect_uri, code_verifier) -> TokenPair` | Validate code + PKCE, issue token pair |
| `refresh_tokens` | `(session, refresh_token_value, client_id) -> TokenPair` | Validate + rotate refresh token, issue new pair |
| `revoke_token_family` | `(session, family_id) -> None` | Revoke all tokens in a rotation chain |
| `revoke_by_refresh_token` | `(session, user_id, refresh_token_value) -> None` | Revoke specific token + family |
| `cleanup_expired` | `(session) -> int` | Delete expired auth codes and refresh tokens |

**Helper file**: `backend/app/services/desktop_auth/desktop_auth_crypto.py`

| Function | Description |
|----------|-------------|
| `generate_client_id() -> str` | 32-char URL-safe random string |
| `generate_auth_code() -> str` | 48-char URL-safe random string |
| `generate_refresh_token() -> str` | 64-char URL-safe random string |
| `hash_token(value: str) -> str` | SHA-256 hex digest |
| `verify_pkce(verifier: str, challenge: str) -> bool` | S256 verification |

### Background Tasks

**File**: `backend/app/services/desktop_auth/desktop_auth_scheduler.py`

- **Expired code cleanup**: Runs every 15 minutes, deletes auth codes where `expires_at < now`
- **Expired token cleanup**: Runs hourly, deletes revoked/expired refresh tokens older than 7 days
- Follows same pattern as `cli_setup_token_scheduler.py`

### Configuration

Add to `backend/app/core/config.py`:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `DESKTOP_AUTH_ENABLED` | bool | True | Feature flag for desktop auth endpoints |
| `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES` | int | 15 | Access token lifetime for desktop clients |
| `DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS` | int | 30 | Refresh token lifetime |

---

## Frontend Implementation

### UI Components

The frontend changes are minimal — this feature is primarily backend-driven. The desktop app itself is a separate project. Frontend additions are for managing connected desktop sessions.

#### Settings > Sessions Page

**File**: `frontend/src/routes/_layout/settings.tsx` (new tab or section)

**Component**: `frontend/src/components/UserSettings/DesktopSessionsCard.tsx`

- Card title: "Desktop Sessions"
- Description: "Manage Cinna Desktop app connections"
- **List view**: Shows registered desktop clients with:
  - Device name
  - Platform icon (macOS/Windows/Linux)
  - App version
  - Last used time (relative)
  - "Disconnect" button (revoke)
- **Empty state**: "No desktop apps connected. Download Cinna Desktop to get started."
- **Disconnect dialog**: AlertDialog confirmation before revoking (same pattern as `LocalDevCard.tsx` disconnect)

#### Authorization Consent Page (optional, future)

For MVP, the authorize endpoint redirects directly after login. In the future, a consent screen could be added:

**File**: `frontend/src/routes/desktop-authorize.tsx` (public route)

- Shows: "Cinna Desktop wants to access your account"
- Instance name, device name
- "Allow" / "Deny" buttons
- For MVP: skip this, auto-approve after login

### State Management

- Query key: `["desktop-clients"]`
- `useQuery` to fetch client list from `GET /api/v1/desktop-auth/clients`
- `useMutation` for disconnect (DELETE) with query invalidation

### User Flows

#### Connecting Desktop App (from desktop side — not platform frontend)

1. User opens Cinna Desktop, enters instance URL (e.g. `my-company.cinna.io`) or selects "Cloud"
2. Desktop fetches `/.well-known/cinna-desktop` to validate instance
3. Desktop registers as a client via `POST /api/v1/desktop-auth/clients` (first time only — requires initial auth, see bootstrap below)
4. Desktop opens browser to authorize endpoint with PKCE
5. User logs in (if not already) via existing login page
6. Browser redirects to localhost callback with code
7. Desktop exchanges code for tokens
8. Desktop displays "Connected to {instance_name}" with user info

**Bootstrap problem**: Step 3 requires auth, but the user hasn't authenticated yet. Two approaches:

**Approach A (Recommended)**: Combine client registration with token exchange. The `/token` endpoint accepts optional `device_name`/`platform`/`app_version` in the authorization_code exchange. If no `client_id` is provided in the authorize request, one is auto-created during token exchange. This eliminates the chicken-and-egg problem.

**Approach B**: Pre-register client_id in the desktop app (hardcoded per-platform). Simpler but less flexible — all installs share one client_id per platform.

**Decision**: Use **Approach A** — auto-registration during first token exchange. The authorize endpoint accepts `client_id=new` as a sentinel, and the token exchange creates the client record alongside the first token pair.

#### Disconnecting from Settings

1. User navigates to Settings > Sessions
2. Sees list of connected desktop apps
3. Clicks "Disconnect" on a device
4. Confirms in dialog
5. Backend revokes client + all refresh tokens
6. Desktop app's next API call or refresh fails with 401
7. Desktop shows "Session expired, please log in again"

---

## Database Migrations

**Migration file**: `add_desktop_auth_tables.py`

### Tables to create:

1. **`desktop_oauth_client`** — with indexes on `client_id` (unique) and `user_id`
2. **`desktop_refresh_token`** — with indexes on `token_hash` (unique), `client_id`, and `token_family`
3. **`desktop_auth_code`** — with index on `code_hash` (unique)

### Foreign keys:

- `desktop_oauth_client.user_id` -> `user.id` ON DELETE CASCADE
- `desktop_refresh_token.client_id` -> `desktop_oauth_client.id` ON DELETE CASCADE
- `desktop_refresh_token.user_id` -> `user.id` ON DELETE CASCADE
- `desktop_auth_code.user_id` -> `user.id` ON DELETE CASCADE

### Downgrade:

- Drop `desktop_auth_code`, `desktop_refresh_token`, `desktop_oauth_client` in that order (respects FK dependencies)

---

## Error Handling & Edge Cases

### Authorization Flow

| Scenario | Handling |
|----------|----------|
| Invalid/unknown instance URL | Desktop shows "Could not connect to instance" with suggestion to check URL |
| Instance doesn't support desktop auth | `/.well-known/cinna-desktop` returns `desktop_auth_enabled: false` or 404 — desktop shows "This instance doesn't support desktop login" |
| User cancels browser login | No callback received — desktop shows timeout after 5 minutes with "Try again" button |
| Authorization code expired | Token endpoint returns 400 `invalid_grant` — desktop restarts auth flow |
| PKCE verification fails | Token endpoint returns 400 `invalid_grant` — desktop restarts auth flow |
| Redirect URI mismatch | Authorize endpoint returns 400 — desktop shows error |

### Token Lifecycle

| Scenario | Handling |
|----------|----------|
| Access token expired | Desktop silently refreshes using refresh token |
| Refresh token expired | Desktop shows "Session expired, please log in again" — clears tokens, restarts auth |
| Refresh token revoked (by user from Settings) | Same as expired — 401 on refresh attempt |
| Refresh token replay (stolen token reused) | Entire token family revoked — all sessions for that client invalidated |
| Client revoked | All API calls return 401 — desktop clears state and prompts re-login |
| User account deactivated | `get_current_user` returns 400 "Inactive user" — desktop shows appropriate message |

### Network & Edge Cases

| Scenario | Handling |
|----------|----------|
| Desktop offline during refresh | Queue the refresh, retry when online |
| Multiple desktop instances same client_id | Token rotation may invalidate the other instance — each install should register as a separate client |
| Port conflict on localhost callback | Desktop tries ports 19836-19846, gives up after 10 attempts with error message |
| User has no password and no Google OAuth configured | User can still use Google OAuth via browser — the authorize flow redirects to the standard login page which supports Google |

---

## UI/UX Considerations

### Settings > Sessions Card

- Use same card layout as other Settings sections (consistent with `LocalDevCard.tsx`)
- Device platform shown with icon: Apple logo for macOS, Windows logo, Tux for Linux
- Last used time: use relative format ("2 hours ago", "3 days ago")
- Disconnect button: red/destructive variant, requires confirmation dialog
- Empty state: subtle illustration + download link

### Desktop App (guidance for cinna-desktop project)

- Instance input: text field with placeholder "your-instance.cinna.io" and a "Cloud" quick-select button
- Instance validation: show green checkmark when `/.well-known/cinna-desktop` returns successfully
- Login status: show spinner while waiting for browser callback, with "Waiting for browser login..." text
- Connected state: show instance name, user email, workspace name in sidebar/header
- Token expiry: handle silently — user should never see token-related errors during normal use

---

## Integration Points

### Existing Systems

- **Authentication (`deps.py`)**: Desktop access tokens are standard JWTs with the same structure as web tokens. `CurrentUser` dependency works unchanged — no modifications to existing auth middleware.
- **All existing API endpoints**: Work transparently with desktop access tokens (same Bearer format).
- **Google OAuth**: Users who only have Google accounts can still authenticate — the browser-based authorize flow uses the existing login page which supports Google.
- **Domain whitelist**: Enforced at the existing login/signup level — desktop auth inherits this automatically.

### New Endpoint Registration

- `backend/app/main.py`: Register `/.well-known/cinna-desktop` route at app level (not under API prefix)
- `backend/app/api/main.py`: Register `desktop_auth.router` under the API router
- `backend/app/main.py` (lifespan): Start/stop desktop auth cleanup scheduler

### Frontend Client Regeneration

After implementing backend endpoints, regenerate the frontend OpenAPI client:
```bash
source ./backend/.venv/bin/activate && make gen-client
```

This creates `DesktopAuthService` in `frontend/src/client/sdk.gen.ts`.

---

## Future Enhancements (Out of Scope)

- **Consent screen**: Show "Allow Cinna Desktop to access your account?" page before issuing code (MVP auto-approves)
- **Scoped tokens**: Desktop tokens with limited permissions (read-only, specific workspace, etc.)
- **Device trust/remember**: Skip re-authentication for trusted devices
- **Push notifications**: Backend pushes events to connected desktop apps via WebSocket
- **Biometric unlock**: Desktop app uses OS biometrics to unlock stored refresh token
- **Token binding**: Bind tokens to device hardware (DPoP or similar)
- **Mobile app support**: Same OAuth flow works for mobile — redirect URI uses custom scheme instead of localhost
- **Admin dashboard**: Admin view of all connected desktop clients across users

---

## Summary Checklist

### Backend Tasks

- [ ] Add `DESKTOP_AUTH_ENABLED`, `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES`, `DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS` to `config.py`
- [ ] Create models in `backend/app/models/desktop_auth/`:
  - `desktop_oauth_client.py` — DesktopOAuthClient (table), Base, Public, Create
  - `desktop_refresh_token.py` — DesktopRefreshToken (table)
  - `desktop_auth_code.py` — DesktopAuthCode (table)
- [ ] Re-export models in `backend/app/models/__init__.py`
- [ ] Create Alembic migration `add_desktop_auth_tables.py` with all three tables + indexes + FKs
- [ ] Create `backend/app/services/desktop_auth/desktop_auth_service.py` — DesktopAuthService
- [ ] Create `backend/app/services/desktop_auth/desktop_auth_crypto.py` — crypto helpers (client_id gen, code gen, refresh token gen, hash, PKCE verify)
- [ ] Create `backend/app/services/desktop_auth/desktop_auth_scheduler.py` — expired code/token cleanup
- [ ] Create `backend/app/api/routes/desktop_auth.py` — all OAuth endpoints
- [ ] Add `/.well-known/cinna-desktop` endpoint in `main.py`
- [ ] Register router in `backend/app/api/main.py`
- [ ] Register scheduler in `main.py` lifespan
- [ ] Handle authorize flow: redirect to login page if not authenticated, issue code on return

### Frontend Tasks

- [ ] Create `frontend/src/components/UserSettings/DesktopSessionsCard.tsx` — connected devices list + disconnect
- [ ] Add DesktopSessionsCard to Settings page (new tab or section within existing Channels/Sessions area)
- [ ] Regenerate OpenAPI client after backend implementation

### Testing & Validation

- [ ] Verify instance discovery endpoint returns correct metadata
- [ ] Test full authorization code flow with PKCE (happy path)
- [ ] Test authorization code expiry and single-use enforcement
- [ ] Test PKCE verification (correct verifier passes, wrong verifier fails)
- [ ] Test refresh token rotation (old token invalidated, new token works)
- [ ] Test refresh token replay detection (reuse of revoked token revokes entire family)
- [ ] Test client revocation cascades to all tokens
- [ ] Test redirect URI validation (only localhost accepted)
- [ ] Test that desktop access tokens work with existing API endpoints (CurrentUser resolves)
- [ ] Test user deactivation blocks token refresh
- [ ] Test cleanup scheduler removes expired codes and tokens
