# Frontend-Backend-AgentEnv Streaming Architecture

## Overview

The system implements a three-layer streaming architecture that decouples frontend connections from backend-to-agent-env processing. This ensures message processing continues even if the frontend disconnects, and enables graceful handling of interruptions.

**Key Principle**: Backend-to-agent-env streaming is independent of frontend-to-backend streaming.

## Architecture Layers

```
┌─────────────┐         ┌─────────────┐         ┌──────────────────┐
│  Frontend   │ ◄─SSE──►│   Backend   │ ◄─SSE──►│  Agent Env       │
│             │         │             │         │  (Docker)        │
│ React/TS    │         │ FastAPI     │         │ FastAPI + SDK    │
└─────────────┘         └─────────────┘         └──────────────────┘
     │                       │                          │
     │                       │                          │
   Local                 PostgreSQL                Workspace
  Storage                Sessions/                 Files/Logs
                         Messages
```

## Message Flow

### 1. Sending a Message

#### Via Frontend

**Frontend** (`useMessageStream.ts:sendMessage`):
- Opens SSE connection to `/api/v1/sessions/{session_id}/messages/stream`
- Optimistically adds user message to cache
- Sets streaming state

**Backend** (`messages.py:send_message_stream`):
- Saves user message to database immediately
- Registers stream in `ActiveStreamingManager`
- Delegates to `MessageService.stream_message_with_events()`

#### Via Agent Handover

**Agent Tool** (`tools/agent_handover.py:agent_handover`):
- Calls backend endpoint `POST /agents/handover/execute`

**Backend** (`agents.py:execute_handover`):
- Async endpoint delegates to `AgentService.execute_handover()`

**Service** (`agent_service.py:execute_handover`):
- Creates new session for target agent
- Launches background task using `asyncio.create_task()`
- Background task calls `MessageService.handle_stream_message()` to process message
- Returns immediately without blocking

**Backend** (`message_service.py:stream_message_with_events`):
Orchestrates the complete flow:
1. Registers active stream for tracking
2. Connects to agent environment via `send_message_to_environment_stream()`
3. Proxies events from agent env to frontend
4. Saves agent response to database
5. Updates session status
6. Syncs prompts (if building mode)
7. Unregisters stream when done

**Agent Environment** (`routes.py:chat_stream` → `sdk_manager.py:send_message_stream`):
- Creates/resumes SDK client (`ClaudeSDKClient`)
- Registers session in `ActiveSessionManager`
- Sends message via `client.query()`
- Streams responses via `client.receive_messages()`
- Formats and yields events (assistant, tool, thinking, result)
- Unregisters session when done

### 2. Streaming Response

**Event Types**:
- `session_created` - New external session ID created
- `assistant` - Text response from agent
- `tool` - Tool use event
- `thinking` - Agent reasoning (Claude Sonnet 4+)
- `system` - System notification (e.g., interrupt notification)
- `result` - Final result with metadata
- `interrupted` - Message was interrupted
- `error` - Error occurred
- `done` - Stream completed

**Event Flow**:
```
Agent Env SDK → Agent Env Server → Backend Service → Backend API → Frontend Hook
   (format)        (SSE)              (process)        (SSE)         (render)
```

**Frontend Handling** (`useMessageStream.ts`):
- Receives events via SSE and transforms into `StructuredStreamEvent[]`
- Updates `streamingEvents` state for real-time display
- Handles special events (session_created, interrupted, error, done)
- Invalidates queries when stream completes
- Polls for AI-generated title

**Backend Processing** (`message_service.py`):
- Collects streaming events in memory
- Captures external session ID from early response
- Updates `ActiveStreamingManager` with session ID
- Saves agent message to database when stream completes
- Detects `AskUserQuestion` tool usage for follow-up handling
- Updates session status (active → completed/interrupted)

## Session Management

### External Session IDs

SDK sessions persist across multiple messages for context continuity.

**Storage**: `Session.external_session_mappings` - JSON mapping
```json
{"claude": "abc123-def456"}
```

