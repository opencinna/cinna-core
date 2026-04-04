# Chat Windows — Technical Reference

## File Locations

### Frontend — Core Chat Components

- `frontend/src/components/Chat/MessageList.tsx` — Main scrollable message container, auto-scroll logic, streaming zone splitting (before/during/after streaming), scroll-down button
- `frontend/src/components/Chat/MessageBubble.tsx` — Individual message renderer: user/agent/system routing, streaming events extraction, tool approval integration, question extraction, status badges, recovery modal trigger, pending indicator for user messages with `sent_to_agent_status="pending"`
- `frontend/src/components/Chat/StreamingMessage.tsx` — Live streaming message with pulsing loader dots, delegates to StreamEventRenderer
- `frontend/src/components/Chat/StreamEventRenderer.tsx` — Event-type dispatcher: assistant→MarkdownRenderer, tool→ToolCallBlock, thinking→collapsible block (hidden in compact), system→notification block, webapp_action→WebappActionBlock
- `frontend/src/components/Chat/MarkdownRenderer.tsx` — `react-markdown` + `remark-gfm` wrapper with custom code rendering (inline: background highlight, block: dark CLI-like slate-900 theme)
- `frontend/src/components/Chat/MessageInput.tsx` — Textarea with file attachment, drag-drop upload, send/stop buttons, prompt refinement via utilities API. Input is always enabled; `isStreaming` prop shows stop button stacked above send button (both 28px tall) without disabling the textarea
- `frontend/src/components/Chat/ChatHeader.tsx` — Session title, mode indicator dot (orange=building, blue=conversation), integration badges, sub-tasks badge, app button, options menu
- `frontend/src/components/Chat/FileBadge.tsx` — File attachment visual badge with download link
- `frontend/src/components/Chat/FileUploadModal.tsx` — Drag-drop file upload dialog, 100MB limit
- `frontend/src/components/Chat/MessageActions.tsx` — Action buttons below messages: "Answer Questions", "Approve Tools"

### Frontend — Tool Call Blocks

- `frontend/src/components/Chat/ToolCallBlock.tsx` — Main dispatcher, routes to specialized blocks by `toolName.toLowerCase()`
- `frontend/src/components/Chat/ReadToolBlock.tsx` — File path display
- `frontend/src/components/Chat/WriteToolBlock.tsx` — File path + content
- `frontend/src/components/Chat/EditToolBlock.tsx` — File path + old/new string diff
- `frontend/src/components/Chat/BashToolBlock.tsx` — Command display
- `frontend/src/components/Chat/CompactBashBlock.tsx` — Shortened command for compact mode
- `frontend/src/components/Chat/GlobToolBlock.tsx` — Glob pattern display
- `frontend/src/components/Chat/WebSearchToolBlock.tsx` — Search query display
- `frontend/src/components/Chat/TodoWriteToolBlock.tsx` — Todo items with progress
- `frontend/src/components/Chat/AskUserQuestionToolBlock.tsx` — Question count badge
- `frontend/src/components/Chat/AgentHandoverToolBlock.tsx` — Handover target + task message
- `frontend/src/components/Chat/UpdateSessionStateToolBlock.tsx` — State + summary
- `frontend/src/components/Chat/KnowledgeQueryToolBlock.tsx` — Query + article IDs
- `frontend/src/components/Chat/WebappActionBlock.tsx` — Action name + data payload

### Frontend — Special Widgets

- `frontend/src/components/Chat/AnswerQuestionsModal.tsx` — Multi-question modal with radio/checkbox/text input support, question deduplication
- `frontend/src/components/Chat/RecoverSessionModal.tsx` — Session recovery modal, detects auto-resend vs manual recovery
- `frontend/src/components/Chat/SubTasksPanel.tsx` — Overlay panel listing sub-tasks with status badges and navigation
- `frontend/src/components/Chat/ModeSwitchToggle.tsx` — Building/conversation mode toggle button
- `frontend/src/components/Chat/ToolCallMessage.tsx` — Legacy tool call display (backwards compatibility)

### Frontend — Hooks & Services

