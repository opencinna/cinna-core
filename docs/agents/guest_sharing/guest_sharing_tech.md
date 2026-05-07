# Guest Sharing — Technical Reference

## File Locations

### Backend

**Models:**
- `backend/app/models/sharing/agent_guest_share.py` — `AgentGuestShare` (table), `GuestShareGrant` (table), `AgentGuestShareCreate`, `AgentGuestShareUpdate`, `AgentGuestSharePublic`, `AgentGuestShareCreated`, `AgentGuestSharesPublic`, `GuestShareTokenPayload`
- `backend/app/models/__init__.py` — model re-exports

**API Routes:**
- `backend/app/api/routes/guest_shares.py` — two routers: `router` (owner CRUD, prefix `/agents/{agent_id}/guest-shares`, tag `guest-shares`) and `guest_router` (anonymous/activation flow, prefix `/guest-share`, tag `guest-share`)

**Services:**
- `backend/app/services/sharing/agent_guest_share_service.py` — `AgentGuestShareService` (static methods only)

**Auth Dependencies:**
- `backend/app/api/deps.py` — `GuestShareContext`, `get_current_user_or_guest()`, `CurrentUserOrGuest`

**Database Migration:**
- `backend/app/alembic/versions/ba8f1f14621f_...` (check alembic history for exact filename)

### Frontend

**Route (public, guest chat page):**
- `frontend/src/routes/guest/$guestShareToken.tsx` — `GuestChatPage` and all sub-components: `GuestLoadingScreen`, `GuestErrorScreen`, `SecurityCodeScreen`, `GuestChatHeader`, `GuestSessionSidebar`, `GuestNewChatState`, `GuestChatArea`

**Route (public, guest file viewer):**
- `frontend/src/routes/guest/file-viewer.tsx` — standalone file viewer for workspace files opened from env panel; search params: `envId`, `path`

**Owner Management Component:**
- `frontend/src/components/Agents/GuestShareCard.tsx` — `GuestShareCard` (rendered on Integrations tab)

**Context Hook:**
- `frontend/src/hooks/useGuestShare.tsx` — `GuestShareProvider`, `useGuestShare()` — provides `{ isGuest, guestShareId, agentId, guestShareToken }` to child components

**Integration tab:**
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — renders `GuestShareCard` alongside other integration cards

**Generated Client:**
- `frontend/src/client/sdk.gen.ts` — `GuestSharesService` (owner CRUD) and `GuestShareService` (guest auth)
- `frontend/src/client/types.gen.ts` — TypeScript types for all request/response models

---

## Database Schema

### Table: `agent_guest_share`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | Primary key |
| `agent_id` | UUID FK → `agent.id` | CASCADE delete; indexed |
| `owner_id` | UUID FK → `user.id` | CASCADE delete; indexed |
| `label` | String (max 255), nullable | Owner-assigned display name |
| `token_hash` | String | SHA-256 hex of raw token; indexed; used for all lookups |
| `token_prefix` | String (max 12) | First 8 characters of raw token; for UI display only |
| `token` | String, nullable | Full raw token; stored for `share_url` reconstruction |
| `expires_at` | Datetime | Computed from `expires_in_hours` at creation; immutable |
| `created_at` | Datetime | UTC creation timestamp |
| `is_revoked` | Boolean | Currently always `false`; token lookup excludes revoked records; deletion is the active revocation path |
| `security_code_encrypted` | String, nullable | Fernet-encrypted 4-digit code |
| `failed_code_attempts` | Integer | Counter of wrong code entries; reset on code update |
| `is_code_blocked` | Boolean | Set to `true` after 3 wrong attempts; reset on code update |
| `allow_env_panel` | Boolean | Whether guests can open the environment panel |

### Table: `guest_share_grant`

Tracks which authenticated users have activated a guest share. Unique constraint on `(user_id, guest_share_id)`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | Primary key |
| `user_id` | UUID FK → `user.id` | CASCADE delete |
| `guest_share_id` | UUID FK → `agent_guest_share.id` | CASCADE delete |
| `activated_at` | Datetime | UTC activation timestamp |

---

## API Endpoints

### Owner Router (`/api/v1/agents/{agent_id}/guest-shares`)

All owner endpoints require `CurrentUser` (standard JWT auth) and verify agent ownership before any operation.