**Lifecycle**:
1. First message: no external session ID
2. Agent env creates SDK session
3. Backend captures session ID from `session_created` event (within ~1 second)
4. Stored via `SessionService.set_external_session_id()`
5. Subsequent messages: resume with stored session ID
6. Corruption: clear session ID and start fresh

**API** (`session_service.py`):
- `get_external_session_id()` - Retrieve session ID for current SDK
- `set_external_session_id()` - Store or clear session ID

### Active Stream Tracking

**Manager** (`active_streaming_manager.py:ActiveStreamingManager`):
- Tracks ongoing backend-to-agent-env streams
- Independent of frontend connection state
- Provides stream status for reconnection

**Endpoint**: `GET /sessions/{id}/messages/streaming-status`

Returns: `{is_streaming: bool, stream_info: {...}}`

**Usage**:
- Frontend checks on mount to detect interrupted streams
- Enables reconnection after page refresh
- Shows streaming UI even without active SSE connection

## Decoupled Streaming Design

### Problem Solved

**Old Behavior**: Frontend abort → Backend stream closed → Agent env stream closed → Corrupted SDK sessions, lost messages

**New Behavior**: Frontend disconnect → Backend continues → Agent env completes normally → All data saved, session intact

**Agent Handovers**: Use the same decoupled architecture - background task processes message independently without any frontend connection, ensuring reliable agent-to-agent communication

### Implementation Challenge: Python Generator Chain Closure

**The Issue Encountered**:

When the frontend disconnected (browser closed), Python's async generator chain automatically closed all generators in the chain, causing the backend-to-agent-env stream to terminate prematurely. This happened because:

1. FastAPI's `StreamingResponse` detected client disconnect and raised `asyncio.CancelledError`
2. Python's async generator protocol automatically called `.aclose()` on the generator being consumed
3. This closure propagated up the entire chain: `event_stream()` → `stream_message_with_events()` → `send_message_to_environment_stream()`
4. The `finally` block in `stream_message_with_events()` executed immediately, unregistering the stream
5. Result: Agent response never saved, session left in inconsistent state, external session ID lost

**Symptoms**:
- Stream duration very short (0.9-6.6 seconds) when frontend disconnected
- Log: "Unregistered stream" appeared BEFORE frontend disconnect warning
- No "Agent response saved" message in logs
- Zero events consumed after disconnect attempt
- No communication with agent environment completed

**Root Cause**: Attempting to continue consuming from an already-closed generator in a background task is impossible. The generator chain is tightly coupled by Python's async iteration protocol.

### Queue-Based Decoupling Solution

**Architecture** (`messages.py:send_message_stream`):

The solution uses an `asyncio.Queue` to break the generator chain coupling:

```
┌─────────────────────────────────────────────────────┐
│ Background Task (independent lifecycle)             │
│  - Consumes from stream_message_with_events()       │
│  - Pushes all events to asyncio.Queue               │
│  - Runs to completion even if frontend disconnects  │
└──────────────┬──────────────────────────────────────┘
               │ asyncio.Queue (unbounded)
               ↓
┌──────────────────────────────────────────────────────┐
│ Frontend Generator (can disconnect anytime)          │
│  - Reads events from queue                           │
│  - Yields as SSE to client                           │
│  - On disconnect: raises exception, queue remains    │
└──────────────────────────────────────────────────────┘
```

**Implementation** (`messages.py:event_stream` method in `send_message_stream` endpoint):

1. **Create Queue First**: Unbounded `asyncio.Queue` for event passing
2. **Start Background Task**: `stream_consumer_task()` is launched BEFORE yielding to frontend
3. **Consumer Task**:
   - Independently consumes from `MessageService.stream_message_with_events()`
   - Puts all events into queue (even after frontend disconnects)
   - Signals completion by putting `None` in queue
   - Logs whether frontend was still connected at completion
4. **Frontend Loop**: Simple `while True` loop reading from queue and yielding SSE events
5. **On Disconnect**: Exception caught, `frontend_connected` flag set to `False`, but consumer task **not cancelled**
6. **Key Property**: Background task lifecycle is independent - not tied to generator chain

**Critical Implementation Details**:

