# Frontend-Backend-AgentEnv Streaming Architecture

## Overview

The system implements a three-layer streaming architecture with **WebSocket** for frontend-backend communication and **SSE** for backend-agent-env communication. This hybrid approach eliminates browser connection limits while maintaining compatibility with the agent environment's SSE-based SDK streaming.

**Key Principles**:
- Frontend-backend streaming is decoupled via WebSocket rooms and background tasks
- Backend-agent-env streaming remains independent using SSE
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

## Message Flow

### 1. Sending a Message

**Frontend** (`useMessageStream.ts:sendMessage`):
- Subscribes to session-specific WebSocket room: `session_{session_id}_stream`
- Subscribes to `stream_event` events via `eventService`
- Sends POST to `/api/v1/sessions/{session_id}/messages/stream`
- Optimistically adds user message to cache
- Sets streaming state

**Backend API** (`messages.py:send_message_stream`):
- Validates session ownership
- Handles file attachments via `MessageService.prepare_user_message_with_files()` if present
- Creates user message with `sent_to_agent_status='pending'`
- Delegates to `SessionService.initiate_stream()` at line 116
- Returns immediately with response: `{status: "ok", stream_room: "session_{id}_stream"}`

**Backend Service** (`session_service.py:initiate_stream`):
Orchestrates pending message processing and environment activation:
1. Checks if environment needs activation (suspended/inactive)
2. If activation needed:
   - Activates environment in background task
   - Environment sends `agent_usage_intent` event when ready
   - Event handler calls `process_pending_messages()` to start streaming
3. If environment already active:
   - Immediately processes pending messages via `process_pending_messages()`
4. `process_pending_messages()` handles streaming:
   - Emits `STREAM_STARTED` backend event (triggers activity creation)
   - Streams from agent-env via SSE
   - Emits `STREAM_ERROR`/`STREAM_INTERRUPTED` backend events as needed
   - Emits `STREAM_COMPLETED` backend event (triggers activity/environment handlers)
   - Emits each streaming event to WebSocket room
   - Emits `stream_completed` WebSocket event when done

**Event Service** (`event_service.py:emit_stream_event`):
- Emits events to session-specific room: `session_{session_id}_stream`
- Event format: `{session_id, event_type, data, timestamp}`
- Uses Socket.IO server's room-based broadcasting

**Agent Environment** (unchanged):
- Receives message via SSE stream (`routes.py:chat_stream`)
- Creates/resumes SDK client via `sdk_manager.py:send_message_stream`
- Streams responses via SDK
- Yields SSE events (assistant, tool, thinking, result)

### 2. Streaming Response

**Event Types** (unchanged):
- `stream_started` - Backend processing started (WebSocket only)
- `user_message_created` - User message saved (WebSocket only)
- `session_created` - External session ID created
- `assistant` - Text response from agent
- `tool` - Tool use event
- `thinking` - Agent reasoning
- `system` - System notification
- `interrupted` - Message was interrupted
- `error` - Error occurred
- `stream_completed` - Stream finished (WebSocket only)
- `done` - Agent processing complete

**Event Flow**:
```
Agent Env SDK → Agent Env Server → Backend Service → WebSocket Room → Frontend Hook
   (format)        (SSE)              (emit_stream)     (Socket.IO)     (handler)
```

**Frontend Handling** (`useMessageStream.ts:handleStreamEvent`):
- Receives events via WebSocket through `eventService.subscribe("stream_event")`
- Verifies event belongs to current session
- Transforms into `StructuredStreamEvent[]` for real-time display
- Updates `streamingEvents` state
- Calls `handleStreamComplete()` on `stream_completed` or `interrupted`
- Cleans up subscriptions and unsubscribes from room

**Frontend Event Service** (`eventService.ts`):
- Listens for `stream_event` Socket.IO events
- Routes to subscribers via `handleStreamEvent()` method
- Manages room subscriptions via `subscribeToRoom()/unsubscribeFromRoom()`

**Backend Processing** (`message_service.py:stream_message_with_events`):
- Emits `STREAM_STARTED` backend event (for activity tracking)
- Streams from agent-env via SSE
- Collects events in memory
- Captures external session ID early
- Creates agent message placeholder on first `assistant` event
- Saves final agent response when stream completes
- Emits `STREAM_COMPLETED`/`STREAM_ERROR`/`STREAM_INTERRUPTED` backend events
- Backend event handlers (ActivityService, EnvironmentService) react to events
- Tracked by `ActiveStreamingManager`