| Method | Path | Request Body | Response | Description |
|--------|------|--------------|----------|-------------|
| `POST` | `/` | `AgentGuestShareCreate` | `AgentGuestShareCreated` | Create share. Token and security code shown once only |
| `GET` | `/` | — | `AgentGuestSharesPublic` | List all shares for the agent with session counts |
| `GET` | `/{guest_share_id}` | — | `AgentGuestSharePublic` | Get single share with session count |
| `PUT` | `/{guest_share_id}` | `AgentGuestShareUpdate` | `AgentGuestSharePublic` | Update label, security code, or `allow_env_panel` |
| `DELETE` | `/{guest_share_id}` | — | `Message` | Permanently delete share; sessions retain `guest_share_id = NULL` |

**`AgentGuestShareCreate` fields:**
- `label: str | None` — optional display label
- `expires_in_hours: int` — 1–720; default 24
- `allow_env_panel: bool` — default `false`

**`AgentGuestShareUpdate` fields:**
- `label: str | None`
- `security_code: str | None` — exactly 4 digits; validated by regex `^\d{4}$`; setting resets lockout counters
- `allow_env_panel: bool | None`

**`AgentGuestShareCreated` additional fields (only on creation):**
- `token: str` — full raw token (shown once only)
- `share_url: str` — `{FRONTEND_HOST}/guest/{token}`
- `security_code: str` — plaintext 4-digit code (shown once only)

### Guest Auth Router (`/api/v1/guest-share`)

No standard auth required on `info` and `auth` endpoints. `activate` requires a regular user JWT.

| Method | Path | Auth | Request Body | Response | Notes |
|--------|------|------|--------------|----------|-------|
| `GET` | `/{token}/info` | None | — | `dict` | Returns agent name, description, `is_valid`, `requires_code`, `is_code_blocked`, `allow_env_panel` |
| `POST` | `/{token}/auth` | None | `GuestShareAuthRequest` (optional) | `dict` with `access_token`, `token_type`, `guest_share_id`, `agent_id` | Issues guest JWT; 403 on wrong/blocked code, 410 on expired/revoked, 404 on not found |
| `POST` | `/{token}/activate` | `CurrentUser` | `GuestShareAuthRequest` (optional) | `dict` with `guest_share_id`, `agent_id`, `agent_name` | Creates `GuestShareGrant`; idempotent |

**`GuestShareAuthRequest`:**
- `security_code: str | None`

---

## Service Layer — `AgentGuestShareService`

All methods are static. Located in `backend/app/services/sharing/agent_guest_share_service.py`.

### Core Methods

**`create_guest_share(session, user_id, agent_id, data) → AgentGuestShareCreated`**
Verifies agent ownership. Generates `token = secrets.token_urlsafe(32)`, hashes it, takes first 8 chars as prefix. Generates `security_code = f"{random.randint(0, 9999):04d}"`, encrypts it. Computes `expires_at = now + timedelta(hours=data.expires_in_hours)`. Creates DB record. Constructs `share_url = f"{settings.FRONTEND_HOST}/guest/{token}"`. Returns `AgentGuestShareCreated` with plain token, URL, and security code.

**`list_guest_shares(session, user_id, agent_id) → AgentGuestSharesPublic`**
Verifies agent ownership. Returns all shares ordered by `created_at DESC`, with `session_count` computed via `COUNT` on `Session.guest_share_id`, `share_url` reconstructed from stored token, and `security_code` decrypted for owner display.

**`get_guest_share(session, user_id, agent_id, guest_share_id) → AgentGuestSharePublic | None`**
Verifies ownership. Same enrichment as list (session count, share_url, decrypted security code).

**`delete_guest_share(session, user_id, agent_id, guest_share_id) → bool`**
Hard-deletes the record. Foreign key on `Session.guest_share_id` is SET NULL, so sessions survive. Returns `False` if not found.

**`update_guest_share(session, user_id, agent_id, guest_share_id, data) → AgentGuestSharePublic | None`**
Updates any combination of `label`, `security_code` (re-encrypts; resets `failed_code_attempts = 0` and `is_code_blocked = False`), and `allow_env_panel`. Returns enriched public record.

### Auth Methods

**`validate_token(session, raw_token) → AgentGuestShare | None`**
Hashes the raw token and queries for a record that matches, is not revoked (`is_revoked == False`), and has not expired (`expires_at > now`). Returns `None` if no valid share found.

