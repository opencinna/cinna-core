# Guest Sharing

## Purpose

Allows an agent owner to share their agent with anonymous or lightly-authenticated external viewers through a disposable URL. The recipient follows the link, optionally enters a 4-digit security code, and immediately starts a conversation with the agent — no account required. The owner can create multiple independent links (for different clients, demos, or use-cases), see how many sessions each has produced, and delete any link to cut off access immediately.

Guest sharing is distinct from the bundle/install model: there is no copy of the agent involved. The guest always talks to the owner's live environment. It is also distinct from webapp sharing, which exposes a data dashboard rather than a chat session.

## Core Concepts

### Guest Share Token

A URL-safe random token (`secrets.token_urlsafe(32)`) generated once at creation time. The full token is stored in the `AgentGuestShare.token` column and also shown in the response body — **this is the only time the plain token is returned**. Subsequent API calls return only the first 8 characters (`token_prefix`) for identification purposes.

When a guest visits `/guest/{token}`, the frontend calls `GET /api/v1/guest-share/{token}/info` to fetch share metadata, then `POST /api/v1/guest-share/{token}/auth` to obtain a guest JWT.

Tokens are looked up by SHA-256 hash (`token_hash`), so the plain token is never compared in a database query.

### Security Code

Every guest share is assigned a random 4-digit numeric code at creation time. The code is stored encrypted (`security_code_encrypted` using Fernet symmetric encryption via `encrypt_field`/`decrypt_field`) and is shown plaintext to the owner both on creation and in the share management card.

Owners distribute the code out-of-band (separately from the URL). When the recipient opens the link, the frontend detects `requires_code: true` from the info endpoint and presents a 4-digit entry screen before authenticating.

**Lockout policy:** After 3 consecutive wrong attempts, `is_code_blocked` is set to `true` and the link becomes permanently blocked. The owner can unblock by setting a new security code via the edit dialog, which also resets `failed_code_attempts` to 0.

The code can be updated (never deleted) by the owner at any time.

### Environment Panel Allow-Flag

Each share carries an `allow_env_panel` boolean (default `false`). When enabled, the guest chat page shows an **App** button in the header that opens the agent's environment panel — a read-only view of the workspace filesystem. The credentials tab and database viewer are hidden for guests regardless of this setting.

The flag can be toggled after creation via the edit dialog.

### Expiration

Created with an `expires_in_hours` value (1 to 720 hours; default 24). The expiration timestamp (`expires_at`) is immutable after creation. An expired link is treated identically to a deleted one from the guest's perspective, but the DB record is preserved so the owner can see it in the list with an "Expired" badge.

The guest JWT issued on authentication is capped at 24 hours but never extends past the share's own `expires_at`.

### Chat-Only Scope

Guest sessions are always created in **conversation mode**. Building mode is blocked at the session route level. Guests see only the sessions that were started through their specific share token (`guest_share_id` scoping). The owner's own sessions are not visible to guests.

Guest sessions run under `user_id = agent.owner_id`, so they execute in the owner's active environment and consume the owner's compute quota.

### Grant Activation (Authenticated Users)

If a logged-in user visits a guest share link, the frontend calls `POST /api/v1/guest-share/{token}/activate` instead. This creates a `GuestShareGrant` record (idempotent via `ON CONFLICT DO NOTHING`) and returns the `agent_id` without issuing an anonymous JWT. The authenticated user's regular JWT is then used for subsequent requests. This allows team members with accounts to access the link without going through anonymous auth.

## User Stories

### 1. Owner Creates a Guest Share

1. Owner opens the agent's **Integrations** tab.
2. Scrolls to the **Guest Share Links** card and clicks **New**.
3. Optionally enters a descriptive label (e.g., "Demo for Acme Corp") and selects an expiration duration (1 hour / 24 hours / 7 days / 30 days).
4. Clicks **Create Link**.
5. The dialog switches to a confirmation view showing the full share URL and the 4-digit security code displayed as large digit tiles. Both can be copied.
6. Owner shares the URL with the recipient via one channel (e.g., email) and the code via a separate channel (e.g., a message or phone call).
7. Owner closes the dialog. The new share appears in the list with an **Active** badge, session count (0), and time-remaining indicator.

### 2. Guest Opens the Link

1. Guest visits the shared URL: `/guest/{token}`.
2. The page fetches share info (agent name/description, `requires_code`, validity).
3. If the share is invalid or expired, an error screen is shown immediately.
4. If `requires_code` is `true`, a 4-digit entry screen is presented. The guest types the code digit-by-digit (auto-submit on completion).
5. On success, the frontend calls the anonymous auth endpoint, receives a guest JWT, and stores it in `localStorage["access_token"]`.
6. The guest chat page renders: a sidebar showing previous sessions for this share, and a new-chat area.
7. The guest types a first message. A new session is created and the message is sent.

### 3. Guest Returns / Refreshes Page

On page refresh, the frontend checks `localStorage["access_token"]` for an existing guest JWT that is still valid (not expired). If found, the auth flow is skipped and the user proceeds directly to the chat page. If the JWT has expired, anonymous auth is re-run (which may prompt for the security code again if `requires_code` is still `true`).

### 4. Authenticated User Activates the Link

1. A logged-in user visits the share URL.
2. The page detects a non-guest JWT in `localStorage`.
3. `POST /api/v1/guest-share/{token}/activate` is called with the user's JWT.
4. A `GuestShareGrant` record is created (or silently ignored if already existing).
5. The user proceeds to the chat page using their normal authenticated identity.