### 3. WebSocket vs SSE

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

## WebSocket Architecture

### Room-Based Streaming

**Room Naming**: `session_{session_id}_stream`

**Lifecycle**:
1. Frontend subscribes to room before sending message
2. Backend emits all streaming events to this room
3. Frontend receives events and updates UI
4. Frontend unsubscribes from room when stream completes

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
- Uses `_create_task_with_error_logging()` for background task management
- Frontend receives immediate response, then WebSocket events
- No SSE connection kept open

**Benefits**:
- Endpoint returns immediately (no long-running HTTP request)
- Background task can't be interrupted by HTTP client disconnect
- WebSocket room delivery is decoupled from task execution
- Multiple clients can subscribe to same room (multi-tab support ready)
- Automatic environment activation before streaming

## Session Management

### External Session IDs

SDK sessions persist across messages for context continuity (unchanged).

**Storage**: `Session.external_session_mappings` JSON field

**API** (`session_service.py`):
- `get_external_session_id(session)` - Retrieve for current SDK
- `set_external_session_id(db, session, external_session_id)` - Store/clear

### Active Stream Tracking

**Manager** (`active_streaming_manager.py:ActiveStreamingManager`):
- Tracks ongoing backend-to-agent-env streams (unchanged)
- Independent of frontend WebSocket connection state
- Provides stream status for reconnection

**Endpoint**: `GET /sessions/{id}/messages/streaming-status` (unchanged)

**Usage**:
- Frontend checks on mount via `checkAndReconnectToActiveStream()`
- Enables reconnection after page refresh
- Polls status every 1s until stream completes
- Shows streaming UI without active WebSocket subscription

## Interruption Handling

### User Manual Interruption

**Frontend** (`useMessageStream.ts:stopMessage`):
1. Sets `isInterruptPending = true` (shows spinner)
2. Sends `POST /messages/interrupt`
3. Waits for `interrupted` event via WebSocket

**Backend Interrupt Flow** (unchanged from SSE implementation):
- `messages.py:interrupt_message` calls `active_streaming_manager.request_interrupt()`
- If external_session_id available: forwards to agent-env immediately
- If not available: queues as pending interrupt
- `message_service.py` forwards pending interrupt when session ID captured

**Agent Environment** (unchanged):
- Receives interrupt via `POST /chat/interrupt/{external_session_id}`
- Sets flag in `active_session_manager`
- Streaming loop checks flag and calls `client.interrupt()`
- Yields interrupted event

**Backend Response**:
- Receives `interrupted` event from agent-env
- Emits to WebSocket room via `emit_stream_event()`
- Saves message with `status="user_interrupted"`
- Sets session status to "active" (not "completed")

**Frontend Handling**:
- Receives `interrupted` event via WebSocket
- Clears `isInterruptPending` flag
- Calls `handleStreamComplete(wasInterrupted=true)`
- Displays interrupted badge and system notification

### Properties (unchanged)

- SDK cleanup ensures session not corrupted
- Partial content saved with interrupt status
- Session can be resumed in next message
- Race conditions handled via pending interrupt queue

## Error Handling

### WebSocket-Specific Errors

**Connection Errors**:
- Frontend: `eventService` handles reconnection automatically
- Backend: No change needed - WebSocket delivery is fire-and-forget
- Error events still emitted to room even if client temporarily disconnected

**Backend Errors** (`initiate_stream` → `process_pending_messages`):
- Catches exceptions during environment activation or message processing
- Emits error event to WebSocket room via `emit_stream_event()`
- Includes error type and message in event data
- Frontend receives and displays error

### Session Corruption (unchanged)

Agent environment detects corruption, backend clears external_session_id, next message starts fresh.

### Network Errors (unchanged)

Backend-to-agent-env SSE errors handled same as before, yielded as error events, now emitted via WebSocket.

## Reconnection & Recovery

### Page Refresh During Streaming

**Frontend** (`useMessageStream.ts:checkAndReconnectToActiveStream`):
1. Runs on mount (checks `hasCheckedForActiveStream` ref)
2. Fetches `GET /streaming-status`
3. If streaming:
   - Subscribes to WebSocket room
   - Sets `isStreaming = true`
   - Subscribes to `stream_event` via `eventService`
   - Refreshes messages from database
   - Polls `/streaming-status` every 1s until complete
