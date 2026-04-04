# Real-Time Event Bus System

## Overview

Event bus system for real-time communication with **two channels**:
1. **WebSocket (Socket.IO)**: Backend → Frontend real-time updates
2. **Backend Event Handlers**: Backend → Backend event-driven processing

Events are emitted from backend services, triggering both WebSocket broadcasts to frontend clients and backend handler functions for server-side processing.

## Architecture

```
Frontend Components                    Backend Services
       |                                      |
   useEventBus hooks                    EventService
       |                                 /          \
   eventService.ts      ←WebSocket→  Socket.IO   Backend Handlers
       |                               Server      (registered functions)
   socket.io-client                      |              |
                                     /ws endpoint   EnvironmentService
                                                    SessionService
                                                    etc.
```

**WebSocket URL**: `ws://localhost:8000/ws` (dev) | `wss://api.domain.com/ws` (prod)

**Event Flow**:
1. Service emits event via `event_service.emit_event()`
2. Event is sent to WebSocket clients (frontend)
3. Event is also dispatched to registered backend handlers (server-side processing)

## Event Structure

```typescript
{
  type: string              // Event type (e.g., 'session_updated')
  model_id?: string         // Related model UUID
  text_content?: string     // Optional user notification
  meta?: Record<string, any> // Additional metadata
  user_id?: string          // Target user UUID
  timestamp: string         // ISO timestamp
}
```

## Event Types

**Sessions**: `session_created`, `session_updated`, `session_deleted`
**Messages**: `message_created`, `message_updated`, `message_deleted`
**Activities**: `activity_created`, `activity_updated`, `activity_deleted`
**Agents**: `agent_created`, `agent_updated`, `agent_deleted`
**Environments**: `environment_activating`, `environment_activated`, `environment_activation_failed`, `environment_suspended`
**Streaming**: `stream_started`, `stream_completed`, `stream_error`, `stream_interrupted`, `session_interaction_status_changed`
**Todo Progress**: `todo_list_updated`, `task_todo_updated`
**Generic**: `notification`

### Session Interaction Status Changed

**`SESSION_INTERACTION_STATUS_CHANGED`** is emitted when a session's `interaction_status` changes (streaming starts/ends):

**Emitted from** (`session_service.py`):
- `handle_stream_started()` - when streaming begins (`interaction_status: "running"`)
- `handle_stream_completed()` - when streaming ends normally (`interaction_status: ""`)
- `handle_stream_error()` - when streaming fails (`interaction_status: ""`)
- `handle_stream_interrupted()` - when user interrupts (`interaction_status: ""`)

**Meta payload**:
```json
{
  "session_id": "uuid-string",
  "interaction_status": "running" | "",
  "streaming_started_at": "2026-01-23T10:30:00.000Z"  // only when "running"
}
```

**Dual room emission**: Each handler emits this event to two destinations:
1. `user_{owner_id}` room — for authenticated owner sessions in the regular app UI
2. `session_{session_id}_stream` room — for webapp chat viewers who connect using their `webapp_share_id` as their Socket.IO user identifier and are therefore never in the owner's user room

This dual emission is required because webapp viewers cannot receive events from the owner's user room. The session stream room is the only shared channel between the owner's streaming pipeline and the webapp viewer's Socket.IO connection.

**Frontend usage**: The session page subscribes to this event to immediately invalidate the session query, providing near-instant detection of streaming state changes without waiting for the next poll interval. This enables derived streaming state (`isStreaming = session.interaction_status === "running"`) to update reactively. The webapp chat widget also subscribes, using the event to show/hide the streaming indicator.

### Environment Activation Events

**`ENVIRONMENT_ACTIVATING`** is emitted when an environment begins transitioning to "running":
- **`activate_suspended_environment()`**: When reactivating a suspended environment
- **`start_environment()`**: When starting a stopped environment

This event allows the frontend to show "Activating" loading state in the UI (e.g., "App" icon in session header).

**`ENVIRONMENT_ACTIVATED`** is emitted whenever an environment transitions to "running" status:
- **`activate_suspended_environment()`**: When a suspended environment is reactivated
- **`start_environment()`**: When an environment starts for the first time or restarts
- **`rebuild_environment()`**: When an environment is rebuilt and was previously running

