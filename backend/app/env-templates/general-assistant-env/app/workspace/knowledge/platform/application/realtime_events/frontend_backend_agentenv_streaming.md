# Frontend-Backend-AgentEnv Streaming Architecture

## Overview

The system implements a three-layer streaming architecture with **WebSocket** for frontend-backend communication and **SSE** for backend-agent-env communication. This hybrid approach eliminates browser connection limits while maintaining compatibility with the agent environment's SSE-based SDK streaming.

**Key Principles**:
- **DB as single source of truth** - Database always reflects current streaming content (within ~2s)
- **WebSocket as enhancement, not requirement** - System works via polling alone; WebSocket adds real-time granularity
- **No hidden messages** - In-progress agent messages are displayed with streaming indicator
- **Derived UI state** - Frontend streaming state derived from `session.interaction_status`, not local state
- Message processing continues even if frontend disconnects

## Architecture Layers

```
┌─────────────┐         ┌─────────────┐         ┌──────────────────┐
│  Frontend   │ ◄─WS───►│   Backend   │ ◄─SSE──►│  Agent Env       │
│             │         │             │         │  (Docker)        │
│ React/TS    │         │ FastAPI     │         │ FastAPI + SDK    │
└─────────────┘         └─────────────┘         └──────────────────┘
     │                       │                          │
     │                       │                          │
   Local                 PostgreSQL                Workspace
  Storage                Sessions/                 Files/Logs
  Socket.IO              Messages
```

## Core Architecture: Event Sequencing & Deduplication

### The Problem

When the backend flushes streaming content to DB every ~2s, and the frontend receives both DB content (via query refetch) and real-time WS events, there's a risk of rendering the same content twice or showing inconsistent state.

### Solution: Index-Based Event Ordering

**Backend** assigns a monotonically incrementing `event_seq` (starting from 1) to each streaming event within a stream. This index is:
- Included in every WS event sent to the frontend
- Stored in the DB message's `streaming_events` array (each event has its `event_seq`)
- Used by the API to merge in-memory events with DB-persisted events

**Frontend** maintains a single ordered event list, populated from two sources:
1. **DB message** (on load/refetch): provides events up to the last flush + in-memory buffer
2. **WebSocket** (real-time): provides new events as they arrive

The frontend tracks `lastKnownSeq` (highest `event_seq` it has). Deduplication rules when receiving an event with seq=K:
- `K <= lastKnownSeq` → **duplicate**, ignore (already have it from DB or earlier WS)
- `K === lastKnownSeq + 1` → **next expected**, append to display list
- `K > lastKnownSeq + 1` → **gap detected**, trigger message refetch to fill missing events

### Rendering Model

The frontend maintains ONE combined event list for the streaming message:

```
On page load:
  1. Fetch messages → in-progress msg has streaming_events: [{seq:1,...}, ..., {seq:8,...}]
  2. Set lastKnownSeq = 8
  3. Subscribe to WS room
  4. WS delivers seq:9, seq:10, ... → append to list
  5. Render: all events in order (1..10+)

On message refetch (every 2s):
  1. DB now has streaming_events up to seq:12 (from API merge of DB + in-memory)
  2. lastKnownSeq was 10 from WS
  3. Events seq:11, seq:12 are new from DB → merge into list
  4. Update lastKnownSeq = 12
  5. WS continues with seq:13, 14, ...
```

### Key Properties

- **No separate "base + delta" rendering** - single unified event list displayed by one component
- **Backend controls ordering** - indices are always incremental, assigned server-side
- **Self-healing** - gaps trigger refetch, which fills missing data from DB + in-memory buffer
- **Idempotent** - receiving same event twice (from both DB and WS) is harmless (skip by seq)

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Page refresh | API returns ALL events (DB + in-memory buffer via ActiveStreamingManager). Zero gap. WS reconnects for live updates. |
| WS disconnect mid-stream | Events stop arriving live. Message refetch returns complete list from API (DB + in-memory). No data loss. |
| WS never connects | Pure polling mode. API always returns complete events. Fully functional, just not real-time. |
| Duplicate event (same seq from DB and WS) | Skip - already in list. No double rendering. |
| Gap detected (seq jumps) | Trigger immediate message refetch. API provides all events including in-memory ones. |
| Backend restart mid-stream | In-memory buffer lost. DB has events up to last flush. Stream itself is also lost (background task killed). Session shows content up to last flush point. |

## Message Flow

### 1. Sending a Message