- `frontend/src/hooks/useSessionStreaming.ts` — Main streaming orchestration: `sendMessage()`, `stopMessage()`, WebSocket room subscription, event deduplication by `event_seq`, gap detection, optimistic message cache (with `sent_to_agent_status="pending"`), title polling, cleanup. Sending a new message while streaming preserves existing stream events rather than clearing them
- `frontend/src/hooks/useToolApproval.ts` — Tool approval state management, filters already-approved tools, handles API call
- `frontend/src/hooks/useGuestShare.tsx` — Context provider for guest share metadata (`isGuest`, `guestShareId`, `agentId`, `guestShareToken`)
- `frontend/src/services/eventService.ts` — Socket.IO singleton client, room subscription with `activeRooms` tracking, auto-reconnect (5 attempts, exponential backoff 1-32s), subscription ID management

### Frontend — Routes (Hosting Contexts)

- `frontend/src/routes/_layout/session/$sessionId.tsx` — Full session page: session/messages queries with dynamic refetch intervals (3s/2s streaming, 10s idle), environment panel, sub-tasks panel, WebSocket status listener, initial message support via search params
- `frontend/src/routes/guest/$guestShareToken.tsx` — Guest share page: info→code→auth→ready flow, session selection, environment panel (configurable), GuestShareProvider context
- `frontend/src/routes/webapp/$webappToken.tsx` — Webapp viewer: embed support, auth flow, delegates to WebappChatWidget

### Frontend — Webapp Chat Widget

- `frontend/src/components/Webapp/WebappChatWidget.tsx` — Self-contained embedded widget: FAB toggle, localStorage cache (`webapp_chat_{token}`), iframe page context collection via `postMessage`, streaming via direct `eventService` subscription, `chatFetch()` helper for webapp-scoped API calls

### Backend — API Routes

- `backend/app/api/routes/messages.py` — Message CRUD, `send_message_stream()`, `interrupt_message()`, `get_messages()` with ActiveStreamingManager in-memory event merge
- `backend/app/api/routes/webapp_chat.py` — Public webapp chat endpoints (session CRUD, message streaming, interruption) — scoped by webapp token

### Backend — Services

- `backend/app/services/sessions/session_service.py` — `initiate_stream()`, `process_pending_messages()`, `handle_stream_started/completed/error/interrupted()`, `handle_environment_activated()`
- `backend/app/services/sessions/message_service.py` — `stream_message_with_events()`: event_seq assignment, incremental DB flush (every ~2s via background thread), streaming event accumulation, TodoWrite detection, session state reset
- `backend/app/services/sessions/active_streaming_manager.py` — In-memory stream tracking: `ActiveStream` dataclass with event buffer, interrupt management, `get_stream_events()` for API merge
- `backend/app/services/events/event_service.py` — `EventService` singleton: Socket.IO server, room-based broadcasting, `emit_stream_event()`, backend event handler registration

### Backend — Models

- `backend/app/models/sessions/session.py` — `ChatSession`: `interaction_status`, `streaming_started_at`, `result_state`, `result_summary`, `todo_progress`, `session_metadata`
- `backend/app/models/sessions/session.py` — `SessionMessage`: `content`, `message_metadata` (contains `streaming_events[]`, `streaming_in_progress`, `tools_needing_approval`, `model`, `total_cost_usd`, `duration_ms`, `num_turns`, `command`)

## Streaming Architecture

### Transport Layers

- **Frontend ↔ Backend**: WebSocket via Socket.IO — room-based event broadcasting, no browser connection limits
- **Backend ↔ Agent Environment**: SSE (Server-Sent Events) — HTTP streaming compatible with agent environment SDK

### Message Send Flow

1. Frontend subscribes to WebSocket room `session_{sessionId}_stream` via `eventService.subscribeToRoom()`
2. Frontend sends `POST /api/v1/sessions/{sessionId}/messages/stream` — returns immediately with `{status: "ok", stream_room}`
3. Backend creates user message, delegates to `SessionService.initiate_stream()`
4. If environment not running: marks session `pending_stream`, activates environment in background
5. When environment ready: `process_pending_messages()` streams from agent-env via SSE
6. Each SSE event gets sequential `event_seq`, buffered in `ActiveStreamingManager`, emitted to WebSocket room
7. DB flushed every ~2s in background thread (`asyncio.to_thread`)
8. On completion: `interaction_status` cleared, `session_interaction_status_changed` event emitted to user room
9. Frontend detects status change → refetches messages → streaming display replaced by persisted MessageBubbles

### Event Deduplication (Frontend)