4. Cleanup: unsubscribes when stream completes

**User Experience**: Sees spinning indicator, partial message content, automatically updates when complete, no data loss

### Backend Restart During Streaming

**Impact** (unchanged):
- `ActiveStreamingManager` is in-memory, lost on restart
- WebSocket connections drop and reconnect automatically
- Frontend checks `/streaming-status`, sees streaming=false
- Messages already in database are visible
- Streaming UI not shown but data preserved

## Database Schema

(Unchanged - see Session and SessionMessage tables in original document)

## Implementation Details

### Message Sequence Ordering

(Unchanged - agent message created early on first `assistant` event to ensure correct sequence numbers before tool executions)

### WebSocket Event Format

**Emitted by Backend** (`emit_stream_event`):
```
{
  session_id: string,
  event_type: string,
  data: {...event data from agent-env...},
  timestamp: ISO string
}
```

**Frontend Subscription** (`useMessageStream.ts`):
- Subscribes to `"stream_event"` event type
- Handler receives full event object
- Verifies `session_id` matches current session
- Routes based on `event_type`
- Processes `data` field (original event from agent-env)

## File Reference

### Frontend
- `hooks/useMessageStream.ts` - WebSocket-based streaming, room subscription/unsubscription
- `services/eventService.ts` - Socket.IO client, stream_event handling, room management
- `components/Chat/MessageInput.tsx` - Send/stop UI
- `components/Chat/MessageBubble.tsx` - Interrupted badge display
- `components/Chat/StreamEventRenderer.tsx` - Streaming event rendering
- `components/Chat/MessageList.tsx` - Message list, filters in-progress placeholders
- `components/Chat/StreamingMessage.tsx` - Real-time streaming display

### Backend
- `api/routes/messages.py` - Message endpoint with file handling and streaming initiation (line 116)
- `services/session_service.py` - initiate_stream(), process_pending_messages(), environment activation logic
- `services/message_service.py` - prepare_user_message_with_files(), stream_message_with_events()
- `services/event_service.py` - EventService with emit_stream_event() and backend event handlers
- `services/activity_service.py` - Event handlers for streaming lifecycle (handle_stream_started, etc.)
- `services/active_streaming_manager.py` - Stream tracking
- `main.py` - Event handler registration on startup

### Agent Environment (unchanged)
- `env-templates/python-env-advanced/app/core/server/routes.py` - SSE endpoints
- `env-templates/python-env-advanced/app/core/server/sdk_manager.py` - SDK streaming
- `env-templates/python-env-advanced/app/core/server/sdk_utils.py` - Message formatting
- `env-templates/python-env-advanced/app/core/server/active_session_manager.py` - Interrupt tracking

## Transport Layer Summary

**Frontend-Backend**: WebSocket (Socket.IO)
- Room-based event broadcasting
- Background task execution
- Immediate HTTP response + async events

**Backend-AgentEnv**: SSE (Server-Sent Events)
- HTTP streaming from agent environment
- Compatible with existing SDK implementations
- No changes to agent environment code required

## Background Task Management Best Practices

### Critical Patterns for Async Task Creation

Based on production debugging of streaming cancellation issues, follow these patterns to avoid task cancellation:

#### 1. NEVER Use `asyncio.run()` in WebSocket Handlers

**Problem**: `asyncio.run()` creates a **temporary event loop** that destroys all child tasks when it completes.

**Bad Example**:
```python
# In WebSocket handler (which is already async!)
async def on_message(data):
    # This creates a NEW event loop, runs the function, then DESTROYS the loop
    self.executor.submit(
        lambda: asyncio.run(some_async_function())  # ❌ WRONG!
    )
```

**Correct Pattern**:
```python
# Use the current event loop
async def on_message(data):
    # Create background task in the CURRENT event loop
    asyncio.create_task(some_async_function())  # ✅ CORRECT
```

**Why**: WebSocket handlers already run in FastAPI's event loop. Creating a new loop with `asyncio.run()` causes all tasks spawned during that function to be cancelled when the temporary loop closes.

#### 2. Don't Await Functions That Create Background Tasks

**Problem**: When you await a function that spawns background tasks, those tasks become children of the awaiting context. When the awaited function returns, asyncio cancels the child tasks.