**Frontend** (`useSessionStreaming.ts:sendMessage`):
- Subscribes to session-specific WebSocket room: `session_{session_id}_stream`
- Subscribes to `stream_event` events via `eventService`
- Sends POST to `/api/v1/sessions/{session_id}/messages/stream`
- Optimistically adds user message to cache
- Invalidates session query after 200ms to detect `interaction_status` change

**Backend API** (`messages.py:send_message_stream`):
- Validates session ownership
- Handles file attachments via `MessageService.prepare_user_message_with_files()` if present
- Creates user message with `sent_to_agent_status='pending'`
- Delegates to `SessionService.initiate_stream()`
- Returns immediately with response: `{status: "ok", stream_room: "session_{id}_stream"}`

**Backend Service** (`session_service.py:initiate_stream`):
Orchestrates pending message processing and environment activation:
1. Checks if environment needs activation (suspended/building/starting/activating)
2. If activation needed:
   - If suspended: Activates environment in background task
   - Marks session as `pending_stream` regardless of environment state
   - `ENVIRONMENT_ACTIVATED` event is emitted by lifecycle methods when environment becomes "running"
   - `SessionService.handle_environment_activated()` processes all pending sessions for that environment
3. If environment already active ("running"):
   - Immediately processes pending messages via `process_pending_messages()`

**Note**: `ENVIRONMENT_ACTIVATED` is emitted from all code paths that transition an environment to "running":
- `activate_suspended_environment()` - reactivating suspended environments
- `start_environment()` - initial start or restart
- `rebuild_environment()` - after rebuild if was previously running

This event-driven approach ensures sessions are processed regardless of how the environment became active.

4. `process_pending_messages()` handles streaming:
   - Emits `STREAM_STARTED` backend event (triggers `handle_stream_started` which sets `interaction_status="running"`, `streaming_started_at=now`, and emits `session_interaction_status_changed` WS event)
   - Streams from agent-env via SSE
   - Each event gets assigned `event_seq` and appended to `ActiveStreamingManager` buffer
   - Events flushed to DB every ~2s (non-blocking background thread)
   - Emits each streaming event (with `event_seq`) to WebSocket room
   - On completion: final DB update sets `streaming_in_progress=False`, emits `STREAM_COMPLETED` which triggers `handle_stream_completed` (clears `interaction_status`, `streaming_started_at`, emits `session_interaction_status_changed` to user room)

**Event Service** (`event_service.py:emit_stream_event`):
- Emits events to session-specific room: `session_{session_id}_stream`
- Event format: `{session_id, event_type, data: {..., event_seq}, timestamp}`
- Uses Socket.IO server's room-based broadcasting

**Agent Environment** (multi-adapter architecture):
- Receives message via SSE stream (`routes.py:chat_stream`)
- `sdk_manager.py:SDKManager` selects adapter based on `SDK_ADAPTER_*` ENV variables
- Adapter (e.g., `ClaudeCodeAdapter`) creates/resumes SDK client
- Streams responses via SDK, converts to unified `SDKEvent` format
- Yields SSE events (session_created, assistant, tool, thinking, done, error, interrupted)

### 2. Incremental Persistence

**Backend** (`message_service.py:stream_message_with_events`):

During streaming, the backend assigns `event_seq` to each event and periodically flushes to DB:

```python
event_seq_counter = 0
last_flush_time = time.time()
FLUSH_INTERVAL = 2.0  # seconds

for event in sse_stream:
    event_seq_counter += 1
    event["event_seq"] = event_seq_counter

    # Append to in-memory buffer
    streaming_events.append(event_copy)
    await active_streaming_manager.append_streaming_event(session_id, event_copy)

    # Emit via WebSocket (includes event_seq for frontend deduplication)
    emit_stream_event(session_id, event)

    # Periodic flush to DB (non-blocking background thread)
    if agent_message_id and (time.time() - last_flush_time >= FLUSH_INTERVAL):
        _flush_streaming_content(agent_message_id, streaming_events, event_seq_counter)
        last_flush_time = time.time()
```

The flush updates the agent message's:
- `content` field with accumulated assistant text
- `message_metadata.streaming_events` array (each event has its `event_seq`)
- `message_metadata.streaming_in_progress = True` (cleared to `False` on completion)

### 3. API Merges In-Memory Events

**Backend** (`messages.py:get_messages`):

When the messages API returns the message list, it checks if the session has an active stream with unflushed events:

```python
if await active_streaming_manager.is_streaming(session_id):
    stream_data = await active_streaming_manager.get_stream_events(session_id)
    if stream_data and stream_data["streaming_events"]:
        # Find in-progress message
        in_progress_msg = find_message_with_streaming_in_progress(messages)
        if in_progress_msg:
            # Merge: DB events + in-memory events beyond last flush
            db_events = in_progress_msg.message_metadata["streaming_events"]
            db_max_seq = max(e["event_seq"] for e in db_events)
            new_events = [e for e in stream_data["streaming_events"] if e["event_seq"] > db_max_seq]
            in_progress_msg.message_metadata["streaming_events"] = db_events + new_events
            in_progress_msg.content = stream_data["accumulated_content"]
```

**Key properties**:
- API always returns the most current data (DB + in-memory)
- No gap on page refresh - API gives you everything immediately
- Same format whether from DB or in-memory, so frontend handles them identically
- WS becomes purely a speed optimization (sub-second delivery) rather than a data completeness requirement

### 4. Streaming Response Event Types

- `stream_started` - Backend processing started (WebSocket only)
- `user_message_created` - User message saved (WebSocket only)
- `session_created` - External session ID created
- `assistant` - Text response from agent (has `event_seq`)
- `tool` - Tool use event (has `event_seq`, includes TodoWrite detection)
- `thinking` - Agent reasoning (has `event_seq`)
- `system` - System notification (has `event_seq`)
- `interrupted` - Message was interrupted
- `error` - Error occurred
- `stream_completed` - Stream finished (WebSocket only)
- `done` - Agent processing complete
- `session_interaction_status_changed` - Session streaming status changed (emitted to user room)
- `todo_list_updated` - TodoWrite tool detected, todos saved to session (backend event)
- `task_todo_updated` - Task-level todo update propagated from session (backend event)

**Event Flow**:
```
Agent Env SDK → Agent Env Server → Backend Service → WebSocket Room → Frontend Hook
   (format)        (SSE)              (emit_stream)     (Socket.IO)     (handler)
                                           ↓
                                   DB flush (every 2s)
                                           ↓
                                   ActiveStreamingManager buffer
```

### 5. Frontend State Management

**`useSessionStreaming` hook** (`hooks/useSessionStreaming.ts`):

The hook derives streaming state entirely from the session query:

```typescript
// DERIVED STATE: streaming is active when session says so
const isStreaming = session?.interaction_status === "running"
                 || session?.interaction_status === "pending_stream"
```

**Key differences from previous `useMessageStream`**:
- No local `isStreaming` state - derived from session query
- No `checkAndReconnectToActiveStream` - handled automatically by derived state
- No completion polling - session query detects completion via `interaction_status` change
- No message polling fallback - DB content always current (within 2s via incremental saves)
- Index-based dedup prevents any double-rendering

**State transitions**:
1. `isStreaming` becomes `true` → subscribe to WS room, initialize event list
2. WS events arrive → append to list (deduplicated by `event_seq`)
3. Message query refetches (every 2s) → merge DB events into list (fill gaps)
4. `session_interaction_status_changed` WS event received → invalidate session query
5. `isStreaming` becomes `false` → cleanup, refetch final messages

### 6. Dynamic Polling Intervals

**Session page** (`routes/_layout/session/$sessionId.tsx`):

```typescript
// Session query: faster during streaming for quicker status detection
const sessionQuery = useQuery({
  refetchInterval: isSessionStreaming ? 3000 : 10000,
})

// Messages query: poll during streaming for incremental content
const messagesQuery = useQuery({
  refetchInterval: isSessionStreaming ? 2000 : undefined,
})
```

### 7. Session Status WebSocket Events

**Backend** (`session_service.py`):

When `interaction_status` changes, a WebSocket event is emitted to the user room:
```python
# In handle_stream_started:
await event_service.emit_event(
    event_type="session_interaction_status_changed",
    model_id=session_id,
    meta={
        "session_id": str(session_id),
        "interaction_status": "running",
        "streaming_started_at": now.isoformat(),
    },
    user_id=user_id,
)

# In handle_stream_completed / handle_stream_error / handle_stream_interrupted:
await event_service.emit_event(
    event_type="session_interaction_status_changed",
    meta={"session_id": str(session_id), "interaction_status": ""},
    user_id=user_id,
)
```

**Frontend** (`routes/_layout/session/$sessionId.tsx`):

Subscribes to this event for immediate status detection:
```typescript
eventService.subscribe(EventTypes.SESSION_INTERACTION_STATUS_CHANGED, (event) => {
  if (event.meta?.session_id === sessionId) {
    queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
    if (event.meta?.interaction_status === "") {
      queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
    }
  }
})
```

