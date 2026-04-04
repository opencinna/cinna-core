# Webapp Chat Widget - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/webapp/agent_webapp_interface_config.py` — `chat_mode` field on `AgentWebappInterfaceConfig` (table), `AgentWebappInterfaceConfigBase`, `AgentWebappInterfaceConfigUpdate`, `AgentWebappInterfaceConfigPublic`
- `backend/app/models/sessions/session.py` — `webapp_share_id` nullable FK on `Session` table; also on `SessionCreate`, `SessionPublic`, `SessionPublicExtended`; `page_context` optional field on `MessageCreate`

### Backend - Routes

- `backend/app/api/routes/webapp_chat.py` — Chat API routes: session CRUD, messages, streaming, interrupt. Tag: `webapp-chat`. Thin controller with `_handle_chat_error()` pattern.
- `backend/app/api/routes/webapp_interface_config.py` — Interface config GET/PUT (includes `chat_mode` field). Tag: `webapp-interface-config`.
- `backend/app/api/main.py` — Both routers registered

### Backend - Services

- `backend/app/services/webapp/webapp_chat_service.py` — `WebappChatService` with chat validation, session management, and access verification. Exception hierarchy: `WebappChatError` (base), `WebappChatDisabledError`, `WebappChatSessionNotFoundError`, `WebappChatAccessDeniedError`.
- `backend/app/services/sessions/message_service.py` — `MessageService` provides shared streaming enrichment (`enrich_messages_with_streaming()`), interrupt orchestration (`interrupt_stream()`), response building (`build_stream_response()`), and context-aware message dispatch (`collect_pending_messages` with diff logic). These methods are reused by both webapp chat and regular session routes.
- `backend/app/services/sessions/session_service.py` — `send_session_message` accepts `page_context: str | None` and stores it in `message_metadata` when creating the user `SessionMessage`.
- `backend/app/services/webapp/agent_webapp_interface_config_service.py` — `AgentWebappInterfaceConfigService` with get-or-create, partial update, public read. Exception hierarchy: `InterfaceConfigError` (base), `AgentNotFoundError`, `AgentPermissionError`.

### Backend - Dependencies

- `backend/app/api/deps.py` — `WebappChatContext` (SQLModel with `webapp_share_id`, `agent_id`, `owner_id`), `get_webapp_chat_user()` dependency, `CurrentWebappChatUser` type alias

### Frontend

- `frontend/src/components/Webapp/WebappChatWidget.tsx` — Main chat widget: FAB icon, overlay panel, message list, input, streaming, context collection
- `frontend/src/components/Agents/WebappInterfaceModal.tsx` — Interface config modal with Chat Mode radio group (Disabled / Conversation / Building)
- `frontend/src/routes/webapp/$webappToken.tsx` — Renders `WebappChatWidget` when `chat_mode` is set; declares `iframeRef` for context bridge

### Migrations

- `backend/app/alembic/versions/y5t6u7v8w9x0_replace_show_chat_with_chat_mode.py` — Replaces `show_chat` boolean with `chat_mode` VARCHAR(20) on `agent_webapp_interface_config`
- `backend/app/alembic/versions/z6u7v8w9x0y1_add_webapp_share_id_to_session.py` — Adds `webapp_share_id` FK to `session` table

### Tests

- `backend/tests/api/agents/agents_webapp_chat_test.py` — Chat session lifecycle, mode enforcement, access control, context storage, and context diff tests
- `backend/tests/api/agents/agents_webapp_interface_config_test.py` — Interface config lifecycle including `chat_mode` updates
- `backend/tests/utils/webapp_interface_config.py` — Test utility helpers for interface config

## Database Schema

### `agent_webapp_interface_config` table (modified)

| Column | Type | Description |
|---|---|---|
| `chat_mode` | VARCHAR(20), nullable, default NULL | Chat mode: `"conversation"`, `"building"`, or NULL (disabled). Replaces former `show_chat` boolean. |

### `session` table (modified)

| Column | Type | Description |
|---|---|---|
| `webapp_share_id` | UUID, nullable, FK -> `agent_webapp_share` SET NULL | Tracks which webapp share created this session. SET NULL on delete to preserve sessions. Mirrors `guest_share_id` pattern. |

### `session_message` table — no migration required

`page_context` is stored inside the existing `message_metadata` JSON column, which already exists on `SessionMessage`. No schema change was needed.

## API Endpoints

### Webapp Chat Routes (`backend/app/api/routes/webapp_chat.py`)

Prefix: `/api/v1/webapp/{token}/chat`

| Method | Path | Purpose |
|---|---|---|
| POST | `/sessions` | Create or get active chat session. Idempotent — returns existing if one exists. Session mode from interface config. |
| GET | `/sessions` | Get active chat session for this share. Returns null if none exists. |
| GET | `/sessions/{session_id}` | Get session details. Verifies `webapp_share_id` match. |
| GET | `/sessions/{session_id}/messages` | Get message history. Merges in-memory streaming events with persisted messages. |
| POST | `/sessions/{session_id}/messages/stream` | Send message and stream response via WebSocket. Accepts optional `page_context` field. |
| POST | `/sessions/{session_id}/messages/interrupt` | Interrupt active streaming message. |