This ensures any sessions waiting for the environment (with `pending_stream` status) are processed, regardless of how the environment became active. Critical for:
- Handovers that target an agent while its environment is building/starting
- Messages sent while environment is activating or stopped
- Multiple concurrent requests waiting for the same environment
- Clone environments that auto-start after creation

## Key Files

### Backend
- **`backend/app/models/events/event.py`** - Event models (`EventType`, `EventBase`, `EventPublic`, `EventBroadcast`)
- **`backend/app/services/events/event_service.py`** - `EventService` class (connection management, event emission)
- **`backend/app/api/routes/events.py`** - Event API routes (`/broadcast`, `/stats`, `/test`)
- **`backend/app/main.py`** - Socket.IO mount at `/ws` path

### Frontend
- **`frontend/src/services/eventService.ts`** - Socket.IO client wrapper
- **`frontend/src/hooks/useEventBus.ts`** - React hooks for subscriptions

## Usage

### Backend: Emit Events

```python
# backend/app/api/routes/sessions.py
from app.services.event_service import event_service
from app.models.event import EventType

await event_service.emit_event(
    event_type=EventType.SESSION_UPDATED,
    model_id=session_id,
    user_id=current_user.id,
    meta={"field": "value"}  # optional
)
```

**Key Methods** (`EventService` in `event_service.py`):
- `emit_event()` - Broadcast to user room + call backend handlers
- `broadcast_to_room()` - Broadcast to custom room
- `register_handler()` - Register backend event handler
- `get_connection_stats()` - Get active connections

### Backend: Register Event Handlers

Backend services can react to events without using WebSockets. This enables **event-driven architecture** between backend services.

**1. Create Handler Function**:
```python
# backend/app/services/environments/environment_service.py
@staticmethod
async def handle_stream_completed_event(event_data: dict[str, Any]):
    """
    React to stream completion events.

    Args:
        event_data: Full event dict with type, model_id, meta, user_id, timestamp
    """
    meta = event_data.get("meta", {})
    session_mode = meta.get("session_mode")
    environment_id = meta.get("environment_id")

    if session_mode == "building":
        # Perform post-processing (e.g., sync agent prompts)
        with Session(engine) as session:
            environment = session.get(AgentEnvironment, UUID(environment_id))
            # ... business logic
```

**2. Register Handler on Startup**:
```python
# backend/app/main.py
from app.services.event_service import event_service
from app.models.event import EventType
from app.services.environment_service import EnvironmentService

@app.on_event("startup")
def on_startup():
    # Register backend event handlers
    event_service.register_handler(
        event_type=EventType.STREAM_COMPLETED,
        handler=EnvironmentService.handle_stream_completed_event
    )
```

**Handler Best Practices**:
- Handlers run as background tasks (non-blocking)
- Use fresh database sessions (avoid session conflicts)
- Handle errors gracefully (exceptions are logged, not propagated)
- Keep handlers fast (offload heavy work to background tasks if needed)
- Handlers receive full event data: `{type, model_id, meta, user_id, timestamp}`
- **CRITICAL**: See "Background Task Patterns in Event Handlers" section below for async task management

**Example Use Case**: Streaming Lifecycle Events

When chat streaming occurs, `MessageService` emits events at each stage:
- `STREAM_STARTED` → `ActivityService` creates "session_running" activity
- `STREAM_COMPLETED` → `ActivityService` manages completion, `EnvironmentService` syncs prompts (building mode)
- `STREAM_ERROR` → `ActivityService` creates error activity
- `STREAM_INTERRUPTED` → `ActivityService` cleans up running activity

**Example Use Case**: Todo Progress Tracking

When an agent uses the TodoWrite tool during message processing:
- `MessageService` detects the tool call and updates `Session.todo_progress`
- `TODO_LIST_UPDATED` event is emitted with session_id and todos array
- `InputTaskService.handle_todo_list_updated()` receives the event:
  - If session is linked to a task (via `source_task_id`), saves todos to `InputTask.todo_progress`
  - Emits `TASK_TODO_UPDATED` event for frontend real-time updates