This ensures the frontend reacts within milliseconds of streaming state changes, without waiting for the next poll interval.

## WebSocket Architecture

### Room-Based Streaming

**Room Naming**: `session_{session_id}_stream`

**Lifecycle**:
1. Frontend subscribes to room before sending message (tracked in `activeRooms`)
2. Backend emits all streaming events (with `event_seq`) to this room
3. Frontend receives events and updates unified event list (deduplicated by seq)
4. If WebSocket reconnects: frontend automatically re-subscribes to tracked rooms
5. Frontend unsubscribes from room when streaming ends (derived state becomes false)

**Event Service** (`event_service.py:EventService`):
- Global singleton instance manages Socket.IO server
- Handles client connections with user authentication
- Manages room subscriptions via `subscribe/unsubscribe` Socket.IO events
- Emits stream events via `emit_stream_event()` method

**Connection Management**:
- User-specific room: `user_{user_id}` (auto-joined on connect)
- Session streaming rooms: `session_{session_id}_stream` (subscribed on-demand)
- Authentication via Socket.IO auth parameter with user_id

### Background Task Execution

**Implementation** (`messages.py:send_message_stream` → `session_service.py:initiate_stream`):
- Creates user message with `sent_to_agent_status='pending'`
- Delegates to `SessionService.initiate_stream()` which:
  - Checks if environment needs activation
  - Spawns background task for environment activation if needed
  - Or immediately processes pending messages if environment active
- Uses `create_task_with_error_logging()` for background task management
- Frontend receives immediate response, then WebSocket events
- No SSE connection kept open

**Benefits**:
- Endpoint returns immediately (no long-running HTTP request)
- Background task can't be interrupted by HTTP client disconnect
- WebSocket room delivery is decoupled from task execution
- Multiple clients can subscribe to same room (multi-tab support ready)
- Automatic environment activation before streaming

### WebSocket Event Format

**Emitted by Backend** (`emit_stream_event`):
```json
{
  "session_id": "uuid-string",
  "event_type": "assistant|tool|thinking|system|...",
  "data": {
    "type": "assistant",
    "content": "Hello...",
    "event_seq": 42,
    "tool_name": "Read",
    "metadata": {...}
  },
  "timestamp": "2026-01-23T10:30:00.000Z"
}
```

**Frontend Subscription** (`useSessionStreaming.ts`):
- Subscribes to `"stream_event"` event type
- Handler receives full event object
- Extracts `event_seq` from `data.event_seq` (or `event.event_seq` fallback)
- Deduplicates by seq, appends to unified list or triggers refetch on gap

## WebSocket vs SSE

**Why WebSocket for Frontend-Backend**:
- No browser connection limits (SSE limited to 6 per domain)
- Reliable multi-tab usage without connection failures
- Better mobile browser support and battery efficiency
- Faster reconnection and lower latency
- Explicit error events and better debugging

**Why SSE for Backend-AgentEnv**:
- Agent environment SDK already uses SSE streaming
- No need to refactor agent environment code
- SSE sufficient for single backend-to-container connection
- Maintains backward compatibility with existing agent environments

## Session Management

### External Session IDs

SDK sessions persist across messages for context continuity.

**Storage**: `Session.session_metadata` JSON field (key: external_session_id)

**API** (`session_service.py`):
- `get_external_session_id(session)` - Retrieve for current SDK
- `set_external_session_id(db, session, external_session_id)` - Store/clear

### Active Stream Tracking

**Manager** (`active_streaming_manager.py:ActiveStreamingManager`):
- Tracks ongoing backend-to-agent-env streams
- Independent of frontend WebSocket connection state
- Holds in-memory event buffer between DB flushes
- Provides stream status and events for API response enrichment

**ActiveStream dataclass**:
```python
@dataclass
class ActiveStream:
    session_id: UUID
    external_session_id: Optional[str]
    started_at: datetime
    is_interrupted: bool = False
    is_completed: bool = False
    interrupt_pending: bool = False
    streaming_events: list = []      # In-memory event buffer
    last_flushed_seq: int = 0        # Last seq flushed to DB
    accumulated_content: str = ""     # Accumulated assistant text
```

**Key Methods**:
- `register_stream()` / `unregister_stream()` - Lifecycle management
- `append_streaming_event()` - Add event to in-memory buffer
- `update_last_flushed_seq()` - Track flush progress
- `get_stream_events()` - Get buffer for API merge (returns events + accumulated_content)
- `request_interrupt()` - Request stream interruption

