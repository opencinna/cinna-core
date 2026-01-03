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
- Launches background task via `BackgroundTasks.add_task()`
- Calls `MessageService.handle_stream_message_websocket()` in background
- Returns immediately with response: `{status: "ok", stream_room: "session_{id}_stream"}`

**Backend Service** (`message_service.py:handle_stream_message_websocket`):
Orchestrates WebSocket streaming:
1. Emits `stream_started` event via `event_service.emit_stream_event()`
2. Saves user message to database immediately
3. Auto-generates session title if needed
4. Sets session status to "active"
5. Delegates to `stream_message_with_events()` which streams from agent-env via SSE
6. Emits each event to WebSocket room as received
7. Emits `stream_completed` event when done
8. On error: emits error event with exception details

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
- Streams from agent-env via SSE (unchanged from original implementation)
- Collects events in memory
- Captures external session ID early
- Creates agent message placeholder on first `assistant` event
- Saves final agent response when stream completes
- Updates session status and syncs prompts (building mode)
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

**Implementation** (`messages.py:send_message_stream`):
- Uses FastAPI's `BackgroundTasks` to run `handle_stream_message_websocket()`
- Task executes independently after endpoint returns
- Frontend receives immediate response, then WebSocket events
- No SSE connection kept open

**Benefits**:
- Endpoint returns immediately (no long-running HTTP request)
- Background task can't be interrupted by HTTP client disconnect
- WebSocket room delivery is decoupled from task execution
- Multiple clients can subscribe to same room (multi-tab support ready)

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

**Backend Errors** (`handle_stream_message_websocket`):
- Catches exceptions during streaming setup or processing
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
- `api/routes/messages.py` - WebSocket-based endpoint with BackgroundTasks
- `services/event_service.py` - EventService with emit_stream_event() method
- `services/message_service.py` - handle_stream_message_websocket() and stream_message_with_events()
- `services/active_streaming_manager.py` - Stream tracking (unchanged)
- `services/session_service.py` - Session/external ID management (unchanged)

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

## Future Enhancements

- **Multi-Tab Streaming**: Multiple frontend tabs see same stream via shared WebSocket room
- **Persistent Stream Tracking**: Store active streams in Redis to survive backend restarts
- **WebSocket Heartbeat**: Implement ping/pong for connection health monitoring
- **Event Replay**: Buffer recent events in backend for reconnecting clients
- **Compression**: Enable WebSocket compression for large streaming events
- **Migrate Agent-Env to WebSocket**: Future consideration for full WebSocket architecture
