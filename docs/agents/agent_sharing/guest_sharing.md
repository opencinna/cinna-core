# Guest Sharing

## Purpose

Allow agents to be shared with unauthenticated (or authenticated) users via time-limited, token-based URLs that give anyone instant access to chat with an agent in conversation mode — no signup or login required.

## Core Concepts

- **Guest share link** - A token-based URL that grants temporary chat access to an agent. The token is hashed (SHA256) before storage; only the hash is persisted for lookup
- **Security code** - Every guest share link is created with a random 4-digit code. Guests must provide the correct code before gaining access. After 3 failed attempts, the link is blocked
- **Two access paths** - Anonymous visitors receive a guest JWT (`role: "chat-guest"`); authenticated users receive a `GuestShareGrant` record (keeping their existing JWT)
- **Session ownership** - Guest sessions run in the agent owner's environment with `user_id = owner_id`. The `guest_share_id` field scopes guest sessions separately from the owner's own sessions
- **Bearer token model** - Anyone with the link and security code has access until expiration or revocation. Deleting a link immediately invalidates all access

### Distinction from Agent Sharing

| Aspect | Agent Sharing (`agent_share`) | Guest Sharing (`guest_share`) |
|--------|-------------------------------|-------------------------------|
| What is shared | Full agent clone (copied to recipient) | Temporary chat access via link |
| Recipient | Registered platform user (by email) | Anyone with the link (anonymous) |
| Requires account | Yes | No |
| Result | Independent agent copy | Ephemeral sessions owned by link creator |
| Lifetime | Permanent (until detached/deleted) | Time-limited (hours to days) |

## User Stories / Flows

### Creating a Guest Share Link (Owner)

1. Owner opens agent detail page, navigates to "Integrations" tab
2. In the Guest Share card, clicks "Create Guest Share Link"
3. Enters optional label (e.g., "Demo for client X") and selects expiration (1h, 24h, 7d, 30d)
4. System generates token, creates link with security code
5. Owner sees the share URL and security code — can copy both for distribution
6. Link appears in the list with label, expiration, session count, and status badge

### Guest Accessing a Shared Agent (Anonymous)

1. Guest opens the share URL: `/guest/{token}`
2. Frontend calls info endpoint to check validity
3. If link requires a security code → code entry screen shown
   - Auto-submits when all 4 digits are filled (first attempt only)
   - After failed attempt, auto-submit disabled; must press Enter or click Continue
4. Frontend calls auth endpoint → receives guest JWT
5. Guest JWT stored in localStorage, used for all API calls
6. Guest sees an empty chat page with a text input — ready to type immediately
7. On first message send: session is created on the backend, message is sent, URL updates with session ID
8. All guest chat uses compact conversation mode (simplified tool display, hidden thinking blocks)
9. Guest can view workspace files if the owner enabled `allow_env_panel` on the share link

### Authenticated User Accessing a Guest Share Link

1. Logged-in user opens the share URL
2. Frontend detects existing JWT in localStorage
3. After security code verification (if required), calls activate endpoint
4. Backend creates `GuestShareGrant` record (persistent access)
5. User keeps their existing JWT — no token replacement
6. User interacts with the agent via the guest chat UI using their existing session

### Managing Guest Share Links (Owner)

1. Owner views list of active guest share links with session counts
2. Can copy share link at any time (token is stored for re-access)
3. Can update label, set a new security code (resets block state), or toggle the "Show App panel" setting
4. Can delete a link — immediately invalidates all associated access

### Environment Panel Access

- By default, the "App" button (environment panel) is **hidden** for guest users
- Owner can enable it per share link via the "Show App panel" toggle in the edit dialog
- When enabled, guests can browse the agent's workspace files, scripts, logs, and docs (read-only)
- Credentials tab is always hidden for guests regardless of this setting

## Business Rules

### Link Lifecycle

- Links are valid from creation until `expires_at` or manual revocation
- Deleting a link CASCADE-removes all grants; sessions are preserved with `guest_share_id` SET NULL
- Status badges: Active (green), Expired (red), Revoked (gray)

### Security Code Rules

- Every new guest share link gets a random 4-digit code
- Code is encrypted (Fernet) at rest
- After 3 failed attempts, the link is blocked (`is_code_blocked = True`)
- Owner can view the current code via the management UI
- Owner can set a new code, which resets failed attempts and unblocks the link
- Legacy links without a code are exempt from verification (backward compatibility)