**Endpoint**: `GET /sessions/{id}/messages/streaming-status`
- Uses DB `interaction_status` as primary source of truth
- Supplements with `ActiveStreamingManager` info (duration, external_session_id)

### Session State Auto-Reset on User Message

When an agent calls `update_session_state("needs_input")`, the session's `result_state` is set and the linked task transitions to `PENDING_INPUT`. When the user sends a follow-up message, the `result_state` must be automatically cleared so the task can transition back to `RUNNING`.

**Reset Logic** (`message_service.py:stream_message_with_events`):

Before streaming begins, `_get_session_context_and_reset_state()` handles the reset:

```python
def _get_session_context_and_reset_state():
    with get_fresh_db_session() as db:
        session_db = db.get(ChatSession, session_id)
        # ... get user_id and allowed_tools ...

        # Reset result_state when user sends a new message
        if session_db.result_state is not None:
            previous_result_state = session_db.result_state
            session_db.result_state = None
            session_db.result_summary = None
            db.commit()

            # Sync task status if session is linked to a task
            if session_db.source_task_id:
                InputTaskService.sync_task_status_from_sessions(
                    db_session=db, task_id=session_db.source_task_id
                )

        return user_id, allowed_tools, previous_result_state
```

**Session State Passthrough to Agent-Env**:

The previous state is passed to the agent environment as `session_state` context, enabling future prompt-level awareness of state transitions:

```python
# Build session_state for agent-env
session_state = None
if previous_result_state:
    session_state = {"previous_result_state": previous_result_state}

# Included in the SSE payload to agent-env
payload = {
    "message": user_message,
    "mode": mode,
    "session_id": external_session_id,
    "backend_session_id": backend_session_id,
}
if session_state:
    payload["session_state"] = session_state
```

**Agent-Env Pipeline**:

The `session_state` dict flows through the agent-env layers:
- `ChatRequest.session_state` (models) → `routes.py` → `SDKManager.send_message_stream()` → `BaseSDKAdapter.send_message_stream()`
- Adapters accept the parameter but don't use it yet (extensibility for future prompt integration)

**Task Status Sync Flow**:
```
User sends message → stream_message_with_events() starts
    → _get_session_context_and_reset_state()
        → result_state was "needs_input" → reset to None
        → result_summary → reset to None
        → commit to DB
        → InputTaskService.sync_task_status_from_sessions()
            → Task status: PENDING_INPUT → RUNNING
    → session_state = {"previous_result_state": "needs_input"}
    → send_message_to_environment_stream(..., session_state=session_state)
```

### Streaming Status

The streaming status is now primarily DB-based:

```python
@router.get("/{session_id}/messages/streaming-status")
async def get_streaming_status(...):
    # Use DB interaction_status as primary source of truth
    is_streaming = chat_session.interaction_status == "running"
    # ActiveStreamingManager provides supplementary info
    stream_info = await active_streaming_manager.get_stream_info(session_id)
```

### Todo Progress Tracking

When an agent uses the TodoWrite tool during message processing, the system tracks and propagates the progress:

**Data Flow:**
```
Agent uses TodoWrite → Backend detects tool event → Session.todo_progress updated
                                                  ↓
                                            TODO_LIST_UPDATED event emitted
                                                  ↓
                              InputTaskService.handle_todo_list_updated() handler
                                                  ↓
                                   If session linked to task via source_task_id:
                                     - Save to InputTask.todo_progress
                                     - Emit TASK_TODO_UPDATED event
                                                  ↓
                              Frontend Tasks list receives event via WebSocket
                                                  ↓
                                   TaskTodoProgress component updates UI
```

**Storage:**
- `Session.todo_progress` - JSON array of todo items, updated during streaming
- `InputTask.todo_progress` - JSON array persisted from session, survives page refresh

**Todo Item Structure:**
```json
{
  "content": "Run the build",
  "activeForm": "Running the build",
  "status": "pending" | "in_progress" | "completed"
}
```

## Frontend Display Architecture

### Message Rendering

**MessageList** (`components/Chat/MessageList.tsx`):
- Renders ALL messages including in-progress ones (no `streaming_in_progress` filter)
- Passes `isStreamingMessage` prop to `MessageBubble` when message has `streaming_in_progress=true`
- Shows `StreamingMessage` component at bottom with unified event list from the hook