- Task is created with `asyncio.create_task()` BEFORE any `yield` statements
- Task reference (`consumer_task`) intentionally not stored for later cancellation
- Queue is unbounded to prevent blocking if frontend stops consuming
- `frontend_connected` flag tracks state for logging purposes only
- Exception handling in consumer task ensures errors are logged but don't crash the task
- Consumer task completion logged differently based on `frontend_connected` state

**Backend Service Independence** (`message_service.py:stream_message_with_events`):
- Runs to completion regardless of frontend state
- Streams to agent env via `httpx.AsyncClient.stream()`
- Saves messages to database after consuming all events
- Tracked by `ActiveStreamingManager`
- All database operations use fresh sessions via `get_fresh_db_session()` callback

**Frontend Resilience** (`useMessageStream.ts`):
- On mount: checks `/streaming-status`
- If streaming: shows UI, polls for completion
- On disconnect: messages still saved in database
- On reconnect: refresh to show results

### Critical Considerations for Future Development

**When Modifying Streaming Code**:

1. **Never Chain Generators for Decoupling**: Don't rely on generator chains for independence. Python's async generator protocol couples them tightly via `.aclose()` calls.

2. **Queue-Based Architecture is Essential**: The `asyncio.Queue` pattern is not just an optimization - it's required for true decoupling. Any change that removes the queue will break decoupling.

3. **Background Task Must Start First**: The consumer task MUST be created before any `yield` to the frontend. If you yield first, the task becomes part of the generator chain.

4. **Never Cancel the Consumer Task**: The whole point is to let it run to completion. Don't add cleanup code that cancels the task on errors.

5. **Unbounded Queue is Intentional**: Bounded queues can block the consumer task if frontend stops reading. This defeats the purpose of decoupling.

6. **Database Operations in Service Layer**: All DB writes happen in `stream_message_with_events()` after consuming events. Don't move these to the API layer where they could be tied to frontend state.

7. **Testing Decoupling**: To verify decoupling works:
   - Send a message to a session
   - Close browser window within 2 seconds (before first response)
   - Wait 30-60 seconds for agent to complete
   - Check logs for "Stream consumer completed... frontend disconnected earlier"
   - Reopen session - agent response should be present
   - Verify full stream duration (15-45+ seconds, not 0.9-6 seconds)

8. **Error Handling in Consumer Task**: Errors in consumer task should be logged but not propagated. Put error events in queue if needed, but always signal completion with `None`.

9. **Fresh DB Sessions**: Always use `get_fresh_db_session()` callback for DB operations in the background task context. Don't reuse the request's DB session.

10. **ActiveStreamingManager Semantics**: The stream should remain registered in `ActiveStreamingManager` until `stream_message_with_events()` completes, regardless of frontend state. This allows `/streaming-status` endpoint to correctly report ongoing streams.

**Files Modified for Decoupling**:
- `backend/app/api/routes/messages.py` - Queue-based architecture in `send_message_stream` endpoint
- `backend/app/services/message_service.py` - Independent execution of `stream_message_with_events()`
- `backend/app/services/active_streaming_manager.py` - Stream lifecycle tracking independent of client connections

## Interruption Handling

### User Manual Interruption

**Frontend** (`useMessageStream.ts:stopMessage`):
1. User clicks stop button → shows loading spinner (`isInterruptPending = true`)
2. Sends `POST /messages/interrupt`
3. Keeps SSE connection open
4. Waits for "interrupted" or "done" event

**Backend** (`messages.py:interrupt_message`):
1. Calls `active_streaming_manager.request_interrupt(session_id)`
2. If external_session_id available: forwards immediately to agent env
3. If external_session_id not yet available: queues interrupt as pending

**Backend** (`message_service.py`):
1. Captures external_session_id from `session_created` event (early in stream, ~1 second)
2. Checks for pending interrupt via `active_streaming_manager.update_external_session_id()`
3. If pending: forwards interrupt to agent env immediately (`POST /chat/interrupt/{external_session_id}`)