**Bad Example**:
```python
async def handle_event():
    # initiate_stream() creates background tasks and returns immediately
    await initiate_stream(session_id)  # ❌ WRONG - child tasks will be cancelled!
    # When this function returns, background tasks get cancelled
```

**Correct Pattern**:
```python
async def handle_event():
    # Wrap in create_task to make it independent
    _create_task_with_error_logging(
        initiate_stream(session_id),
        task_name=f"initiate_stream_{session_id}"
    )  # ✅ CORRECT - task is independent
```

**Why**: Background tasks need to be independent of their creator's lifecycle. Using `create_task` creates a top-level task that won't be cancelled when the parent function returns.

#### 3. Always Log Background Task Errors

**Problem**: By default, background tasks that fail silently swallow exceptions, making debugging impossible.

**Solution**: Use the `_create_task_with_error_logging()` helper:

```python
def _create_task_with_error_logging(coro, task_name: str = "background_task"):
    """Create an asyncio task with proper exception logging."""
    task = asyncio.create_task(coro)

    def _handle_task_result(task):
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info(f"Task {task_name} was cancelled")
        except Exception as e:
            logger.error(f"Unhandled exception in {task_name}: {e}", exc_info=True)

    task.add_done_callback(_handle_task_result)
    return task
```

**Usage**:
```python
_create_task_with_error_logging(
    process_message(session_id),
    task_name=f"process_message_{session_id}"
)
```

**Benefits**:
- Logs all exceptions with full stack traces
- Logs task cancellations for debugging
- Keeps task reference to prevent premature garbage collection

#### 4. Avoid Passing Detached ORM Objects to Background Tasks

**Problem**: SQLAlchemy objects become "detached" when their originating session closes. Background tasks with different sessions cannot use detached objects.

**Bad Example**:
```python
async def create_background_task(db: Session):
    environment = db.get(AgentEnvironment, env_id)
    agent = db.get(Agent, agent_id)

    # This background task will fail - objects are detached from db session!
    _create_task_with_error_logging(
        lifecycle_manager.activate_environment(
            db_session=get_new_session(),  # Different session!
            environment=environment,        # ❌ Detached object
            agent=agent                    # ❌ Detached object
        )
    )
```

**Correct Pattern**:
```python
async def create_background_task(db: Session):
    # Store only IDs, not ORM objects
    environment_id = some_environment.id
    agent_id = some_agent.id

    # Background task fetches fresh objects with its own session
    async def _task_with_fresh_objects():
        with get_new_session() as fresh_db:
            fresh_env = fresh_db.get(AgentEnvironment, environment_id)
            fresh_agent = fresh_db.get(Agent, agent_id)

            return await lifecycle_manager.activate_environment(
                db_session=fresh_db,
                environment=fresh_env,
                agent=fresh_agent
            )

    _create_task_with_error_logging(
        _task_with_fresh_objects(),
        task_name=f"activate_env_{environment_id}"
    )
```

**Why**: Each background task should manage its own database session and fetch fresh ORM objects. This ensures objects are properly attached and prevents SQLAlchemy errors.

### Implementation Files Using These Patterns

- `backend/app/services/session_service.py` - `_create_task_with_error_logging()`, `initiate_stream()`, `process_pending_messages()`
- `backend/app/services/event_service.py` - `agent_usage_intent` handler, background task creation for environment activation

### Testing Background Task Issues

When debugging streaming or background task problems:

1. **Check for task cancellation logs**: `grep "was cancelled" backend.log`
2. **Check for unhandled exceptions**: `grep "Unhandled exception" backend.log`
3. **Verify no `asyncio.run()` in async contexts**: `grep -r "asyncio.run" backend/app/`
4. **Look for awaited background task creators**: Search for `await` followed by functions that use `create_task`

## Future Enhancements

- **Multi-Tab Streaming**: Multiple frontend tabs see same stream via shared WebSocket room
- **Persistent Stream Tracking**: Store active streams in Redis to survive backend restarts
- **WebSocket Heartbeat**: Implement ping/pong for connection health monitoring
- **Event Replay**: Buffer recent events in backend for reconnecting clients
- **Compression**: Enable WebSocket compression for large streaming events
- **Migrate Agent-Env to WebSocket**: Future consideration for full WebSocket architecture