**`_find_share_by_token(session, raw_token) → AgentGuestShare | None`**
Same hash lookup without validity filters. Used to distinguish "token does not exist at all" (→ 404) from "token exists but is expired/revoked" (→ 410).

**`_create_guest_jwt(guest_share) → str`**
Computes `exp = min(now + 24h, share.expires_at)`. Encodes payload `{sub, role: "chat-guest", agent_id, owner_id, token_type: "guest_share", exp}` with `settings.SECRET_KEY` using the platform's standard `ALGORITHM`.

**`authenticate_anonymous(session, raw_token, security_code=None) → dict | None`**
Calls `validate_token`. If share is found, calls `_verify_security_code`. On success, calls `_create_guest_jwt` and returns `{access_token, token_type, guest_share_id, agent_id}`. Returns `None` if token does not exist at all; raises `ValueError` if expired/revoked or security code fails.

**`activate_for_user(session, raw_token, user_id, security_code=None) → dict | None`**
Validates token + security code. Upserts `GuestShareGrant` via `INSERT ... ON CONFLICT (uq_guest_share_grant_user_share) DO NOTHING`. Returns `{guest_share_id, agent_id, agent_name}`.

**`get_guest_share_info(session, raw_token) → dict`**
Returns `{agent_name, agent_description, is_valid, guest_share_id, requires_code, is_code_blocked, allow_env_panel}`. Always returns a dict (never raises). `requires_code` is `True` when `security_code_encrypted is not None`.

**`check_grant(session, user_id, guest_share_id) → bool`**
Verifies both the grant record exists and the parent share is still valid (not expired, not revoked). Used by session access control.

### Security Code Verification

**`_verify_security_code(session, guest_share, provided_code) → None`**
No-op if `security_code_encrypted is None` (backward compat). Raises `ValueError("This share link has been blocked...")` if `is_code_blocked`. Raises `ValueError("Security code is required")` if `provided_code` is `None`. Decrypts stored code and compares. On mismatch, increments `failed_code_attempts`; if `>= 3`, sets `is_code_blocked = True` and raises blocked error; otherwise raises with remaining attempts count.

---

## Auth Dependency Layer

Located in `backend/app/api/deps.py`.

### `GuestShareContext`

```python
class GuestShareContext(SQLModel):
    guest_share_id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID
    is_anonymous: bool  # True for role=chat-guest JWT, False for grant-based
```

### `get_current_user_or_guest(session, token) → User | GuestShareContext`

Inspects JWT payload. If `role == "chat-guest"` and `token_type == "guest_share"`, constructs and returns a `GuestShareContext` from the JWT claims. Otherwise falls through to standard user lookup and returns a `User`. Raises `HTTPException(401)` for invalid tokens.

### `CurrentUserOrGuest`

`Annotated[User | GuestShareContext, Depends(get_current_user_or_guest)]` — used in session, message, and file endpoints that support both authenticated users and guests.

---

## Frontend Components

### Route: `frontend/src/routes/guest/$guestShareToken.tsx`

**Auth State Machine:**
The `GuestChatPage` component drives a state machine through `authState`:
- `loading` — fetching share info
- `code_entry` — share requires code and no valid JWT in localStorage
- `authenticating` — calling auth/activate endpoint
- `ready` — auth succeeded, chat UI rendered
- `error` — terminal error (expired, blocked, invalid, network)

**Auth Flow Decision Logic:**
1. `GET /guest-share/{token}/info` (no auth) — loads share metadata
2. If `is_code_blocked` → error state
3. If valid guest JWT exists in `localStorage["access_token"]` (checked via `parseGuestJwt()`) → skip to `performAuth`
4. If `requires_code` and no valid JWT → `code_entry` state
5. Otherwise → `performAuth()` directly

**`performAuth(securityCode?)` logic:**
- Checks `localStorage` for an existing token
- If it's a guest JWT and not expired → restore from claims, set `ready`
- If it's a guest JWT but expired → clear localStorage, call anonymous auth
- If it's a regular user JWT → call `activate` endpoint; on 401/404 clear localStorage and call anonymous auth
- `authenticateAnonymous()` → calls `/auth` endpoint, stores returned JWT in `localStorage["access_token"]`