**Agent Environment** (`sdk_manager.py:send_message_stream`):
1. Captures session_id from SystemMessage.data (dict key or attribute)
2. Registers session in `active_session_manager` immediately
3. Yields `session_created` event with session_id (within ~1 second)
4. Streaming loop checks `active_session_manager.check_interrupt_requested()`
5. If interrupted: calls `client.interrupt()` and continues loop
6. SDK performs cleanup and yields interrupt event (exit code -9 or ResultMessage with subtype "error_during_execution")
7. Formats interrupt UserMessages as "system" events

**Agent Environment** (`routes.py:interrupt_session`):
- Receives interrupt request
- Sets interrupt flag via `active_session_manager.request_interrupt()`

**Agent Environment** (`sdk_utils.py:format_sdk_message`):
- Detects interrupted ResultMessage (subtype "error_during_execution")
- Formats interrupt UserMessages as "system" events with "⚠️ Request interrupted by user"

**Backend** (`message_service.py`):
1. Receives "interrupted" event
2. Saves message with `status="user_interrupted"`
3. Sets session status to "active" (not "completed")
4. Yields "interrupted" event to frontend

**Frontend** (`useMessageStream.ts`, `MessageBubble.tsx`):
1. Receives "interrupted" event and/or "system" events
2. Clears loading spinner
3. Displays:
   - Yellow "Interrupted" badge on message
   - System notification showing "⚠️ Request interrupted by user"
4. Refreshes messages

### SDK Self-Interruption

Same flow as manual interruption, initiated by SDK instead of user (e.g., out of credits, API timeout, rate limit, internal error).

### Key Properties

**Proper Cleanup**:
- SDK `interrupt()` ensures session not corrupted
- Agent env continues receiving messages after interrupt for SDK cleanup
- Exit code -9 is expected after interrupt, not treated as error
- Session can be resumed in next message

**Data Integrity**:
- Partial message content saved with interrupt status
- Streaming events preserved in metadata
- Session state remains valid

**Race Condition Handling**:
- Interrupt can be clicked before external_session_id is available
- Backend queues interrupt in `ActiveStreamingManager` as "pending"
- When external_session_id arrives, pending interrupt is immediately forwarded
- Prevents 400 errors when interrupting newly-started messages

**Error Handling**:
- If `client.interrupt()` fails: yields error event and stops processing
- Backend disconnect detection (ConnectionResetError, BrokenPipeError): stops yielding, SDK continues in background
- Interrupted event only sent after SDK confirms

## Error Handling

### Session Corruption Detection

**Symptoms**: "No conversation found", "exit code -9" without interrupt, "Cannot write to terminated process"

**Agent Environment** (`sdk_manager.py`):
- Catches exceptions during `client.query()`
- Checks error message for corruption indicators
- Yields `{type: "error", session_corrupted: true}`

**Backend** (`message_service.py`):
- Receives corruption error
- Clears `external_session_id` via `SessionService.set_external_session_id(None)`
- Next message starts fresh session

**Frontend**:
- Displays error: "Session corrupted, please try again"
- User resends message
- New SDK session created

### Network Errors

**Frontend Disconnect**: Treated as `AbortError`, backend continues processing, frontend refreshes on reconnect

**Backend-to-Agent Env Errors** (`message_service.py:send_message_to_environment_stream`):
- Yields error event: `{type: "error", error_type: "ConnectionError"}`
- Saved as system message in chat
- Session status set to "idle"

## Reconnection & Recovery

### Page Refresh During Streaming

**Frontend** (`useMessageStream.ts:checkAndReconnectToActiveStream`):
1. Runs on mount
2. Fetches `GET /streaming-status`
3. If streaming:
   - Sets `isStreaming = true` (shows UI)
   - Refreshes messages (shows partial content)
   - Polls status every 1s
   - When done: refreshes messages, invalidates queries

**User Experience**: Sees spinning indicator, partial message content, automatically updates when complete, no data loss

### Backend Restart During Streaming

**Issue**: `ActiveStreamingManager` is in-memory, lost on restart

**Impact**:
- `/streaming-status` returns false
- Frontend won't show streaming UI
- Messages still in database