**MessageBubble** (`components/Chat/MessageBubble.tsx`):
- When `isStreamingMessage=true`: shows pulsing dots indicator ("Streaming...")
- Content updates via message query refetch (every 2s during streaming)
- Once streaming completes: `streaming_in_progress` becomes `false`, indicator disappears

**StreamingMessage** (`components/Chat/StreamingMessage.tsx`):
- Renders the unified event list from the `useSessionStreaming` hook
- Events are deduplicated and ordered by `event_seq`
- Shows "Thinking..." loader when no events yet, pulsing dots while streaming

**StreamEventRenderer** (`components/Chat/StreamEventRenderer.tsx`):
- Renders individual events by type (assistant → markdown, tool → tool block, thinking → collapsible)
- Uses `event_seq` for React keys (stable across re-renders, prevents duplicate DOM nodes)

### Layout During Streaming

```
[...previous completed messages (MessageBubble)]
[In-progress agent message (MessageBubble with isStreamingMessage=true)]
  └─ Shows DB content (updated every 2s) with streaming indicator
[StreamingMessage: real-time events from hook (deduplicated unified list)]
  └─ Shows latest events from WS + DB, pulsing cursor
```

## Interruption Handling

### User Manual Interruption

**Frontend** (`useSessionStreaming.ts:stopMessage`):
1. Sets `isInterruptPending = true` (shows spinner on stop button)
2. Sends `POST /messages/interrupt`
3. Waits for `interaction_status` to clear (via session query or WS event)

**Backend Interrupt Flow**:
- `messages.py:interrupt_message` calls `active_streaming_manager.request_interrupt()`
- If external_session_id available: forwards to agent-env immediately
- If not available: queues as pending interrupt
- `message_service.py` forwards pending interrupt when session ID captured

**Agent Environment**:
- Receives interrupt via `POST /chat/interrupt/{external_session_id}`
- Sets flag in `active_session_manager`
- Streaming loop checks flag and calls `client.interrupt()`
- Yields interrupted event

**Backend Response**:
- Receives `interrupted` event from agent-env
- Emits to WebSocket room via `emit_stream_event()`
- Saves message with `status="user_interrupted"`, `streaming_in_progress=False`
- `handle_stream_interrupted` clears `interaction_status` and `streaming_started_at`
- Emits `session_interaction_status_changed` to user room

**Frontend Handling**:
- Receives `session_interaction_status_changed` event → invalidates session query
- `isStreaming` becomes false (derived from session query)
- Hook cleanup: unsubscribes from WS room, refetches messages
- Displays interrupted badge on the message

### Properties

- SDK cleanup ensures session not corrupted
- Partial content saved with interrupt status
- Session can be resumed in next message
- Race conditions handled via pending interrupt queue

## Error Handling

### WebSocket-Specific Errors

**Connection Errors**:
- Frontend: `eventService` handles reconnection automatically and re-subscribes to tracked rooms
- Backend: No change needed - WebSocket delivery is fire-and-forget
- Events missed during WS disconnection are recovered via message query polling (every 2s)
- Message query always returns complete data (DB + in-memory merge from ActiveStreamingManager)

**Backend Errors** (`initiate_stream` → `process_pending_messages`):
- Catches exceptions during environment activation or message processing
- Emits error event to WebSocket room
- `handle_stream_error` clears `interaction_status` and `streaming_started_at`
- Emits `session_interaction_status_changed` to user room
- Frontend detects error via session query change

### Session Corruption

Agent environment detects corruption, backend clears external_session_id, next message starts fresh.

### Network Errors

Backend-to-agent-env SSE errors handled same as before, yielded as error events, emitted via WebSocket and saved to DB.

## Reconnection & Recovery

### Page Refresh During Streaming

**How it works (no explicit reconnection logic needed)**:
1. Session query loads → `interaction_status === "running"` detected
2. `isStreaming` derived as `true` → hook subscribes to WS room
3. Messages query loads → API merges in-memory events from ActiveStreamingManager
4. In-progress message rendered with all events (DB + in-memory, zero gap)
5. WS events append new events (deduplicated by `event_seq`)
6. Content updates progressively via message polling (every 2s)
7. When streaming ends: session query detects `interaction_status=""` → cleanup

**User Experience**: Sees accumulated content immediately on load, streaming indicator shown, real-time events resume via WS within seconds.

### Navigation from Activities to Active Session

Same mechanism as page refresh - derived state from session query handles everything automatically. No special reconnection logic needed.

### WebSocket Reconnection During Streaming

**Problem**: Socket.IO may reconnect (transport upgrade, network blip) and get a new socket ID.