**Sub-components rendered in `ready` state:**
- `GuestChatHeader` — agent name/description, optional App panel toggle button (shown only when `allowEnvPanel && hasSession`)
- `GuestSessionSidebar` — lists sessions filtered by `guestShareId`; New Chat button; polls every 10 seconds
- `GuestNewChatState` — empty state with message input; creates session on first send via `SessionsService.createSession({ guest_share_id })`
- `GuestChatArea` — standard `MessageList` + `MessageInput` using `useSessionStreaming`; optionally renders `EnvironmentPanel`
- `SecurityCodeScreen` — 4-digit input with auto-advance, auto-submit, paste support; calls `performAuth(code)` on submit

**Session creation in `GuestNewChatState`:**
```
SessionsService.createSession({ agent_id, mode: "conversation", guest_share_id })
MessagesService.sendMessageStream({ sessionId, content })
```

### Component: `frontend/src/components/Agents/GuestShareCard.tsx`

Owner-facing card on the Integrations tab. Uses `GuestSharesService` from the generated client.

**TanStack Query keys:**
- `["guest-shares", agentId]` — list query

**Mutations:**
- `createShareMutation` — `GuestSharesService.createGuestShare`
- `deleteShareMutation` — `GuestSharesService.deleteGuestShare`
- `updateShareMutation` — `GuestSharesService.updateGuestShare`

**Local state:**
- `createDialogOpen`, `shareLabel`, `expirationHours` — create form
- `createdShareUrl`, `createdSecurityCode` — post-creation reveal
- `copiedUrl`, `copiedCode`, `copiedShareId` — copy feedback
- `editDialogOpen`, `editingShare`, `editLabel`, `editSecurityCode`, `editAllowEnvPanel` — edit form

**Share status logic:** `getShareStatus()` returns `"active" | "expired" | "revoked" | "blocked"`. Expired if `expires_at < now`. Revoked if `is_revoked`. Blocked if `is_code_blocked`.

**Expiration options:** 1 hour (1h), 24 hours (24h), 7 days (168h), 30 days (720h).

### Hook: `frontend/src/hooks/useGuestShare.tsx`

`GuestShareProvider` wraps the guest route and provides `{ isGuest: true, guestShareId, agentId, guestShareToken }` via React Context. Components can call `useGuestShare()` to detect whether they are running inside a guest session and adjust behavior (e.g., `EnvironmentPanel` passes `isGuest` down to hide credentials tab and redirect file viewer links to the guest route).

### Route: `frontend/src/routes/guest/file-viewer.tsx`

Standalone viewer for workspace files opened from the env panel in guest mode. Does not require platform login — uses whatever JWT is in `localStorage`. Search params: `envId` (environment ID), `path` (workspace-relative path). Renders `CSVViewer`, `MarkdownViewer`, `JSONViewer`, or `TextViewer` based on file extension; falls back to download-only for unsupported types.

---

## Security Model

| Concern | Mechanism |
|---------|-----------|
| Token secrecy | Plain token shown once on creation; stored as SHA-256 hash; comparison always against hash |
| Security code storage | Fernet symmetric encryption (`encrypt_field`/`decrypt_field`); `SECRET_KEY` from env |
| Brute-force protection | 3-attempt lockout; locked shares return 403 immediately without decryption |
| JWT scope | `role=chat-guest`, `token_type=guest_share` claims; `GuestShareContext` returned instead of `User` by `get_current_user_or_guest()` |
| JWT lifetime | Capped at 24h and never beyond share expiry; not refreshed |
| Session isolation | Sessions filtered by `guest_share_id`; guests cannot see or access sessions of other shares or owner's own sessions |
| Mode restriction | Building mode blocked at session route level for guest callers |
| Credential access | Credentials tab hidden in env panel for guests; credential values never exposed via guest APIs |
| Ownership gating | All owner CRUD endpoints verify `agent.owner_id == current_user.id` before any operation |
| Cascade on delete | Deleting a share (or agent, or owner) cascades correctly; sessions retain `guest_share_id = NULL` (SET NULL) |
| `is_revoked` note | Field exists in schema and is checked during token validation, but no current endpoint sets it to `true`; deletion is the active revocation path |

---

## Environment Variable

| Variable | Location | Purpose |
|----------|----------|---------|
| `SECRET_KEY` | `.env` | JWT signing for guest share tokens and Fernet encryption of security codes |
| `FRONTEND_HOST` | `.env` | Used to construct `share_url` in service responses |