### 5. Owner Manages Shares

- **Copy link** — copies the share URL to clipboard directly from the list row (only for active shares).
- **Edit** — opens a dialog to update the label, set a new security code (which also unblocks a blocked link), or toggle `allow_env_panel`.
- **Delete** — destroys the `AgentGuestShare` record permanently. Sessions created through the deleted share retain a `guest_share_id = NULL` (SET NULL on delete) and are not deleted. The link immediately stops working.

### 6. Owner Revokes Access

Deleting the share is the revocation mechanism. There is no separate revoke action. `is_revoked` exists as a field in the model but is never set to `true` by any current endpoint — deletion is the active path.

## Business Rules

- **Token format**: URL-safe base64, 32 bytes of randomness (`secrets.token_urlsafe(32)`), stored as SHA-256 hash. Token prefix is first 8 characters.
- **Expiration range**: 1 to 720 hours at creation. Immutable after creation.
- **Security code format**: Exactly 4 decimal digits, randomly generated at creation. Validated by regex `^\d{4}$` on update.
- **Lockout**: 3 wrong security code attempts → `is_code_blocked = true`. Unblocked only by owner setting a new code.
- **Guest JWT lifetime**: `min(now + 24h, share.expires_at)`.
- **Guest JWT claims**: `role = "chat-guest"`, `token_type = "guest_share"`, `sub = guest_share_id`, `agent_id`, `owner_id`.
- **Session ownership**: `user_id = agent.owner_id` on all guest-created sessions.
- **Mode restriction**: Guest sessions forced to `conversation` mode; building mode blocked at route level.
- **Session scoping**: Guests see only sessions tagged with their `guest_share_id`.
- **What guests cannot do**: Access agent configuration, manage credentials, view credential values, use building mode, invoke slash commands (autocomplete is inactive without `sessionId` context), access the database viewer in the env panel.
- **What guests can do with env panel (if enabled)**: View and download workspace files.
- **Ownership requirement**: Only the agent owner can create, list, update, or delete shares for an agent.
- **Multiple shares**: An agent can have any number of concurrent guest shares (different clients, different expiration windows, different security codes).
- **Token reveal**: The full token is returned exactly once — in the `AgentGuestShareCreated` response body on creation. It cannot be retrieved again. The `share_url` is reconstructed from `token` when the token is stored and shown in list responses.

## Architecture Overview

```
Agent Owner → Integrations Tab → GuestShareCard
                                      │
                                      ↓
                           POST /agents/{id}/guest-shares
                                      │
                                      ↓
                          AgentGuestShareService.create_guest_share()
                          generates token, encrypts security code,
                          hashes token for storage
                                      │
                                      ↓
                          agent_guest_share table
                                      │
Guest → /guest/{token} ───────────────┘
             │
             ↓
    GET /guest-share/{token}/info   (no auth)
             │
    (optional) Security code entry screen
             │
    POST /guest-share/{token}/auth  (no auth)
             │
             ↓
    JWT: role=chat-guest, sub=guest_share_id
    stored in localStorage["access_token"]
             │
             ↓
    POST /sessions  (guest JWT)  →  Session.guest_share_id = share.id
                                    Session.user_id = agent.owner_id
             │
             ↓
    Chat via standard session endpoints (CurrentUserOrGuest dep)
```

## Integration Points

- **Authentication** — Guest JWT with `role=chat-guest` is a distinct token type decoded by `get_current_user_or_guest()` in `deps.py`, returning a `GuestShareContext` rather than a `User`. Session and message endpoints accept `CurrentUserOrGuest`. See [Auth](../../application/auth/auth.md)
- **Agent Sessions** — Sessions created by guests carry `guest_share_id`; `user_id` is set to the agent owner's ID. Building mode is blocked for these sessions. The sessions endpoint filters by `guest_share_id` when a guest caller is detected. See [Agent Sessions](../../application/agent_sessions/agent_sessions.md)
- **Chat Windows** — The guest share page (`/guest/$guestShareToken`) is one of three chat-window hosting contexts, alongside the authenticated session page and the webapp widget. It wraps the standard `MessageList` + `MessageInput` components inside `GuestShareProvider`. See [Chat Windows](../../application/chat_interface/chat_windows.md)
- **Environment Panel** — When `allow_env_panel` is true, the guest page renders `EnvironmentPanel` in read-only guest mode: workspace tree and file download work, credentials tab and database viewer are hidden. See [App Env Panel Widget](../../application/agent_sessions/app_env_panel_widget.md)
- **File Management** — Guest file viewer uses a dedicated standalone route (`/guest/file-viewer`) to open files from the env panel without requiring platform login. See [Agent File Management](../agent_file_management/agent_file_management.md)
- **Agent Webapp** — Webapp shares and guest shares are independent; both can coexist on the same agent install, serving different access purposes. See [Agent Webapp](../agent_webapp/agent_webapp.md)
- **Credential Security Hardening** — `SecurityEvent` records include a nullable `guest_share_id` FK, enabling per-share security audit queries. See [Credential Security Hardening](../agent_credentials/credential_security_hardening.md)
- **Agent Management** — The **Guest Share Links** card lives on the agent's Integrations tab alongside A2A tokens, MCP Connectors, Email, Webhooks, and Webapp Share. Visible to owner only. See [Agent Management](../../application/agent_management/agent_management.md)