All endpoints require webapp-viewer JWT auth and validate that chat is enabled.

### Interface Config Routes (`backend/app/api/routes/webapp_interface_config.py`)

Prefix: `/api/v1/agents/{agent_id}/webapp-interface-config`

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Get or create interface config (includes `chat_mode`). |
| PUT | `/` | Update interface config (partial update via `exclude_unset`). |

## Services & Key Methods

### `WebappChatService` (`backend/app/services/webapp/webapp_chat_service.py`)

- `validate_chat_enabled()` — Checks `chat_mode` from interface config; raises `WebappChatDisabledError` if null; returns the chat_mode string
- `get_or_create_session()` — Finds active session by `webapp_share_id` or creates new one via `SessionService.create_session()`; delegates to `get_active_session()` internally to avoid query duplication
- `get_active_session()` — Returns most recent active session for a `webapp_share_id`, or None
- `verify_session_access()` — Fetches session by ID, verifies `webapp_share_id` match; raises `WebappChatSessionNotFoundError` or `WebappChatAccessDeniedError`

### `MessageService` (`backend/app/services/sessions/message_service.py`) — shared methods

These methods are shared between webapp chat routes and regular session routes (`messages.py`), eliminating duplication of streaming enrichment, interrupt orchestration, and response building logic.

- `enrich_messages_with_streaming()` — Merges in-memory streaming events from `active_streaming_manager` into message list; deduplicates by `event_seq`; patches accumulated content onto in-progress messages
- `interrupt_stream()` — Full interrupt orchestration: requests interrupt via `active_streaming_manager`, checks pending state, resolves environment, forwards interrupt to agent-env; raises `ValueError` if no active stream
- `build_stream_response()` — Builds standardized response dict from `SessionService.send_session_message()` result; handles `command_executed`, `streaming`, `pending`, and default actions
- `collect_pending_messages()` — Builds agent-bound message content; contains context diff logic (see Context Management section)

### `AgentWebappInterfaceConfigService` (`backend/app/services/webapp/agent_webapp_interface_config_service.py`)

- `get_or_create()` — Returns config for agent (creates with defaults if missing); verifies agent ownership
- `update()` — Partial update of config fields using `model_dump(exclude_unset=True)`; verifies agent ownership
- `get_by_agent_id()` — Returns `AgentWebappInterfaceConfigBase` for public endpoints (no auth); returns default instance if no record
- `_verify_agent_ownership()` — Reusable agent lookup + ownership check; `AgentPermissionError` returns 404 to avoid leaking existence
- `_get_or_create_config()` — Internal helper that gets or creates config record

## Frontend Components

- `WebappChatWidget.tsx` — Main widget: manages open/closed state, session lifecycle, message polling, streaming via WebSocket, interrupt, localStorage persistence, and context collection. Key internal functions:
  - `loadExistingSession(isBackgroundVerify)` — fetches or verifies the active session; when `isBackgroundVerify=true`, skips the loading spinner and does not clear the cache on no-session
  - `loadMessages(sid, silent)` — fetches message history; when `silent=true`, skips the `isLoadingMessages` spinner (used during background verify)
  - `refreshMessages()` — convenience wrapper calling `loadMessages` without silent flag (used after stream completes)
  - `collectIframeContext(iframeRef)` — sends a `postMessage` to the webapp iframe and awaits a `page_context_response` (500ms timeout, returns null on timeout)
  - `buildPageContext(iframeRef)` — orchestrates context collection: captures `window.getSelection()` (selected text, truncated at 2,000 chars) and calls `collectIframeContext`; returns a serialized JSON string or `undefined` if no context available
  - Sub-components: ChatFAB (floating button with unread badge), ChatOverlayPanel (slide-in panel with header, message list, input)
- `WebappInterfaceModal.tsx` — Radio group for chat mode (Disabled / Conversation / Building), replaces former show_chat toggle
- `$webappToken.tsx` — Conditionally renders `WebappChatWidget` when `interface_config.chat_mode` is set; passes token, mode, agent name, and `iframeRef`. The `iframeRef` (`useRef<HTMLIFrameElement>(null)`, declared before any conditional returns to satisfy Rules of Hooks) is attached to the webapp `<iframe>` element and passed to the chat widget so it can collect schema.org context via postMessage.

## localStorage Cache

`WebappChatWidget.tsx` caches the chat session in localStorage for seamless page-refresh persistence.

**Key**: `webapp_chat_{webappToken}` (e.g., `webapp_chat_abc123`)

**Value schema**:
```typescript
interface WebappChatCache {
  sessionId: string       // UUID of the active session
  messages: MessagePublic[] // Persisted messages (not streaming events)
  cachedAt: number        // Unix ms timestamp (for future TTL use)
}
```

**Cache helpers** (module-level pure functions):
- `getCacheKey(webappToken)` — returns the localStorage key
- `readCache(webappToken)` — reads and validates shape; returns `null` on any error
- `writeCache(webappToken, sessionId, messages)` — serializes and stores; silent on quota errors
- `clearCache(webappToken)` — removes the entry; silent on storage errors