`useSessionStreaming.ts` tracks `lastKnownSeq` (highest seen `event_seq`):
- `K <= lastKnownSeq` → duplicate, skip
- `K === lastKnownSeq + 1` → next expected, append
- `K > lastKnownSeq + 1` → gap detected, trigger message refetch to fill missing events

Two event sources merge into one ordered list:
1. **WebSocket** — real-time events (sub-second delivery)
2. **Message query refetch** — DB + in-memory merge every 2s during streaming

### Derived Streaming State

Frontend streaming state is NOT local — it's derived from the session query:

`isStreaming = session.interaction_status === "running" || session.interaction_status === "pending_stream"`

This means:
- Page refresh during streaming → session loads with `interaction_status="running"` → streaming auto-resumes
- No explicit reconnection logic needed
- Multi-tab support possible (all tabs see same session state)

### Dynamic Polling Intervals

Session page adjusts refetch timing based on streaming state:
- **Session query**: 3s during streaming, 10s idle — configured in `$sessionId.tsx`
- **Messages query**: 2s during streaming, disabled idle — configured in `$sessionId.tsx`
- **WebSocket status events**: `session_interaction_status_changed` triggers immediate query invalidation for sub-second state transitions

## Auto-Scroll Implementation

`MessageList.tsx` manages scroll behavior:

- `scrollContainerRef` — ref to scrollable div container
- `messagesEndRef` — ref to invisible div at bottom of message list
- `userHasScrolled` — boolean state, true when user scrolls more than 100px from bottom
- `showScrollButton` — boolean state, controls floating arrow button visibility
- `scrollToBottom()` — calls `messagesEndRef.scrollIntoView({ behavior: "smooth" })`, resets `userHasScrolled`
- `handleScroll()` — reads `scrollTop + clientHeight` vs `scrollHeight`, threshold 100px from bottom
- Auto-scroll effect triggers on `[messages, streamingEvents, userHasScrolled]` changes — only scrolls when `!userHasScrolled`

`WebappChatWidget.tsx` uses simpler always-scroll approach: `scrollToBottom()` on every `[messages, streamingEvents]` change without user-scroll tracking.

## Streaming Zone Split (MessageList)

During streaming, `MessageList.tsx` partitions messages:

1. Finds in-progress message: `message_metadata.streaming_in_progress === true`
2. Gets its `sequence_number` as `streamingSeq`
3. `beforeStreaming` = messages where `!streaming_in_progress && sequence_number < streamingSeq`
4. `afterStreaming` = messages where `!streaming_in_progress && sequence_number > streamingSeq`
5. Renders: `beforeStreaming` → `StreamingMessage` (with live events) → `afterStreaming`

This prevents system/delegation messages (created by tool calls during streaming) from appearing before the streaming message completes.

## Webapp Widget Specifics

### LocalStorage Caching

`WebappChatWidget.tsx` caches session state in `localStorage` under key `webapp_chat_{webappToken}`:
- Stores: `sessionId`, `messages[]`, `cachedAt` timestamp
- Restored on mount for instant display
- Background verification against backend after cache restore (silent failure keeps cache)
- Updated on every `[sessionId, messages]` change

### Page Context Collection

Before sending a message, the widget collects context from the host iframe:
1. Captures `window.getSelection()` text (max 2,000 chars)
2. Sends `postMessage({ type: "request_page_context" })` to iframe
3. Waits up to 500ms for `page_context_response` with schema.org microdata
4. Builds JSON payload with `selected_text`, `page.url`, `page.title`, `microdata`
5. Sends as `page_context` field in message request body

### Webapp Action Forwarding

When a `webapp_action` stream event arrives:
1. Event handler in widget extracts `action` and `data`
2. Forwards via `iframeRef.current.contentWindow.postMessage({ type: "webapp_action", action, data }, "*")`
3. Iframe handles the action (e.g., `refresh_page`, `update_form`, `show_notification`)

### Streaming State Management (Widget)

Unlike the session page hook, the webapp widget manages streaming state locally:
- `isStreaming` — local state, set from `session.interaction_status` on load and from WebSocket events
- Subscribes to `session_interaction_status_changed` events for the session
- Subscribes to `stream_event` events via `eventService` (same deduplication by `event_seq`)
- On stream complete: clears events, resets seq counter, refreshes messages from API

## Component Prop Patterns

### `conversationModeUi` Prop

