# Agent Sessions — Technical Reference

## File Locations

### Backend — Models
- `backend/app/models/session.py` — `Session`, `SessionMessage`, `SessionCreate`, `SessionUpdate`, `SessionPublic`, `SessionPublicExtended`, `SessionsPublicExtended`, `MessageCreate`, `MessagePublic`, `MessagesPublic`

### Backend — Routes
- `backend/app/api/routes/sessions.py` — Session CRUD endpoints
- `backend/app/api/routes/messages.py` — Message send, stream, interrupt, and status endpoints
- `backend/app/api/main.py` — Router registration (tag: `sessions`, `messages`)

### Backend — Services
- `backend/app/services/session_service.py` — All session lifecycle logic, streaming orchestration, environment activation
- `backend/app/services/message_service.py` — Message creation, streaming from agent-env, incremental DB flush
- `backend/app/services/active_streaming_manager.py` — In-memory streaming state tracker (singleton)
- `backend/app/services/event_service.py` — WebSocket event emission to `session_{id}_stream` rooms and user rooms

### Backend — Dependencies
- `backend/app/api/deps.py` — `CurrentUserOrGuest`, `GuestShareContext`, `get_current_user_or_guest()`

### Frontend — Routes
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Session chat page (main entry point)
- `frontend/src/routes/_layout/sessions/index.tsx` — All sessions list grouped by agent
- `frontend/src/routes/_layout/sessions/agent/$agentId.tsx` — Per-agent sessions table
- `frontend/src/routes/_layout/sessions.tsx` — Layout shell (passthrough outlet)

### Frontend — Hooks
- `frontend/src/hooks/useSessionStreaming.ts` — Streaming state, WebSocket subscription, message send/stop, event deduplication

### Frontend — Components: Sessions
- `frontend/src/components/Sessions/AgentSessionsGroup.tsx` — Agent group card in sessions list
- `frontend/src/components/Sessions/AgentSessionsTable.tsx` — Tabular session list per agent
- `frontend/src/components/Sessions/SessionCard.tsx` — Individual session card
- `frontend/src/components/Sessions/SessionModeBadge.tsx` — Mode badge (building/conversation)
- `frontend/src/components/Sessions/CreateSession.tsx` — New session creation form
- `frontend/src/components/Sessions/EditSession.tsx` — Edit session title/mode
- `frontend/src/components/Sessions/DeleteSession.tsx` — Delete with confirmation
- `frontend/src/components/Sessions/LatestSessions.tsx` — Recent sessions widget
- `frontend/src/components/Sessions/AgentConversations.tsx` — Session list container

### Frontend — Components: Chat
- `frontend/src/components/Chat/MessageList.tsx` — Full message history renderer
- `frontend/src/components/Chat/MessageBubble.tsx` — Individual message; shows streaming indicator when `streaming_in_progress=true`
- `frontend/src/components/Chat/MessageInput.tsx` — Text input with send/stop controls, file upload
- `frontend/src/components/Chat/StreamingMessage.tsx` — Real-time streaming event display
- `frontend/src/components/Chat/StreamEventRenderer.tsx` — Renders events by type using `event_seq` as React key; handles `assistant`, `tool`, `thinking`, `system`, and `webapp_action` event types
- `frontend/src/components/Chat/WebappActionBlock.tsx` — Visual block for `webapp_action` events; supports normal (full block with icon + data fields) and compact (inline one-liner) modes
- `frontend/src/components/Chat/ModeSwitchToggle.tsx` — Toggle building/conversation mode
- `frontend/src/components/Chat/SubTasksPanel.tsx` — Side panel showing sub-tasks spawned by agent
- `frontend/src/components/Chat/RecoverSessionModal.tsx` — Session recovery dialog
- `frontend/src/components/Chat/FileUploadModal.tsx` — File attachment dialog
- `frontend/src/components/Chat/ToolCallBlock.tsx` — Tool execution display block
- `frontend/src/components/Chat/AnswerQuestionsModal.tsx` — Tool approval UI
- `frontend/src/components/Chat/UpdateSessionStateToolBlock.tsx` — Displays agent `update_session_state` call
- `frontend/src/components/Chat/AgentHandoverToolBlock.tsx` — Displays `create_agent_task` tool call

### Frontend — Services
- `frontend/src/services/eventService.ts` — Socket.IO client, room management (`subscribe`, `unsubscribeFromRoom`), `sendAgentUsageIntent()`