**Mitigation**: Session status in database ("active", "completed") can be used as fallback

## Database Schema

### Session Table

Fields:
- `status` - "idle", "active", "completed", "error"
- `last_message_at` - Updated on each message
- `external_session_mappings` - JSON: `{sdk_type: session_id}`
- `mode` - "building" or "conversation"
- `agent_sdk` - "claude" (future: "openai", etc.)

### SessionMessage Table

Fields:
- `role` - "user", "agent", "system"
- `content` - Message text (summary for agent responses)
- `sequence_number` - Auto-incremented per session
- `message_metadata` - JSON with streaming events, model, cost
- `tool_questions_status` - "unanswered", "answered" (AskUserQuestion)
- `status` - "", "user_interrupted", "error"
- `status_message` - Description of status

## Implementation Details

### Session ID Extraction

**Challenge**: SystemMessage.data is a dict, not an object

**Solution** (`sdk_manager.py`): Handles both dict keys and object attributes when extracting session_id from SystemMessage

**Data Structure**:
```json
{
  "data": {
    "session_id": "271d12ee-8f15-41be-a662-9152409cc4f2",
    "type": "system",
    "subtype": "init"
  }
}
```

### Interrupt Detection from SDK Messages

**Challenge**: SDK may send interrupt confirmation in different formats
- Exit code -9 exception (forceful termination)
- ResultMessage with `subtype: "error_during_execution"` (graceful completion)
- UserMessages with interrupt details (informational)

**Solution** (`sdk_utils.py:format_sdk_message`):
1. Accepts `interrupt_initiated` parameter
2. Detects interrupted ResultMessage (subtype "error_during_execution")
3. Formats interrupt UserMessages as "system" events with "⚠️ Request interrupted by user"

**Result**: Interrupted messages properly detected regardless of SDK termination method

### Interrupt Handling Flow

**Implementation** (`sdk_manager.py`):
- Calls `client.interrupt()` but continues loop (doesn't break immediately)
- SDK sends final messages for cleanup
- SDK raises exit code -9 when ready
- Exception handler yields interrupted event
- Ensures complete SDK cleanup

**Interrupt Failure** (`sdk_manager.py`):
- If `client.interrupt()` fails: yields error event and stops processing

**Backend Disconnect Detection** (`routes.py`):
- Catches `ConnectionResetError` and `BrokenPipeError` when yielding
- SDK continues processing even if backend disconnects
- Finally block ensures cleanup happens

## File Reference

### Frontend
- `hooks/useMessageStream.ts` - Main streaming logic, handles system events
- `components/Chat/MessageInput.tsx` - UI for send/stop
- `components/Chat/MessageBubble.tsx` - Displays interrupted badge and status
- `components/Chat/StreamEventRenderer.tsx` - Renders streaming events including system notifications
- `components/Chat/MessageList.tsx` - Message list container
- `components/Chat/StreamingMessage.tsx` - Real-time streaming display

### Backend
- `api/routes/messages.py` - API endpoints
- `api/routes/agents.py` - Agent handover endpoint (triggers background processing)
- `services/message_service.py` - Streaming orchestration
- `services/agent_service.py` - Agent handover execution with background message processing
- `services/active_streaming_manager.py` - Stream tracking
- `services/session_service.py` - Session/external ID management

### Agent Environment
- `env-templates/python-env-advanced/app/core/server/routes.py` - API endpoints, disconnect detection
- `env-templates/python-env-advanced/app/core/server/sdk_manager.py` - SDK streaming, interrupt handling
- `env-templates/python-env-advanced/app/core/server/sdk_utils.py` - SDK message formatting
- `env-templates/python-env-advanced/app/core/server/active_session_manager.py` - Interrupt tracking

## Future Enhancements

- **Persistent Stream Tracking**: Store active streams in Redis/database to survive backend restarts
- **Real-time Reconnection**: Reconnect to ongoing SSE stream using resume/offset mechanism
- **Progress Indicators**: Track message position in stream, show percentage complete
- **Multi-client Support**: Multiple frontend tabs see same stream via WebSocket broadcast
