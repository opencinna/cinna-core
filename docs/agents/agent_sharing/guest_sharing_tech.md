# Guest Sharing - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/agent_guest_share.py` - `AgentGuestShare`, `GuestShareGrant`, `AgentGuestShareBase`, `AgentGuestShareCreate`, `AgentGuestShareUpdate`, `AgentGuestSharePublic`, `AgentGuestShareCreated`, `AgentGuestSharesPublic`, `GuestShareTokenPayload`
- `backend/app/models/session.py` - Added `guest_share_id` field on `Session`, `SessionCreate`, `SessionPublic`, `SessionPublicExtended`

### Backend - Routes

- `backend/app/api/routes/guest_shares.py` - Owner management router (prefix: `/agents/{agent_id}/guest-shares`) + guest auth router (prefix: `/guest-share`)

### Backend - Services

- `backend/app/services/agent_guest_share_service.py` - `AgentGuestShareService` (token CRUD, validation, auth, grants)
- `backend/app/services/session_service.py` - `create_session()` accepts `guest_share_id` parameter

### Backend - Auth Layer

- `backend/app/api/deps.py` - `GuestShareContext`, `get_current_user_or_guest()`, `CurrentUserOrGuest` dependency

### Backend - Modified Routes (Guest Access Checks)

- `backend/app/api/routes/sessions.py` - Uses `CurrentUserOrGuest`, guest share access checks on create/list/get, `_verify_session_access()` helper
- `backend/app/api/routes/messages.py` - Uses `CurrentUserOrGuest`, guest share access checks on all message endpoints, `_verify_message_access()` helper
- `backend/app/api/routes/workspace.py` - Uses `CurrentUserOrGuest`, `_verify_workspace_read_access()` helper for tree/download/view

### Frontend - Owner UI

- `frontend/src/components/Agents/GuestShareCard.tsx` - Owner management card (create, list, copy link, delete)
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` - Renders GuestShareCard in the integrations grid

### Frontend - Guest Chat

- `frontend/src/routes/guest/$guestShareToken.tsx` - Public guest chat page (not under `_layout/`)
- `frontend/src/hooks/useGuestShare.tsx` - `GuestShareProvider` context + `useGuestShare` hook

### Migrations

- `backend/app/alembic/versions/f53ac2dee553_add_agent_guest_share_tables.py` - Creates `agent_guest_share` and `guest_share_grant` tables, adds `session.guest_share_id`
- `backend/app/alembic/versions/w3r4s5t6u7v8_add_security_code_to_guest_shares.py` - Adds `security_code_encrypted`, `failed_code_attempts`, `is_code_blocked` to `agent_guest_share`
- `backend/app/alembic/versions/x4s5t6u7v8w9_add_allow_env_panel_to_guest_shares.py` - Adds `allow_env_panel` to `agent_guest_share`

### Tests

- `backend/tests/api/agents/guest_shares_test.py` - Owner CRUD tests
- `backend/tests/api/agents/guest_shares_auth_test.py` - Anonymous auth + grant activation tests
- `backend/tests/api/agents/guest_shares_sessions_test.py` - Session access control tests
- `backend/tests/api/agents/guest_shares_security_code_test.py` - Security code verification tests
- `backend/tests/utils/guest_share.py` - Test utilities for guest share creation

## Database Schema

### agent_guest_share Table

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| `agent_id` | UUID, FK → agent CASCADE | Agent this link gives access to |
| `owner_id` | UUID, FK → user CASCADE | User who created the link |
| `label` | string (max 255), nullable | Optional description |
| `token_hash` | string, not null | SHA256 hash of the bearer token (lookup key) |
| `token_prefix` | string (max 12) | First 8 chars for display |
| `token` | string, nullable | Raw token stored for owner re-access (share URL reconstruction) |
| `expires_at` | datetime, not null | Expiration time |
| `created_at` | datetime | |
| `is_revoked` | boolean, default False | Manual revocation flag |
| `security_code_encrypted` | string, nullable | Fernet-encrypted 4-digit code |
| `failed_code_attempts` | integer, default 0 | Failed code entry attempts |
| `is_code_blocked` | boolean, default False | True after 3 failed attempts |
| `allow_env_panel` | boolean, default False | Whether guests can access the environment panel |

**Indexes:** `agent_id`, `owner_id`, `token_hash`

### guest_share_grant Table

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| `user_id` | UUID, FK → user CASCADE | Authenticated user who activated the link |
| `guest_share_id` | UUID, FK → agent_guest_share CASCADE | Which guest share was activated |
| `activated_at` | datetime | When the grant was created |

**Constraints:** `UNIQUE(user_id, guest_share_id)` — one grant per user per link, idempotent. CASCADE on `guest_share_id` — deleting the share removes all grants.

### session Table (Modified)

- `guest_share_id` (UUID, nullable, FK → agent_guest_share SET NULL) - Tracks which guest share created this session. SET NULL on delete to preserve sessions.

## API Endpoints

### Owner Management Router

Prefix: `/api/v1/agents/{agent_id}/guest-shares`, tags: `guest-shares`

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/` | Create guest share link (returns token, share_url, security_code) | CurrentUser (owner) |
| `GET` | `/` | List guest share links (with session counts, decrypted security codes) | CurrentUser (owner) |
| `GET` | `/{guest_share_id}` | Get single guest share (with decrypted security code) | CurrentUser (owner) |
| `PUT` | `/{guest_share_id}` | Update label, security code, and/or allow_env_panel | CurrentUser (owner) |
| `DELETE` | `/{guest_share_id}` | Delete guest share link | CurrentUser (owner) |