### Migrations (session-related)
- `backend/app/alembic/versions/a67c5808eea7_add_agent_sessions_extend_agent_add_.py` — Initial: `session` and `message` tables
- `backend/app/alembic/versions/a509ae4fadd1_add_interaction_status_to_session.py` — `interaction_status` field
- `backend/app/alembic/versions/b3ea462fe787_add_pending_messages_tracking_to_.py` — `pending_messages_count` field
- `backend/app/alembic/versions/cc18aee3c7c4_agent_sdk_to_sessions.py` — SDK session metadata
- `backend/app/alembic/versions/f3a1b2c4d5e6_add_agent_session_mode_and_sender_email.py` — `mode`, `sender_email`, `integration_type`
- `backend/app/alembic/versions/7aeed6ea3abf_add_share_source_and_session_email_.py` — `guest_share_id`, `email_thread_id`
- `backend/app/alembic/versions/t0o1p2q3r4s5_add_streaming_started_at_to_session.py` — `streaming_started_at`
- `backend/app/alembic/versions/u1p2q3r4s5t6_add_session_state_and_task_feedback.py` — `result_state`, `result_summary`, `source_task_id`
- `backend/app/alembic/versions/p6k7l8m9n0o1_add_todo_progress_to_session.py` — `todo_progress`
- `backend/app/alembic/versions/e346c86d4373_add_status_and_status_message_fields_to_.py` — `status`, `status_message` on `message`
- `backend/app/alembic/versions/d4e5f6a7b8c9_remove_agent_sdk_from_session.py` — Remove deprecated `agent_sdk`
- `backend/app/alembic/versions/dc259404533e_add_mcp_integration_tables.py` — `mcp_connector_id`, `mcp_session_id` on session
- `backend/app/alembic/versions/1cfe565b5e39_add_mcp_session_meta_table.py` — `mcp_session_meta` table
- `backend/app/alembic/versions/3a154fd039f5_add_user_workspaces_support.py` — `user_workspace_id` on session

### Tests
- `backend/tests/api/agents/` — Session and message integration tests

---

## Database Schema

### Table: `session`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID, PK | Session identifier |
| `environment_id` | UUID, FK → agent_environment.id (CASCADE) | Executing environment |
| `user_id` | UUID, FK → user.id (CASCADE) | Session owner (owner_id for guest sessions) |
| `user_workspace_id` | UUID, FK → user_workspace.id (CASCADE), nullable | Workspace isolation |
| `access_token_id` | UUID, FK → agent_access_tokens.id (SET NULL), nullable | A2A token scope tracking |
| `source_task_id` | UUID, FK → input_task.id (SET NULL), nullable | Input task that spawned session |
| `guest_share_id` | UUID, FK → agent_guest_share.id (SET NULL), nullable | Guest share link scoping |
| `mcp_connector_id` | UUID, FK → mcp_connector.id (SET NULL), nullable | MCP connector link |
| `mcp_session_id` | VARCHAR, nullable | MCP transport session ID (metadata only) |
| `title` | VARCHAR, nullable | Auto-generated from first assistant message |
| `mode` | VARCHAR | `"conversation"` \| `"building"` |
| `status` | VARCHAR | `"active"` \| `"completed"` \| `"error"` \| `"paused"` |
| `interaction_status` | VARCHAR | `""` \| `"running"` \| `"pending_stream"` |
| `pending_messages_count` | INTEGER | User messages with `sent_to_agent_status="pending"` |
| `session_metadata` | JSON | Stores `external_session_id`, `sdk_type` |
| `todo_progress` | JSON, nullable | List of TodoItem dicts from agent's TodoWrite tool |
| `result_state` | VARCHAR, nullable | Agent-declared: `"completed"` \| `"needs_input"` \| `"error"` |
| `result_summary` | VARCHAR, nullable | Agent's description accompanying result_state |
| `email_thread_id` | VARCHAR, nullable | Email Message-ID for threading |
| `integration_type` | VARCHAR, nullable | `"email"` \| `"a2a"` |
| `sender_email` | VARCHAR, nullable | Original email sender (owner mode only) |
| `streaming_started_at` | DATETIME, nullable | Set when `interaction_status="running"`, cleared on end |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |
| `last_message_at` | DATETIME, nullable | Updated on each new message |

