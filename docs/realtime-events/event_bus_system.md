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
**Streaming**: `stream_started`, `stream_completed`, `stream_error`
**Generic**: `notification`

## Key Files

### Backend
- **`backend/app/models/event.py`** - Event models (`EventType`, `EventBase`, `EventPublic`, `EventBroadcast`)
- **`backend/app/services/event_service.py`** - `EventService` class (connection management, event emission)
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
# backend/app/services/environment_service.py
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

**Example Use Case**: `stream_completed` Event

When a chat stream completes, `MessageService` emits `STREAM_COMPLETED` event. `EnvironmentService` listens for this event and automatically syncs agent prompts from the environment back to the agent model (for "building" mode sessions).

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

## Extension Points

To extend this system:

1. **Add new event types**:
   - Update `EventType` enum in `backend/app/models/event.py`
   - Document the event's metadata structure

2. **Add backend event handlers**:
   - Create handler function: `async def handle_event(event_data: dict)`
   - Register in `main.py` startup: `event_service.register_handler(event_type, handler)`

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