**Solution** (`eventService.ts`):
1. `subscribeToRoom()` adds room to `activeRooms` set (persists intent)
2. On Socket.IO `connect` event: re-emits `subscribe` for all tracked rooms
3. Any events missed during disconnection are recovered via message query refetch

**User Experience**: WS reconnects and room subscription restored automatically. Missed events filled by next message query poll (within 2s). No user-visible interruption.

### Backend Restart During Streaming

**Impact**:
- `ActiveStreamingManager` is in-memory, lost on restart
- Background streaming task killed
- DB has events up to last flush point (within 2s of crash)
- `interaction_status` may be stuck at "running" (no STREAM_COMPLETED emitted)
- WebSocket connections drop and reconnect automatically
- Frontend session query will continue to show `isStreaming=true` until manually resolved

**Recovery**: Requires manual intervention or a startup cleanup task to clear stale `interaction_status="running"` sessions.

## Database Schema

### Session Fields (streaming-related)

| Field | Type | Description |
|-------|------|-------------|
| `interaction_status` | `str` | `""` (idle), `"running"` (streaming), `"pending_stream"` (waiting for env) |
| `streaming_started_at` | `datetime | None` | When current stream started (cleared on end) |
| `status` | `str` | `"active"`, `"completed"`, `"error"` |
| `pending_messages_count` | `int` | Number of unsent user messages |
| `result_state` | `str | None` | Agent-set state (e.g., `"needs_input"`, `"completed"`). Auto-reset to null on next user message. |
| `result_summary` | `str | None` | Agent-set summary text. Auto-reset to null on next user message. |
| `source_task_id` | `UUID | None` | Linked InputTask ID. Used for task status sync on state reset. |

### SessionMessage Fields (streaming-related)

| Field | Type | Description |
|-------|------|-------------|
| `content` | `str` | Accumulated text (updated every 2s during streaming) |
| `message_metadata.streaming_in_progress` | `bool` | Whether message is still being generated |
| `message_metadata.streaming_events` | `list[dict]` | Array of events with `event_seq`, `type`, `content` |
| `message_metadata.model` | `str` | Model used for generation |
| `status` | `str` | `""`, `"user_interrupted"`, `"error"` |

## Implementation Details

### Message Sequence Ordering

Agent message created early on first `assistant` event to ensure correct sequence numbers before tool executions (like handover or task creation).

### Periodic Flush Implementation

The flush runs in a background thread (`asyncio.to_thread`) to avoid blocking the streaming loop:

```python
def _flush_streaming_content(msg_id, content, events, seq, metadata):
    from sqlalchemy.orm.attributes import flag_modified
    with get_fresh_db_session() as db:
        agent_msg = db.get(SessionMessage, msg_id)
        if agent_msg:
            agent_msg.content = content
            metadata["streaming_in_progress"] = True
            metadata["streaming_events"] = events
            agent_msg.message_metadata = metadata
            flag_modified(agent_msg, "message_metadata")
            db.add(agent_msg)
            db.commit()

await asyncio.to_thread(_flush_streaming_content)
await active_streaming_manager.update_last_flushed_seq(session_id, flush_seq)
```

### What Was Removed (Complexity Reduction)

| Removed | Reason |
|---------|--------|
| `checkAndReconnectToActiveStream()` | Derived state from session query handles this |
| Completion polling interval (15s delay + 3s poll) | Session query detects `interaction_status` change via WS event |
| Message polling fallback (3s interval in hook) | DB always has current content via incremental saves + query refetch |
| `streaming_in_progress` message filter | Messages shown immediately with streaming indicator |
| Local `isStreaming` state in hook | Derived from `session.interaction_status` |
| `streamCompleteCalledRef` guard | No duplicate completion issue with derived state |
| `useMessageStream.ts` | Replaced by `useSessionStreaming.ts` |

## File Reference

### Frontend
- `hooks/useSessionStreaming.ts` - Streaming hook with derived state, seq-based dedup, unified event list
- `services/eventService.ts` - Socket.IO client, stream_event handling, room management
- `routes/_layout/session/$sessionId.tsx` - Session page with dynamic polling and WS status listener
- `components/Chat/MessageInput.tsx` - Send/stop UI
- `components/Chat/MessageBubble.tsx` - Message display with streaming indicator for in-progress messages
- `components/Chat/StreamEventRenderer.tsx` - Event rendering by type (uses `event_seq` for keys)
- `components/Chat/MessageList.tsx` - Message list, renders in-progress messages with indicator
- `components/Chat/StreamingMessage.tsx` - Real-time streaming display with unified event list
- `components/Tasks/TaskTodoProgress.tsx` - Todo progress display component for tasks
- `routes/_layout/tasks.tsx` - Tasks list with real-time todo progress updates