- Frontend subscribes to `TASK_TODO_UPDATED` to update the Tasks list UI in real-time

```python
# Message service emits event after stream finishes
await event_service.emit_event(
    event_type=EventType.STREAM_COMPLETED,
    model_id=session_id,
    meta={
        "session_id": str(session_id),
        "environment_id": str(environment_id),
        "agent_id": str(agent_id),
        "session_mode": session_mode,  # "building" or "conversation"
        "was_interrupted": False
    },
    user_id=user_id
)

# Environment service handler automatically processes this event
# No direct coupling between MessageService and EnvironmentService
```

### Frontend: Subscribe to Events

**1. Initialize Connection** (in root layout):
```tsx
// frontend/src/routes/__root.tsx
import { useEventBusConnection } from "@/hooks/useEventBus"

function RootComponent() {
  useEventBusConnection() // Auto-connects when authenticated
  return <Outlet />
}
```

**2. Subscribe in Components**:
```tsx
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"

function LatestSessions() {
  const queryClient = useQueryClient()

  useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
    queryClient.invalidateQueries({ queryKey: ["sessions"] })
  })

  // ... component code
}
```

**3. Subscribe to Todo Progress Updates** (example from Tasks list):
```tsx
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"

function TasksList() {
  const [taskTodos, setTaskTodos] = useState<Record<string, TodoItem[]>>({})

  useEventSubscription(EventTypes.TASK_TODO_UPDATED, (event) => {
    const { task_id, todos } = event.meta || {}
    if (task_id && todos) {
      setTaskTodos((prev) => ({ ...prev, [task_id]: todos }))
    }
  })

  // Render task cards with TaskTodoProgress component
}
```

**Key Hooks** (`useEventBus.ts`):
- `useEventBusConnection()` - Manages connection lifecycle
- `useEventSubscription(type, handler)` - Subscribe to single event type
- `useMultiEventSubscription(types, handler)` - Subscribe to multiple types
- `useRoomSubscription(room)` - Subscribe to custom room

## API Endpoints

```
POST /api/v1/events/broadcast - Broadcast event (admin or to self)
GET  /api/v1/events/stats     - Connection statistics
POST /api/v1/events/test      - Send test event to current user
```

## Production Setup

### Environment Variables

**Backend (`.env`)**:
```bash
DOMAIN=project.com
FRONTEND_HOST=https://project.com
BACKEND_CORS_ORIGINS="https://project.com,https://api.project.com"
```

**Frontend (build args in `docker-compose.yml`)**:
```yaml
args:
  - VITE_API_URL=https://api.${DOMAIN}
```

### Docker Configuration

WebSocket uses **same port** as HTTP API (8000). Socket.IO upgrades HTTP → WebSocket.

```yaml
# docker-compose.yml
backend:
  ports:
    - "8000:8000"  # API + WebSocket
frontend:
  ports:
    - "80:80"      # Static files
```

### Reverse Proxy (Nginx)

```nginx
server {
    listen 443 ssl;
    server_name api.project.com;

    location / {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;  # 24h keepalive
    }
}
```

**Production URLs**:
- Frontend: `https://project.com`
- Backend API: `https://api.project.com`
- WebSocket: `wss://api.project.com/ws` (auto-upgrade from HTTPS)

## Testing

```bash
# Send test event
curl -X POST http://localhost:8000/api/v1/events/test \
  -H "Authorization: Bearer TOKEN"

# Check connection stats
curl http://localhost:8000/api/v1/events/stats \
  -H "Authorization: Bearer TOKEN"
```

## Dependencies

**Backend**: `python-socketio==5.16.0`, `python-engineio==4.13.0`
**Frontend**: `socket.io-client`

## Best Practices

### Frontend
1. **Invalidate queries, don't manually update cache**:
   ```tsx
   queryClient.invalidateQueries({ queryKey: ["sessions"] })
   ```

2. **Use specific event types** (avoid wildcard `"*"` subscriptions)

3. **Filter events in handlers when needed**:
   ```tsx
   if (event.model_id === currentSessionId) { /* handle */ }
   ```