**Lifecycle**:
1. On mount: `readCache()` called; if hit, sets `sessionId` + `messages` in state immediately, marks `needsBackgroundVerifyRef = true`, and sets `cacheRestoredRef = true` to prevent the effect from running a second time if `webappToken` ever changes identity
2. Background verify: fires once when `sessionId` becomes non-null from cache (guarded by `needsBackgroundVerifyRef` and `backgroundVerifyDoneRef`); calls `loadExistingSession(true)` which passes `isBackgroundVerify=true`, suppressing the loading spinner via `loadMessages(sid, silent=true)` and preserving cached state if the session is not found or the request fails
3. If background verify finds no active session: cached state is preserved (no `clearCache()`, no state reset). Only an explicit (non-background) `loadExistingSession(false)` call will clear the cache and reset state when no session exists
4. Cache write: `useEffect` on `[sessionId, messages, webappToken]` calls `writeCache()` on every state change
5. Incognito / new window: isolated localStorage gives a fresh session automatically

## Context Management

Context management covers the full lifecycle of page context data: collection on the frontend, transmission in the message payload, storage in the backend, injection into agent-bound content, and the diff optimization that minimizes context overhead across turns.

For full technical details, see the dedicated aspect document: **[Context Management Tech](webapp_chat_context_tech.md)**. For business logic and user flows, see **[Context Management](webapp_chat_context.md)**.

## Agent-to-Webapp Action Framework

The action framework enables agents to trigger webapp UI actions by embedding `<webapp_action>` XML tags in their responses. Tags are parsed mid-stream, emitted as WebSocket events, and stripped from the persisted message.

For full technical details (tag parsing, mid-stream scanning, WebSocket event structure, frontend handling, context-bridge.js dispatcher), see the dedicated aspect document: **[Actions Tech](webapp_chat_actions_tech.md)**. For business logic and user flows, see **[Actions](webapp_chat_actions.md)**.

## Socket.IO Connection for Webapp Viewers

Webapp viewers connect to Socket.IO independently from authenticated users. The connection is established in `$webappToken.tsx` via a `useEffect` that fires once `authState === "ready"` and `chatMode` is non-null:

```typescript
// Connect using webapp_share_id (from JWT sub) as user identifier
eventService.connect(claims.sub)
```

**Important**: The `sub` claim in the webapp-viewer JWT holds the `webapp_share_id` UUID (not the owner's user_id). This means:
- The viewer joins Socket.IO room `user_{webapp_share_id}` — not the owner's user room
- The viewer subscribes to `session_{session_id}_stream` room when a chat session starts to receive streaming events
- `session_interaction_status_changed` events for the viewer must be broadcast to the session stream room, not the owner's user room

The connection is torn down (`eventService.disconnect()`) when the page component unmounts.

## Session Stream Room Event Routing

All four `SessionService` stream event handlers emit `session_interaction_status_changed` to **two** destinations:

1. `user_{owner_id}` room — for authenticated owner sessions viewing in the regular app
2. `session_{session_id}_stream` room — for webapp viewers subscribed to this room

```python
# Both rooms receive the same payload
await event_service.emit_event(
    event_type="session_interaction_status_changed",
    model_id=UUID(session_id),
    meta=status_meta,
    user_id=user_id,          # → user_{owner_id} room
)
await event_service.emit_event(
    event_type="session_interaction_status_changed",
    model_id=UUID(session_id),
    meta=status_meta,
    room=f"session_{session_id}_stream",  # → webapp viewer room
)
```

This dual emission applies to all handlers: `handle_stream_started`, `handle_stream_completed`, `handle_stream_error`, and `handle_stream_interrupted`.

## Stream Completed Fallback in WebappChatWidget

`WebappChatWidget.tsx` subscribes to both `session_interaction_status_changed` (via `eventService.subscribe`) and raw `stream_event` messages from the session stream room. When a `stream_event` arrives with `event_type === "stream_completed"`, the widget also clears its streaming state. This acts as a fallback in case the `session_interaction_status_changed` event is delayed or missed, providing more resilient UX.

## Widget Dimensions

- Width: `w-[460px]` (460px, capped at `calc(100vw - 2rem)` on small screens)
- Height: `min(600px, calc(100vh - 6rem))`

## Security

- Chat reuses webapp share JWT (`role: "webapp-viewer"`, `token_type: "webapp_share"`)
- `WebappChatContext` dependency extracts and validates JWT claims on every request
- Session access enforced via `webapp_share_id` match — viewers can only access sessions created from their share token
- Chat endpoints validate `chat_mode != null` on every request (not just session creation)
- Sessions run as `user_id = owner_id` — same security model as guest shares
- The session stream room (`session_{id}_stream`) is joinable by anyone with a valid Socket.IO connection who knows the session ID. This is acceptable because: (a) session IDs are UUIDs with 122 bits of entropy, (b) the room only carries streaming content events and status changes, not sensitive session data, and (c) the webapp viewer already has access to session content via the chat API endpoints.

---

*Last updated: 2026-03-08 — extracted Agent-to-Webapp Action Framework to separate aspect docs*