### Guest Auth Router

Prefix: `/api/v1/guest-share`, tags: `guest-share`

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/{token}/auth` | Authenticate anonymous → guest JWT (accepts `security_code`) | None |
| `POST` | `/{token}/activate` | Activate grant for logged-in user (accepts `security_code`) | CurrentUser |
| `GET` | `/{token}/info` | Public info (agent name, validity, `requires_code`, `is_code_blocked`, `allow_env_panel`) | None |

### Modified Endpoints

**Sessions** (`/api/v1/sessions`):
- `POST /` - Accepts optional `guest_share_id`, validates guest share context, forces conversation mode
- `GET /` - Accepts optional `guest_share_id` query param for filtering; auto-filters for anonymous guests
- `GET /{id}` - Verifies guest share access via `_verify_session_access()`

**Messages** (`/api/v1/sessions/{session_id}/messages`):
- All message endpoints use `CurrentUserOrGuest` and verify access via `_verify_message_access()`

**Workspace** (`/api/v1/environments/{env_id}/workspace`):
- `GET /tree`, `GET /download/{path}`, `GET /view-file/{path}` - Use `CurrentUserOrGuest` and `_verify_workspace_read_access()`

## Services & Key Methods

### AgentGuestShareService (`backend/app/services/agent_guest_share_service.py`)

All methods are `@staticmethod`:

- `create_guest_share()` - Generate token via `secrets.token_urlsafe(32)`, hash with SHA256, generate 4-digit security code, encrypt with Fernet, create DB record
- `list_guest_shares()` - Query by `agent_id` with session_count subquery, ordered by `created_at` DESC; includes share_url and decrypted security_code
- `get_guest_share()` - Single share with session count, share_url, decrypted security_code
- `update_guest_share()` - Update label, security code, and/or `allow_env_panel`; new code resets `failed_code_attempts` and `is_code_blocked`
- `delete_guest_share()` - Ownership check, delete record (CASCADE removes grants, SET NULL on sessions)
- `validate_token()` - Hash lookup, check not revoked, check not expired
- `authenticate_anonymous()` - Validate token → verify security code → issue guest JWT (capped at 24h)
- `activate_for_user()` - Validate token → verify security code → UPSERT grant via `INSERT ... ON CONFLICT DO NOTHING`
- `get_guest_share_info()` - Public info: agent name, description (first 200 chars), validity, requires_code, is_code_blocked, allow_env_panel
- `check_grant()` - Query grant for (user_id, guest_share_id), verify parent share still valid
- `_hash_token()` - SHA256 helper
- `_verify_security_code()` - Decrypt stored code, compare, track failed attempts, block after 3 failures
- `_create_guest_jwt()` - Build JWT with `role: "chat-guest"`, `sub: guest_share_id`, capped lifetime
- `_find_share_by_token()` - Find share by token without validity checks (for error differentiation)

### Auth Layer (`backend/app/api/deps.py`)

- `GuestShareContext` - SQLModel with `guest_share_id`, `agent_id`, `owner_id`, `is_anonymous`, `user_id`
- `get_current_user_or_guest()` - Decodes JWT; returns `User` for regular tokens (`role: "user"`), `GuestShareContext` for guest tokens (`role: "chat-guest"`)
- `CurrentUserOrGuest` - Annotated type alias for the dependency
- Existing `get_current_user()` and `CurrentUser` remain unchanged

### Access Verification Helpers

- `sessions.py:_verify_session_access()` - Checks session ownership, guest share ID match, or grant existence
- `messages.py:_verify_message_access()` - Same logic as session access
- `workspace.py:_verify_workspace_read_access()` - Checks agent ownership or any grant for the agent

## Frontend Components

### GuestShareCard (`frontend/src/components/Agents/GuestShareCard.tsx`)

Located in Agent Integrations tab. Features:
- List active guest share links with label, expiration, session count, status badges (Active/Expired/Revoked)
- Create dialog with label input + expiration selector (1h, 24h, 7d, 30d)
- Token display dialog on creation with copy button
- Copy share link button on each active share (owner can re-copy at any time)
- Edit dialog with label, security code, and "Show App panel" toggle (`allow_env_panel`)
- Delete button with AlertDialog confirmation
- React Query: query key `["guest-shares", agentId]`, mutations for create/delete/update

### Guest Chat Page (`frontend/src/routes/guest/$guestShareToken.tsx`)

Public route (not under `_layout/`). Component hierarchy:
- `GuestChatPage` → `GuestChatHeader`, `GuestSessionSidebar`, `GuestNewChatState`, `GuestChatArea`
- Reuses `MessageList` (compact mode), `MessageInput`, `EnvironmentPanel` (conditional), `useSessionStreaming` from existing chat UI
- Session list polls every 10s, filtered by `guest_share_id`
- **Deferred session creation**: No session is created until the guest sends their first message. `GuestNewChatState` shows a text input; on send, creates session + sends message + navigates to session URL
- **Compact mode**: All guest chat uses `conversationModeUi="compact"` — tool calls shown in simplified view, thinking blocks hidden
- **Environment panel**: "App" button and `EnvironmentPanel` only rendered when `allow_env_panel` is true on the share link

### Guest Share Context (`frontend/src/hooks/useGuestShare.tsx`)

`GuestShareProvider` wraps the guest chat page, providing:
- `isGuest: boolean` - Whether in guest context
- `guestShareId: string | null` - Current guest share ID
- `agentId: string | null` - Agent being accessed
- `guestShareToken: string | null` - Token from URL

Used by child components to detect guest context and hide restricted UI elements.

## Configuration

- Token generation: `secrets.token_urlsafe(32)` (256-bit entropy)
- Token hashing: SHA256 for storage and lookup
- Security code encryption: Fernet (symmetric encryption)
- Guest JWT lifetime: min(24 hours, share expiry)
- Max failed code attempts: 3 (then blocked)
- Expiration options: 1h, 24h, 7d, 30d
- `allow_env_panel`: boolean, default `false` — controls whether the "App" button and environment panel are visible to guests
- Conversation mode UI: always `compact` for guest sessions (simplified tool display)

## Security

- **Token security** - SHA256 hash stored for lookup; raw token also stored for owner re-access only
- **JWT validation** - Backend validates guest share status on every request, not just JWT validity
- **Security code** - Fernet-encrypted at rest, 3-attempt limit with blocking
- **Grant isolation** - Authenticated users keep their JWT; no token replacement, multi-tab safe
- **Scope restrictions** - Guests cannot access credentials, building mode, agent config, environment management, database endpoints
- **Session scoping** - `guest_share_id` field ensures guests only see their own sessions
- **Immediate revocation** - Deleting a guest share link immediately blocks all access

## Error Handling

| Scenario | HTTP Status | Detail |
|----------|-------------|--------|
| Token not found | 404 | "Guest share not found" |
| Token expired or revoked | 410 | "Guest share link has expired or been revoked" |
| Invalid JWT format | 403 | "Could not validate credentials" |
| Guest share ID mismatch | 403 | "Guest share ID mismatch" |
| Agent ID mismatch | 403 | "Agent ID mismatch with guest share" |
| Building mode requested | 400 | "Guest share sessions only support conversation mode" |
| Security code required | 403 | "Security code is required" |
| Incorrect security code | 403 | "Incorrect security code. N attempt(s) remaining." |
| Link blocked | 403 | "This share link has been blocked due to too many failed attempts." |

**Concurrency patterns:**
- Grant UPSERT: `INSERT ... ON CONFLICT DO NOTHING` for idempotent grant creation
- Session count: Computed on read via subquery (not cached)
- Token deletion: CASCADE handles grant cleanup atomically