Passed through the component tree to control display density:
- `"detailed"` — full tool output, thinking blocks visible, expanded bash commands
- `"compact"` — filenames only for Read/Edit, shortened bash, thinking blocks hidden
- Set at route level: session page uses user's choice, webapp widget derives from `chatMode`

### `MessageInput` Props

| Prop | Type | Purpose |
|------|------|---------|
| `onSend` | `(content, fileIds?) => void` | Called when user sends a message |
| `onStop` | `() => void` | Called when user clicks stop button |
| `isStreaming` | `boolean` | Shows stop button stacked above send button; textarea remains enabled |
| `sendDisabled` | `boolean` | Deprecated — use `isStreaming` instead. Kept for backward compatibility |
| `isInterruptPending` | `boolean` | Disables stop button and shows spinner while interrupt request is in flight |
| `placeholder` | `string` | Textarea placeholder text |
| `agentId` | `string` | Used for prompt refinement API call |
| `mode` | `"building" \| "conversation"` | Passed to prompt refinement API |

When `isStreaming` is true, the button area shows a destructive stop button (28px) stacked above a primary send button (28px), totalling the same 60px height as the idle send button. Sending while streaming is always permitted — the backend queues the message via `pending_stream` status.

### Message Metadata Fields Used by Chat Components

| Field | Component | Purpose |
|-------|-----------|---------|
| `streaming_in_progress` | MessageList | Identifies in-progress message for zone splitting |
| `streaming_events[]` | MessageBubble → StreamEventRenderer | Event array for completed message rendering |
| `command` | MessageBubble | Routes to MarkdownRenderer instead of StreamEventRenderer |
| `tools_needing_approval` | MessageBubble → useToolApproval | Triggers "Approve Tools" action button |
| `tool_questions_status` | MessageBubble | Triggers "Answer Questions" action button when "unanswered" |
| `model`, `total_cost_usd`, `duration_ms`, `num_turns` | MessageBubble | Metadata tooltip on info icon |
| `task_created`, `task_id`, `session_id`, `inbox_task` | MessageBubble | Task creation system message links |
| `task_feedback`, `task_state` | MessageBubble | Task feedback message styling and icons |

### `sent_to_agent_status` Field (Top-Level Message Field)

`sent_to_agent_status` is a top-level field on `SessionMessage` (not inside `message_metadata`). It drives the pending indicator on user messages:

| Value | Meaning | UI |
|-------|---------|-----|
| `"pending"` | Message created but not yet delivered to the agent | Amber "Pending" badge with pulsing clock icon |
| `"sent"` | Message delivered to the agent | No badge |

The optimistic message added to the React Query cache by `useSessionStreaming.sendMessage()` always sets `sent_to_agent_status: "pending"`. When the real message is fetched from the server (after stream completion), the status reflects the actual backend state. If additional messages were queued while the agent was streaming, they remain `"pending"` until the backend processes them in `process_pending_messages()`.

## Adding a New Tool Call Block

To add a specialized renderer for a new tool:

1. Create component in `frontend/src/components/Chat/` following existing patterns (e.g., `MyToolBlock.tsx`)
2. Import in `frontend/src/components/Chat/ToolCallBlock.tsx`
3. Add routing condition: `if (toolNameLower === "mytool" && toolInput?.required_field) { return <MyToolBlock ... /> }`
4. Add compact mode variant if needed (check `isCompact` flag)
5. Unknown tools automatically fall back to generic JSON parameter display — no registration needed for basic support

## Adding a New Stream Event Type

To handle a new event type from the backend:

1. Backend: emit events with the new `type` field in `message_service.py:stream_message_with_events()`
2. Frontend: add rendering case in `frontend/src/components/Chat/StreamEventRenderer.tsx`
3. If the event needs special handling in the streaming hook: update `frontend/src/hooks/useSessionStreaming.ts` event handler
4. For webapp-specific events: also update `frontend/src/components/Webapp/WebappChatWidget.tsx` stream event handler

## Key Libraries

- `react-markdown` + `remark-gfm` — Markdown rendering with GFM support
- `socket.io-client` — WebSocket transport for streaming events
- `date-fns` — Timestamp formatting (`formatDistanceToNow`)
- `lucide-react` — Icons throughout chat UI
- `sonner` — Toast notifications for tool approval, recovery actions
- `@tanstack/react-query` — Message and session data fetching with configurable refetch intervals
- `@tanstack/react-router` — Route-level data loading and navigation (session page, guest share, webapp)