### Backend
- `api/routes/messages.py` - Message endpoints with in-memory event merge and DB-based streaming status
- `services/session_service.py` - Stream lifecycle handlers, `session_interaction_status_changed` emission
- `services/message_service.py` - `stream_message_with_events()` with event_seq, incremental flush, TodoWrite detection
- `services/event_service.py` - EventService with emit_stream_event() and backend event handlers
- `services/active_streaming_manager.py` - Stream tracking with in-memory event buffer
- `services/activity_service.py` - Event handlers for streaming lifecycle
- `services/input_task_service.py` - handle_todo_list_updated() for task todo propagation
- `models/session.py` - Session model with `streaming_started_at`, `todo_progress` fields
- `models/event.py` - EventType constants including `SESSION_INTERACTION_STATUS_CHANGED`
- `main.py` - Event handler registration on startup

### Agent Environment (multi-adapter architecture)
- `env-templates/app_core_base/core/server/routes.py` - SSE endpoints
- `env-templates/app_core_base/core/server/sdk_manager.py` - Multi-adapter SDK manager
- `env-templates/app_core_base/core/server/adapters/` - Adapter implementations
  - `base.py` - `SDKEvent`, `SDKEventType`, `SDKConfig`, `BaseSDKAdapter`, `AdapterRegistry`
  - `claude_code.py` - `ClaudeCodeAdapter` for claude-code/* variants
  - `google_adk.py` - `GoogleADKAdapter` placeholder for google-adk-wr/*
- `env-templates/app_core_base/core/server/sdk_utils.py` - Logging utilities
- `env-templates/app_core_base/core/server/active_session_manager.py` - Interrupt tracking

## Transport Layer Summary

**Frontend-Backend**: WebSocket (Socket.IO)
- Room-based event broadcasting
- Background task execution
- Immediate HTTP response + async events
- `session_interaction_status_changed` events for instant state updates

**Backend-AgentEnv**: SSE (Server-Sent Events)
- HTTP streaming from agent environment
- Compatible with existing SDK implementations
- No changes to agent environment code required

## Background Task Management Best Practices

### Critical Patterns for Async Task Creation

Based on production debugging of streaming cancellation issues, follow these patterns to avoid task cancellation:

#### 1. NEVER Use `asyncio.run()` in WebSocket Handlers

**Problem**: `asyncio.run()` creates a **temporary event loop** that destroys all child tasks when it completes.

**Correct Pattern**:
```python
async def on_message(data):
    # Create background task in the CURRENT event loop
    asyncio.create_task(some_async_function())  # ✅ CORRECT
```

#### 2. Don't Await Functions That Create Background Tasks

**Problem**: When you await a function that spawns background tasks, those tasks become children of the awaiting context and get cancelled when it returns.

**Correct Pattern**:
```python
async def handle_event():
    create_task_with_error_logging(
        initiate_stream(session_id),
        task_name=f"initiate_stream_{session_id}"
    )  # ✅ CORRECT - task is independent
```

#### 3. Always Log Background Task Errors

Use `create_task_with_error_logging()` from `backend/app/utils.py` to prevent silent failures.

#### 4. Avoid Passing Detached ORM Objects to Background Tasks

Store IDs and fetch fresh objects with a new DB session in the background task.

### Implementation Files Using These Patterns

- `backend/app/utils.py` - `create_task_with_error_logging()` utility function
- `backend/app/services/session_service.py` - `initiate_stream()`, `process_pending_messages()`, stream event handlers
- `backend/app/services/event_service.py` - `agent_usage_intent` handler
- `backend/app/services/message_service.py` - `_flush_streaming_content` (runs in background thread)

## Future Enhancements

- **Multi-Tab Streaming**: Multiple frontend tabs see same stream via shared WebSocket room
- **Persistent Stream Tracking**: Store active streams in Redis to survive backend restarts (fixes stale `interaction_status` issue)
- **Startup Cleanup**: On backend start, scan for sessions with `interaction_status="running"` and reset them
- **WebSocket Heartbeat**: Implement ping/pong for connection health monitoring
- **Compression**: Enable WebSocket compression for large streaming events
- **Migrate Agent-Env to WebSocket**: Future consideration for full WebSocket architecture