### Backend
1. **Use event-driven architecture for service integration**:
   - Prefer backend event handlers over direct service calls
   - Reduces coupling between services
   - Example: Instead of `MessageService` directly calling `EnvironmentService`, emit `STREAM_COMPLETED` event

2. **Include rich metadata in events**:
   ```python
   meta={
       "session_id": str(session_id),
       "agent_id": str(agent_id),
       "session_mode": "building",
       "environment_id": str(environment_id)
   }
   ```

3. **Handle errors gracefully in event handlers**:
   ```python
   try:
       # Handler logic
   except Exception as e:
       logger.error(f"Error in handler: {e}", exc_info=True)
       # Don't raise - handlers should not crash the event loop
   ```

4. **Use fresh database sessions in handlers**:
   ```python
   # Avoid using the request's db session
   with Session(engine) as session:
       # Query and update data
   ```

## Troubleshooting

**Connection Issues**:
- Check browser console: `[EventService] Connected, socket ID: ...`
- Backend logs: `docker-compose logs -f backend | grep EventService`
- Verify CORS: `BACKEND_CORS_ORIGINS` includes frontend URL

**Events Not Received**:
- Event type matches between backend/frontend
- `user_id` set correctly when emitting
- Browser Network tab shows WebSocket connection (ws:// protocol)

## Background Task Patterns in Event Handlers

### Critical Async Patterns to Avoid Task Cancellation

Based on production debugging of streaming cancellation issues, event handlers must follow these patterns when creating background tasks.

#### 1. NEVER Use `asyncio.run()` in Event Handlers

**Problem**: Event handlers run in FastAPI's main event loop. Using `asyncio.run()` creates a temporary event loop that destroys all child tasks when it completes.

**Bad Example**:
```python
async def handle_environment_activated(event_data: dict):
    session_ids = get_sessions_for_environment(env_id)

    for session_id in session_ids:
        # ❌ WRONG - creates temporary event loop!
        asyncio.run(process_pending_messages(session_id))
```

**Correct Pattern**:
```python
async def handle_environment_activated(event_data: dict):
    session_ids = get_sessions_for_environment(env_id)

    for session_id in session_ids:
        # ✅ CORRECT - uses current event loop
        _create_task_with_error_logging(
            process_pending_messages(session_id),
            task_name=f"process_pending_{session_id}"
        )
```

#### 2. Don't Await Functions That Create Background Tasks

**Problem**: Awaiting a function that spawns background tasks ties those tasks to the parent's lifecycle. When the parent completes, child tasks get cancelled.

**Bad Example**:
```python
async def handle_environment_activated(event_data: dict):
    for session_id in session_ids:
        # initiate_stream() creates background tasks
        await initiate_stream(session_id)  # ❌ WRONG - child tasks cancelled!
```

**Correct Pattern**:
```python
async def handle_environment_activated(event_data: dict):
    for session_id in session_ids:
        # Wrap in create_task to make it independent
        _create_task_with_error_logging(
            SessionService.initiate_stream(session_id),
            task_name=f"initiate_stream_{session_id}"
        )  # ✅ CORRECT - task is independent
```

#### 3. Use `create_task_with_error_logging()` Helper

**Why**: Background tasks fail silently by default. This helper logs all exceptions and cancellations.
Implementation is here `backend/app/utils.py`.

**Usage in Event Handlers**:
```python
@staticmethod
async def handle_stream_completed(event_data: dict):
    environment_id = event_data["meta"]["environment_id"]

    # Create background task with error logging
    create_task_with_error_logging(
        sync_agent_prompts(environment_id),
        task_name=f"sync_prompts_{environment_id}"
    )
```

#### 4. Avoid Detached SQLAlchemy Objects in Background Tasks

**Problem**: ORM objects become detached when their session closes. Background tasks must fetch fresh objects with their own sessions.

**Bad Example**:
```python
async def handle_environment_activated(event_data: dict):
    with Session(engine) as session:
        environment = session.get(AgentEnvironment, env_id)
        agent = session.get(Agent, agent_id)

        # ❌ WRONG - objects are detached after session closes!
        _create_task_with_error_logging(
            process_environment(environment, agent),
            task_name="process_env"
        )
```

**Correct Pattern**:
```python
async def handle_environment_activated(event_data: dict):
    # Extract IDs only
    environment_id = event_data["meta"]["environment_id"]
    agent_id = event_data["meta"]["agent_id"]

    # Background task fetches fresh objects
    async def _process_with_fresh_session():
        with Session(engine) as fresh_session:
            environment = fresh_session.get(AgentEnvironment, environment_id)
            agent = fresh_session.get(Agent, agent_id)

            # Process with properly attached objects
            await process_environment(environment, agent, fresh_session)

    _create_task_with_error_logging(
        _process_with_fresh_session(),
        task_name=f"process_env_{environment_id}"
    )  # ✅ CORRECT
```

### Real-World Example: Environment Activation Handler

**Before (Broken)**:
```python
@staticmethod
async def handle_environment_activated(event_data: dict[str, Any]):
    """Handler with task cancellation issues"""
    with Session(engine) as session:
        session_ids = get_pending_session_ids(session, environment_id)

    for session_id in session_ids:
        # Bug: awaiting creates child tasks that get cancelled
        await SessionService.initiate_stream(
            session_id=session_id,
            get_fresh_db_session=lambda: DBSession(engine)
        )
```

**After (Fixed)**:
```python
@staticmethod
async def handle_environment_activated(event_data: dict[str, Any]):
    """Handler with proper task management"""
    with Session(engine) as session:
        session_ids = get_pending_session_ids(session, environment_id)

    for session_id in session_ids:
        # Fixed: wrap in create_task to make independent
        _create_task_with_error_logging(
            SessionService.initiate_stream(
                session_id=session_id,
                get_fresh_db_session=lambda: DBSession(engine)
            ),
            task_name=f"initiate_stream_{session_id}"
        )
```

### Debugging Background Task Issues

When event handlers fail silently:

1. **Check for task cancellation**:
   ```bash
   docker compose logs backend | grep "was cancelled"
   ```

2. **Check for unhandled exceptions**:
   ```bash
   docker compose logs backend | grep "Unhandled exception"
   ```

3. **Verify no `asyncio.run()` in handlers**:
   ```bash
   grep -r "asyncio.run" backend/app/services/
   ```

4. **Test event handler isolation**:
   - Add logging at handler entry: `logger.info(f"Handler started: {event_data}")`
   - Add logging before task creation: `logger.info(f"Creating task: {task_name}")`
   - Check if handler completes but tasks never run

### Implementation Files

Event handlers using these patterns:
- `backend/app/services/sessions/session_service.py` - `handle_environment_activated()`
- `backend/app/services/events/activity_service.py` - `handle_stream_started()`, `handle_stream_completed()`
- `backend/app/services/environments/environment_service.py` - `handle_stream_completed_event()`
- `backend/app/services/tasks/input_task_service.py` - `handle_stream_started()`, `handle_stream_completed()`, `handle_stream_error()`, `handle_todo_list_updated()`
- `backend/app/services/events/event_service.py` - `agent_usage_intent` WebSocket handler

## Extension Points

To extend this system:

1. **Add new event types**:
   - Update `EventType` enum in `backend/app/models/events/event.py`
   - Document the event's metadata structure

2. **Add backend event handlers**:
   - Create handler function: `async def handle_event(event_data: dict)`
   - Register in `main.py` startup: `event_service.register_handler(event_type, handler)`
   - **IMPORTANT**: Follow background task patterns above to avoid task cancellation

3. **Custom WebSocket rooms**:
   - Use `event_service.broadcast_to_room(room_name, event_data)`
   - Frontend subscribes: `useRoomSubscription(room_name)`

4. **Multi-instance scaling** (potential future implementation):
   - Configure Redis adapter for Socket.IO
   - Backend handlers already work across instances (each instance processes events independently)
   ```python
   from socketio import AsyncRedisManager
   redis_manager = AsyncRedisManager('redis://redis:6379')
   sio = socketio.AsyncServer(client_manager=redis_manager)
   ```

## Security

- Connections require authentication (user_id in auth data)
- Users can only broadcast to themselves (unless superuser)
- Each user auto-joins room `user_{user_id}`
- CORS restricted to configured origins