### Table: `message`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID, PK | Message identifier |
| `session_id` | UUID, FK → session.id (CASCADE) | Parent session |
| `role` | VARCHAR | `"user"` \| `"agent"` \| `"system"` |
| `content` | TEXT | Message text; accumulated during streaming |
| `sequence_number` | INTEGER | Monotonically incrementing per session (unique constraint with session_id) |
| `message_metadata` | JSON | `streaming_events`, `streaming_in_progress`, `tools_needing_approval`, `model` |
| `tool_questions_status` | VARCHAR, nullable | `null` \| `"unanswered"` \| `"answered"` |
| `answers_to_message_id` | UUID, FK → message.id, nullable | Tool approval reply link |
| `status` | VARCHAR | `""` \| `"user_interrupted"` \| `"error"` |
| `status_message` | VARCHAR, nullable | Error detail or interrupt reason |
| `sent_to_agent_status` | VARCHAR | `"pending"` \| `"sent"` |
| `timestamp` | DATETIME | Message creation time |

**Key `message_metadata` fields:**
- `streaming_in_progress: bool` — True while generating; cleared on completion
- `streaming_events: list[{event_seq, type, content, metadata}]` — All events flushed to DB; post-processed after stream completion (see below)
- `tools_needing_approval: list[{tool_name, tool_use_id, input}]` — Pending approvals (pre-approved tools filtered out)
- `model: str` — LLM model used for this message

**`streaming_events` event types:**

| `type` | `content` | `metadata` fields | Description |
|--------|-----------|-------------------|-------------|
| `assistant` | Text chunk | `model?` | Agent text output |
| `tool` | Tool label | `tool_id`, `tool_input`, `tool_name`, `needs_approval?` | Tool call |
| `thinking` | Thinking text | — | Extended thinking block |
| `system` | Notification text | `interrupt_notification?` | System message (e.g., interrupt notice) |
| `webapp_action` | Action name | `action`, `data` | Webapp UI command extracted from agent response; produced by post-processing of assistant events containing `<webapp_action>` tags |

After streaming completes, assistant events containing `<webapp_action>` tags are split into interleaved text and `webapp_action` events, and all events are re-numbered sequentially. See [webapp chat actions tech](../../agents/agent_webapp/webapp_chat_actions_tech.md) for the full post-processing algorithm.

---

## API Endpoints

### Sessions (`/api/v1/sessions`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Create session for agent's active environment |
| `GET` | `/` | List sessions (filter: `agent_id`, `user_workspace_id`, `guest_share_id`) |
| `GET` | `/{id}` | Get session with agent metadata (`SessionPublicExtended`) |
| `PATCH` | `/{id}` | Update title, status, or mode |
| `PATCH` | `/{id}/mode` | Switch mode, optionally clear external SDK session |
| `POST` | `/{id}/reset-sdk` | Clear external SDK session ID (forces new context on next message) |
| `POST` | `/{id}/recover` | Mark session for recovery; re-queues last pending user message |
| `DELETE` | `/{id}` | Delete session and all messages |
| `POST` | `/bulk-delete` | Delete multiple sessions by ID list |

**Auth:** All endpoints accept both `CurrentUser` (JWT) and `CurrentUserOrGuest` (guest share JWT).

### Messages (`/api/v1/sessions/{session_id}/messages`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/messages` | Get messages; merges DB content with in-memory streaming buffer |
| `POST` | `/messages/stream` | Send user message; returns immediately with `{status, stream_room}` |
| `POST` | `/messages/interrupt` | Interrupt active stream; forwards to agent-env if external_session_id available |
| `GET` | `/messages/streaming-status` | Check streaming status; DB-based with `ActiveStreamingManager` supplement |

---

## Services & Key Methods

### `SessionService` (`backend/app/services/session_service.py`)

**Session lifecycle:**
- `create_session(db, agent_id, user_id, ...)` — Resolves active environment, creates `Session` record; handles `access_token_id`, `source_task_id`, `guest_share_id`, `email_thread_id`
- `update_session(db, session, data)` — Apply `SessionUpdate` fields
- `update_session_status(db, session_id, status)` — Set `status` field
- `update_interaction_status(db, session_id, interaction_status)` — Set `interaction_status`; also updates `streaming_started_at`
- `switch_mode(db, session, mode, clear_external_session)` — Validates mode change, optionally clears external SDK session
- `mark_session_for_recovery(db, session_id)` — Resets `result_state`, re-queues pending user message

