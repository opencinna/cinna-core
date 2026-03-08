# Webapp Chat Widget - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/agent_webapp_interface_config.py` — `chat_mode` field on `AgentWebappInterfaceConfig` (table), `AgentWebappInterfaceConfigBase`, `AgentWebappInterfaceConfigUpdate`, `AgentWebappInterfaceConfigPublic`
- `backend/app/models/session.py` — `webapp_share_id` nullable FK on `Session` table; also on `SessionCreate`, `SessionPublic`, `SessionPublicExtended`

### Backend - Routes

- `backend/app/api/routes/webapp_chat.py` — Chat API routes: session CRUD, messages, streaming, interrupt. Tag: `webapp-chat`. Thin controller with `_handle_chat_error()` pattern.
- `backend/app/api/routes/webapp_interface_config.py` — Interface config GET/PUT (includes `chat_mode` field). Tag: `webapp-interface-config`.
- `backend/app/api/main.py` — Both routers registered

### Backend - Services

- `backend/app/services/webapp_chat_service.py` — `WebappChatService` with chat validation, session management, and access verification. Exception hierarchy: `WebappChatError` (base), `WebappChatDisabledError`, `WebappChatSessionNotFoundError`, `WebappChatAccessDeniedError`.
- `backend/app/services/message_service.py` — `MessageService` provides shared streaming enrichment (`enrich_messages_with_streaming()`), interrupt orchestration (`interrupt_stream()`), and response building (`build_stream_response()`). These methods are reused by both webapp chat and regular session routes.
- `backend/app/services/agent_webapp_interface_config_service.py` — `AgentWebappInterfaceConfigService` with get-or-create, partial update, public read. Exception hierarchy: `InterfaceConfigError` (base), `AgentNotFoundError`, `AgentPermissionError`.

### Backend - Dependencies

- `backend/app/api/deps.py` — `WebappChatContext` (SQLModel with `webapp_share_id`, `agent_id`, `owner_id`), `get_webapp_chat_user()` dependency, `CurrentWebappChatUser` type alias

### Frontend

- `frontend/src/components/Webapp/WebappChatWidget.tsx` — Main chat widget: FAB icon, overlay panel, message list, input, streaming
- `frontend/src/components/Agents/WebappInterfaceModal.tsx` — Interface config modal with Chat Mode radio group (Disabled / Conversation / Building)
- `frontend/src/routes/webapp/$webappToken.tsx` — Renders `WebappChatWidget` when `chat_mode` is set

### Migrations

- `backend/app/alembic/versions/y5t6u7v8w9x0_replace_show_chat_with_chat_mode.py` — Replaces `show_chat` boolean with `chat_mode` VARCHAR(20) on `agent_webapp_interface_config`
- `backend/app/alembic/versions/z6u7v8w9x0y1_add_webapp_share_id_to_session.py` — Adds `webapp_share_id` FK to `session` table

### Tests

- `backend/tests/api/agents/agents_webapp_chat_test.py` — Chat session lifecycle, mode enforcement, access control tests
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

## API Endpoints

### Webapp Chat Routes (`backend/app/api/routes/webapp_chat.py`)

Prefix: `/api/v1/webapp/{token}/chat`

| Method | Path | Purpose |
|---|---|---|
| POST | `/sessions` | Create or get active chat session. Idempotent — returns existing if one exists. Session mode from interface config. |
| GET | `/sessions` | Get active chat session for this share. Returns null if none exists. |
| GET | `/sessions/{session_id}` | Get session details. Verifies `webapp_share_id` match. |
| GET | `/sessions/{session_id}/messages` | Get message history. Merges in-memory streaming events with persisted messages. |
| POST | `/sessions/{session_id}/messages/stream` | Send message and stream response via WebSocket. |
| POST | `/sessions/{session_id}/messages/interrupt` | Interrupt active streaming message. |

All endpoints require webapp-viewer JWT auth and validate that chat is enabled.

### Interface Config Routes (`backend/app/api/routes/webapp_interface_config.py`)

Prefix: `/api/v1/agents/{agent_id}/webapp-interface-config`

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Get or create interface config (includes `chat_mode`). |
| PUT | `/` | Update interface config (partial update via `exclude_unset`). |

## Services & Key Methods

### `WebappChatService` (`backend/app/services/webapp_chat_service.py`)

- `validate_chat_enabled()` — Checks `chat_mode` from interface config; raises `WebappChatDisabledError` if null; returns the chat_mode string
- `get_or_create_session()` — Finds active session by `webapp_share_id` or creates new one via `SessionService.create_session()`; delegates to `get_active_session()` internally to avoid query duplication
- `get_active_session()` — Returns most recent active session for a `webapp_share_id`, or None
- `verify_session_access()` — Fetches session by ID, verifies `webapp_share_id` match; raises `WebappChatSessionNotFoundError` or `WebappChatAccessDeniedError`

### `MessageService` (`backend/app/services/message_service.py`) — shared methods

These methods are shared between webapp chat routes and regular session routes (messages.py), eliminating duplication of streaming enrichment, interrupt orchestration, and response building logic.

- `enrich_messages_with_streaming()` — Merges in-memory streaming events from `active_streaming_manager` into message list; deduplicates by `event_seq`; patches accumulated content onto in-progress messages
- `interrupt_stream()` — Full interrupt orchestration: requests interrupt via `active_streaming_manager`, checks pending state, resolves environment, forwards interrupt to agent-env; raises `ValueError` if no active stream
- `build_stream_response()` — Builds standardized response dict from `SessionService.send_session_message()` result; handles `command_executed`, `streaming`, `pending`, and default actions

### `AgentWebappInterfaceConfigService` (`backend/app/services/agent_webapp_interface_config_service.py`)

- `get_or_create()` — Returns config for agent (creates with defaults if missing); verifies agent ownership
- `update()` — Partial update of config fields using `model_dump(exclude_unset=True)`; verifies agent ownership
- `get_by_agent_id()` — Returns `AgentWebappInterfaceConfigBase` for public endpoints (no auth); returns default instance if no record
- `_verify_agent_ownership()` — Reusable agent lookup + ownership check; `AgentPermissionError` returns 404 to avoid leaking existence
- `_get_or_create_config()` — Internal helper that gets or creates config record

## Frontend Components

- `WebappChatWidget.tsx` — Main widget: manages open/closed state, session lifecycle, message polling, streaming via WebSocket, interrupt. Sub-components: ChatFAB (floating button with unread badge), ChatOverlayPanel (slide-in panel with header, message list, input)
- `WebappInterfaceModal.tsx` — Radio group for chat mode (Disabled / Conversation / Building), replaces former show_chat toggle
- `$webappToken.tsx` — Conditionally renders `WebappChatWidget` when `interface_config.chat_mode` is set; passes token, mode, agent name

## Security

- Chat reuses webapp share JWT (`role: "webapp-viewer"`, `token_type: "webapp_share"`)
- `WebappChatContext` dependency extracts and validates JWT claims on every request
- Session access enforced via `webapp_share_id` match — viewers can only access sessions created from their share token
- Chat endpoints validate `chat_mode != null` on every request (not just session creation)
- Sessions run as `user_id = owner_id` — same security model as guest shares

---

*Last updated: 2026-03-08*