### JWT Rules

- Guest JWT lifetime capped at 24 hours (or share expiry, whichever is sooner)
- Guest JWT contains `role: "chat-guest"`, `guest_share_id`, `agent_id`, `owner_id`
- Backend validates guest share status on every request (not just JWT validity)
- Expired/revoked shares immediately block access even if JWT is still valid

### Grant Rules (Authenticated Users)

- One grant per user per guest share link (idempotent activation via `INSERT ... ON CONFLICT DO NOTHING`)
- User keeps their existing JWT — no replacement, multi-tab safe
- Owner can test own links without losing owner-level access
- Grants cascade-delete when the guest share link is deleted

### Access Control

| Action | Regular user | User with grant | Anonymous guest |
|--------|-------------|-----------------|-----------------|
| Own agents/sessions | Yes | Yes | No |
| Guest share sessions (create/list/get) | No | Yes (scoped) | Yes (scoped) |
| Guest share message send/stream | No | Yes (conversation only) | Yes (conversation only) |
| Workspace tree/download/view | Yes (own) | Yes (guest share's env) | Yes (guest share's env) |
| Agent config/update | Yes (own) | No | No |
| Credentials / environment management | Yes | No | No |
| Building mode | Yes (own) | No | No |
| Guest share management (CRUD) | Yes (owner) | No | No |

### Session Rules

- Guest sessions use `user_id = agent.owner_id` (runs in owner's environment)
- Sessions tagged with `guest_share_id` for scoping and owner visibility
- Owner sees all sessions (including guest sessions) in their normal session list
- Guests see only sessions matching their `guest_share_id`
- Conversation mode is enforced for all guest share sessions
- Sessions are created lazily — only when the guest sends their first message (not on page load)
- Compact conversation mode UI is always used (simplified tool display)

## Architecture Overview

```
Guest opens link: /guest/{token}
        │
GET /guest-share/{token}/info  (validity check)
        │
Has valid guest JWT? ──YES──► Skip code, go to chat
        │ NO
        │
Security code required? ──YES──► Code entry screen
        │ NO                              │
        ├──────────────────────────────────┘
        │
Has existing JWT (logged in)?
        │
  ┌─────┴─────────────────────────┐
  │ No (anonymous)                │ Yes (authenticated)
  │                               │
  │ POST /guest-share/            │ POST /guest-share/
  │   {token}/auth                │   {token}/activate
  │                               │
  │ → Guest JWT issued            │ → GuestShareGrant created
  │ → Store in localStorage       │ → Keep existing JWT
  └───────────────────────────────┘
        │
        ▼
  Guest sees empty chat with text input
        │
  Guest sends first message
        │
  Session created + message sent
        │
  URL updates with session ID
        │
  Guest chats in compact conversation mode
```

### Guest Chat UI Structure

```
GuestChatPage
├── GuestChatHeader (agent name, icon, description, optional App button)
├── GuestSessionSidebar (session list filtered by guest_share_id, New Chat button)
├── GuestNewChatState (when no session selected — text input, deferred session creation)
└── GuestChatArea (reuses MessageList in compact mode, MessageInput, optional EnvironmentPanel)
```

## Integration Points

- **[Agent Sharing](agent_sharing.md)** - Separate sharing mechanism for registered users (clone-based). Uses `agent_share` naming vs `guest_share` naming
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** - Guest sessions reuse the existing session infrastructure with `guest_share_id` scoping
- **[A2A Access Tokens](../../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md)** - Similar token-based access pattern for machine clients
- **[Authentication](../../application/auth/auth.md)** - Guest JWT extends the existing JWT infrastructure with a `chat-guest` role

## Error States

| State | Display |
|-------|---------|
| Loading | Bot icon + "Loading..." spinner |
| Security code entry | Bot icon + agent name + 4-digit code input |
| Authenticating | Bot icon + "Connecting to {agent}..." spinner |
| Invalid link | "This guest share link is invalid or has been removed." |
| Expired link | "This guest share link has expired. Contact the owner for a new link." |
| Link blocked | "This share link has been blocked due to too many failed attempts." |
| Generic error | "Something went wrong. Please try again." |