**Session queries:**
- `get_session(db, session_id, user_id)` — Fetch with ownership check
- `list_user_sessions(db, user_id, ...)` — All user sessions with extended metadata
- `list_agent_sessions(db, agent_id, user_id)` — All sessions across agent's environments
- `list_environment_sessions(db, environment_id, ...)` — Sessions for one environment
- `list_task_sessions(db, task_id)` — Sessions spawned by an input task
- `get_session_by_email_thread(db, agent_id, email_thread_id)` — Thread matching for email
- `get_session_by_context_id(db, context_id, connector_id)` — MCP session lookup with cross-connector check
- `get_or_create_mcp_session(db, connector_id, context_id, user_id, authenticated_user_id)` — Find or create for MCP; creates `MCPSessionMeta` on new sessions

**External SDK session management:**
- `get_external_session_id(session)` — Read from `session_metadata["external_session_id"]`
- `set_external_session_id(db, session, id)` — Write to metadata
- `clear_external_session(db, session)` — Remove external session ID and sdk_type
- `should_create_new_sdk_session(session)` — True if no `external_session_id` stored

**Streaming orchestration:**
- `send_session_message(db, session_id, user_id, content, ...)` — Main entry: validates access, handles files, creates user message, calls `initiate_stream()`
- `initiate_stream(session_id, environment_id, user_id)` — Checks environment state, activates if needed (background task), marks `pending_stream`, or calls `process_pending_messages()`
- `handle_environment_activated(environment_id)` — Event handler: processes all `pending_stream` sessions for the environment
- `ensure_environment_ready_for_streaming(db, environment_id)` — Synchronous check used by MCP/A2A paths

**Stream lifecycle event handlers** (called by `event_service` on backend events):
- `handle_stream_started(session_id, user_id)` — Sets `interaction_status="running"`, `streaming_started_at=now`, emits `session_interaction_status_changed` to user room
- `handle_stream_completed(session_id, user_id)` — Clears `interaction_status`, `streaming_started_at`; triggers `auto_generate_session_title()`
- `handle_stream_error(session_id, user_id)` — Clears interaction status, sets `status="error"`
- `handle_stream_interrupted(session_id, user_id)` — Clears interaction status

**Title generation:**
- `auto_generate_session_title(db, session_id)` — Reads first assistant message, calls `AIFunctionsService` to generate concise title; updates session

### `MessageService` (`backend/app/services/message_service.py`)

**Message creation:**
- `create_message(db, session_id, role, content, ...)` — Creates `SessionMessage` with auto-incremented sequence, associates file IDs
- `prepare_user_message_with_files(db, session_id, content, file_ids, user_id)` — Validates files, uploads to agent-env, prepends file paths to content, creates message
- `create_user_message_and_emit_event(db, session_id, content, ...)` — Creates message and emits `user_message_created` WebSocket event

**Message queries:**
- `get_session_messages(db, session_id, offset, limit)` — Messages ordered by sequence, files populated from junction table
- `get_last_n_messages(db, session_id, n)` — Last N messages for context window
- `get_last_message(db, session_id)` — Highest-sequence message

**Pending messages:**
- `collect_pending_messages(db, session_id)` — All messages with `sent_to_agent_status="pending"`, reconstructs content with file paths
- `mark_messages_as_sent(db, message_ids)` — Set `sent_to_agent_status="sent"`

**Streaming:**
- `process_pending_messages(session_id, environment_id, user_id)` — Core async streaming loop: collects pending messages, calls `stream_message_with_events()`, handles all events
- `stream_message_with_events(session_id, environment_id, user_id, messages)` — Connects to agent-env via SSE, assigns `event_seq`, buffers in `ActiveStreamingManager`, flushes to DB every ~5s, emits each event to WebSocket room; handles `session_created`, `assistant`, `tool`, `thinking`, `done`, `error`, `interrupted` events
- `send_message_to_environment_stream(env_url, auth_headers, payload)` — HTTP POST to agent-env `/chat/stream`, yields raw SSE events
- `_get_session_context_and_reset_state(session_id)` — Reads session, resets `result_state`/`result_summary` if set, triggers task status sync if `source_task_id` exists; returns previous state for passthrough to agent-env

**Tool handling:**
- `detect_ask_user_question_tool(event)` — Detects `AskUserQuestion` tool call, sets `tool_questions_status="unanswered"`
- `get_environment_url(environment)` — Extracts base URL from environment config
- `get_auth_headers(environment)` — Builds `Authorization: Bearer` headers for agent-env requests
- `forward_interrupt_to_environment(env_url, auth_headers, external_session_id)` — POST to `/chat/interrupt/{id}`

### `ActiveStreamingManager` (`backend/app/services/active_streaming_manager.py`)

Singleton in-memory store tracking live backend-to-agent-env streams.

- `register_stream(session_id, external_session_id)` → `ActiveStream` — Create stream entry
- `update_external_session_id(session_id, external_session_id)` → `bool` — Store SDK ID; returns True if interrupt was pending
- `unregister_stream(session_id)` — Remove on completion
- `request_interrupt(session_id)` → `bool` — Returns True if immediate, False if queued pending SDK session ID
- `append_streaming_event(session_id, event)` — Add to in-memory buffer
- `update_last_flushed_seq(session_id, seq)` — Track last DB-flushed event index
- `get_stream_events(session_id)` → `{streaming_events, accumulated_content}` — Buffer for API merge
- `is_streaming(session_id)` → `bool`
- `get_stream_info(session_id)` → `{is_streaming, duration_seconds, is_interrupted, external_session_id}`

---

## Frontend Components

### `useSessionStreaming` hook (`frontend/src/hooks/useSessionStreaming.ts`)

Takes `{sessionId, session, messagesData, onSuccess, onError}`, returns `{sendMessage, stopMessage, isStreaming, streamingEvents, isInterruptPending}`.

- `isStreaming` — Derived from `session.interaction_status === "running" || "pending_stream"`
- `streamingEvents` — Ordered, deduplicated `StreamEvent[]` built from DB `streaming_events` + live WebSocket events
- Subscribes to WS room `session_{sessionId}_stream` on stream start; unsubscribes on end
- Deduplicates by `event_seq`; triggers message refetch on gap detection
- `sendMessage(content, answersToMessageId?, fileIds?, fileObjs?)` — POSTs to `/messages/stream`, optimistically adds user message to cache
- `stopMessage()` — POSTs to `/messages/interrupt`, sets `isInterruptPending=true`

### Session page (`frontend/src/routes/_layout/session/$sessionId.tsx`)

- Queries: `["session", sessionId]` (3s interval when streaming, 10s otherwise), `["messages", sessionId]` (2s during streaming), `["agent", agentId]`, `["environment", effectiveEnvId]`, `["subTasksCount", sessionId]` (15s)
- On load: sends `eventService.sendAgentUsageIntent(environment_id)` to warm up environment; stores resolved environment ID if backend switched to a different active env
- Listens for WS events: `ENVIRONMENT_ACTIVATING`, `ENVIRONMENT_ACTIVATED`, `ENVIRONMENT_ACTIVATION_FAILED`, `ENVIRONMENT_SUSPENDED`, `SESSION_INTERACTION_STATUS_CHANGED`, `SESSION_STATE_UPDATED`
- Header built dynamically via `setHeaderContent()` from `usePageHeader` context

### Sessions list (`frontend/src/routes/_layout/sessions/index.tsx`)

- Query: `["sessions", activeWorkspaceId]` via `SessionsService.listSessions({userWorkspaceId})`
- Groups sessions by `agent_id` using `useMemo`, renders `AgentSessionsGroup` cards in a responsive grid

### Agent sessions table (`frontend/src/routes/_layout/sessions/agent/$agentId.tsx`)

- Query: `["sessions", "agent", agentId]` via `SessionsService.listSessions({agentId, limit:500, orderBy:"last_message_at", orderDesc:true})`
- Renders `AgentSessionsTable` component

---

## Configuration

No dedicated settings keys for agent sessions. Relevant ambient settings:

| Setting | Location | Relevance |
|---------|----------|-----------|
| `SECRET_KEY` | `.env` | JWT signing for guest share tokens |
| `AGENT_AUTH_TOKEN` | Agent-env container env | JWT for backend↔agent-env HTTP auth |
| Environment container URL | `agent_environment.config` | Destination for SSE streaming calls |

---

## Security

- **Ownership enforcement** — `_verify_session_access()` in sessions route checks `user_id` matches, or `guest_share_id` matches for guest callers, or `access_token_id` scope for A2A callers
- **Guest isolation** — `GuestShareContext` in `deps.py` extracts guest JWT claims; sessions with `guest_share_id` are only accessible to that share's guests or the owner
- **A2A scope** — `access_token_id` on session prevents cross-token access in `list_environment_sessions()`
- **Building mode block** — Guest share sessions and MCP sessions enforce `mode="conversation"`; switching to building blocked at route level
- **Message immutability** — No message update endpoint; content only changes via streaming flush (append-only semantics)
- **Agent-env auth** — All HTTP calls to agent-env include `Authorization: Bearer {AGENT_AUTH_TOKEN}` generated during environment start
