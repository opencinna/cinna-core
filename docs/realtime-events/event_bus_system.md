# Real-Time Event Bus System

## Overview

The Event Bus system provides real-time, bidirectional communication between the backend and frontend using WebSockets (Socket.IO). This allows different parts of the application to react immediately to changes without polling.

## Architecture

```
┌─────────────────┐         WebSocket          ┌──────────────────┐
│   Frontend      │ ◄────────────────────────► │    Backend       │
│                 │    (Socket.IO/ws://...)     │                  │
│  - Components   │                             │  - EventService  │
│  - Hooks        │                             │  - Event Routes  │
│  - eventService │                             │  - Event Models  │
└─────────────────┘                             └──────────────────┘
```

## Features

- **Event-Driven Architecture**: Components subscribe to specific event types
- **User-Specific Rooms**: Events can be broadcast to specific users
- **Custom Rooms**: Support for topic-based subscriptions (e.g., `session_123`)
- **Auto-Reconnection**: Automatic reconnection with exponential backoff
- **Type-Safe Events**: Predefined event types with metadata support
- **React Integration**: Easy-to-use React hooks for component subscriptions

## Event Structure

Each event has the following structure:

```typescript
interface EventData {
  type: string              // Event type (e.g., 'session_updated')
  model_id?: string         // ID of the related model (session_id, message_id, etc.)
  text_content?: string     // Optional notification text for the user
  meta?: Record<string, any> // Additional metadata
  user_id?: string          // User ID for targeted events
  timestamp: string         // When the event was created
}
```

## Available Event Types

### Session Events
- `session_created` - New session created
- `session_updated` - Session updated
- `session_deleted` - Session deleted

### Message Events
- `message_created` - New message created
- `message_updated` - Message updated
- `message_deleted` - Message deleted

### Activity Events
- `activity_created` - New activity created
- `activity_updated` - Activity updated
- `activity_deleted` - Activity deleted

### Agent Events
- `agent_created` - New agent created
- `agent_updated` - Agent updated
- `agent_deleted` - Agent deleted

### Streaming Events
- `stream_started` - Stream started
- `stream_completed` - Stream completed
- `stream_error` - Stream error occurred

### Generic Events
- `notification` - Generic notification

## Backend Usage

### 1. Emitting Events from Backend Code

```python
from app.services.event_service import event_service
from app.models.event import EventType
from uuid import UUID

# Example: Emit a session_updated event
async def update_session(session_id: UUID, user_id: UUID):
    # ... update session logic ...

    # Emit event to the specific user
    await event_service.emit_event(
        event_type=EventType.SESSION_UPDATED,
        model_id=session_id,
        text_content="Your session has been updated",
        meta={
            "session_id": str(session_id),
            "updated_fields": ["title", "status"]
        },
        user_id=user_id
    )
```

### 2. Broadcasting Events via API Endpoint

```python
# POST /api/v1/events/broadcast
{
  "type": "session_updated",
  "model_id": "session-uuid-here",
  "text_content": "Session updated successfully",
  "meta": {
    "session_id": "session-uuid-here",
    "agent_id": "agent-uuid-here"
  },
  "user_id": "user-uuid-here"  # Optional: target specific user
}
```

### 3. Getting Connection Stats

```python
# GET /api/v1/events/stats
# Returns:
{
  "connection_count": 5,
  "connected_users": ["user-id-1", "user-id-2"],
  "is_current_user_connected": true
}
```

### 4. Testing WebSocket Connection

```python
# POST /api/v1/events/test
# Sends a test event to the current user
```

## Frontend Usage

### 1. Initialize Event Bus Connection

Add the event bus connection to your root layout or app component:

```tsx
// In your root layout (e.g., __root.tsx or main App component)
import { useEventBusConnection } from "@/hooks/useEventBus"

function RootLayout() {
  // This automatically connects when user is authenticated
  const { isConnected, socketId } = useEventBusConnection()

  console.log("WebSocket connected:", isConnected, "Socket ID:", socketId)

  return (
    <div>
      {/* Your app content */}
    </div>
  )
}
```

### 2. Subscribe to Specific Event Types

```tsx
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { useQueryClient } from "@tanstack/react-query"

function LatestSessions() {
  const queryClient = useQueryClient()

  // Subscribe to session_updated events
  useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
    console.log("Session updated:", event.model_id, event.meta)

    // Refresh the sessions list
    queryClient.invalidateQueries({ queryKey: ["sessions"] })

    // Optionally show a toast notification
    if (event.text_content) {
      toast.info(event.text_content)
    }
  })

  return <div>{/* Your component */}</div>
}
```

### 3. Subscribe to Multiple Event Types

```tsx
import { useMultiEventSubscription } from "@/hooks/useEventBus"

function SessionsDashboard() {
  const queryClient = useQueryClient()

  // Subscribe to multiple session events
  useMultiEventSubscription(
    ['session_created', 'session_updated', 'session_deleted'],
    (event) => {
      console.log("Session event:", event.type, event.model_id)
      queryClient.invalidateQueries({ queryKey: ["sessions"] })
    }
  )

  return <div>{/* Your component */}</div>
}
```

### 4. Subscribe to All Events

```tsx
import { useEventSubscription } from "@/hooks/useEventBus"

function GlobalEventLogger() {
  // Subscribe to all events (use "*" as event type)
  useEventSubscription("*", (event) => {
    console.log("Event received:", event.type, event)
  })

  return null // This component just logs events
}
```

### 5. Conditional Subscriptions

```tsx
function SessionDetail({ sessionId }: { sessionId: string }) {
  const [autoUpdate, setAutoUpdate] = useState(true)

  // Only subscribe when autoUpdate is enabled
  useEventSubscription(
    EventTypes.SESSION_UPDATED,
    (event) => {
      if (event.model_id === sessionId) {
        console.log("This session was updated!")
        // Refresh session data
      }
    },
    autoUpdate // enabled parameter
  )

  return (
    <div>
      <button onClick={() => setAutoUpdate(!autoUpdate)}>
        {autoUpdate ? "Disable" : "Enable"} Auto-Update
      </button>
    </div>
  )
}
```

### 6. Room-Based Subscriptions

```tsx
import { useRoomSubscription, useEventSubscription } from "@/hooks/useEventBus"

function SessionChat({ sessionId }: { sessionId: string }) {
  // Subscribe to a specific session room
  useRoomSubscription(`session_${sessionId}`)

  // Listen for message events in this session
  useEventSubscription(EventTypes.MESSAGE_CREATED, (event) => {
    if (event.meta?.session_id === sessionId) {
      console.log("New message in this session!")
      // Update messages list
    }
  })

  return <div>{/* Chat UI */}</div>
}
```

## Integration Example: LatestSessions Component

Here's a complete example of how the `LatestSessions` component could use the event bus:

```tsx
// frontend/src/components/Sessions/LatestSessions.tsx
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { useQueryClient } from "@tanstack/react-query"
import { useCustomToast } from "@/hooks/useCustomToast"

export function LatestSessions() {
  const queryClient = useQueryClient()
  const { showInfoToast } = useCustomToast()

  // Subscribe to session events
  useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
    console.log("Session updated:", event.model_id)

    // Invalidate sessions query to trigger refetch
    queryClient.invalidateQueries({ queryKey: ["sessions"] })

    // Show notification if there's text content
    if (event.text_content) {
      showInfoToast(event.text_content)
    }
  })

  useEventSubscription(EventTypes.SESSION_CREATED, (event) => {
    console.log("New session created:", event.model_id)
    queryClient.invalidateQueries({ queryKey: ["sessions"] })
  })

  useEventSubscription(EventTypes.SESSION_DELETED, (event) => {
    console.log("Session deleted:", event.model_id)
    queryClient.invalidateQueries({ queryKey: ["sessions"] })
  })

  // ... rest of component
}
```

## Backend Integration Example: Emitting Events

Here's how to integrate event emission into existing backend services:

```python
# In backend/app/services/session_service.py

from app.services.event_service import event_service
from app.models.event import EventType

class SessionService:
    @staticmethod
    async def update_session(
        db_session: Session,
        session_id: UUID,
        user_id: UUID,
        update_data: SessionUpdate
    ) -> Session:
        # Update the session
        session = db_session.get(Session, session_id)
        # ... update logic ...
        db_session.commit()

        # Emit event to notify connected clients
        await event_service.emit_event(
            event_type=EventType.SESSION_UPDATED,
            model_id=session_id,
            text_content=f"Session '{session.title}' has been updated",
            meta={
                "session_id": str(session_id),
                "title": session.title,
                "updated_at": session.updated_at.isoformat()
            },
            user_id=user_id
        )

        return session
```

## Connection Management

### Auto-Connection
The `useEventBusConnection` hook automatically:
- Connects when a user is authenticated
- Joins a user-specific room (`user_{user_id}`)
- Handles reconnection with exponential backoff
- Disconnects on unmount

### Manual Connection Control
If you need manual control:

```typescript
import { eventService } from "@/services/eventService"

// Connect manually
eventService.connect(userId)

// Check connection status
const isConnected = eventService.isConnected()

// Get socket ID
const socketId = eventService.getSocketId()

// Disconnect
eventService.disconnect()

// Ping server
eventService.ping()
```

## Best Practices

### 1. Use Specific Event Types
```tsx
// ✅ Good - specific event type
useEventSubscription(EventTypes.SESSION_UPDATED, handler)

// ❌ Avoid - subscribing to all events unless necessary
useEventSubscription("*", handler)
```

### 2. Invalidate Queries, Don't Directly Update Cache
```tsx
// ✅ Good - let React Query refetch
useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
  queryClient.invalidateQueries({ queryKey: ["sessions"] })
})

// ❌ Avoid - manually updating cache can lead to inconsistencies
useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
  queryClient.setQueryData(["sessions"], (old) => {
    // Manual update logic...
  })
})
```

### 3. Filter Events in Handler
```tsx
useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
  // Only handle events for the current session
  if (event.model_id === currentSessionId) {
    // Update UI
  }
})
```

### 4. Provide Meaningful Metadata
```python
# Backend - include useful context in meta
await event_service.emit_event(
    event_type=EventType.MESSAGE_CREATED,
    model_id=message_id,
    meta={
        "session_id": str(session_id),
        "agent_id": str(agent_id),
        "message_role": "agent",
        "has_questions": True
    },
    user_id=user_id
)
```

## Troubleshooting

### WebSocket Connection Issues

1. **Check backend logs** for connection attempts:
```bash
docker-compose logs -f backend | grep EventService
```

2. **Check frontend console** for connection status:
```javascript
console.log("Connected:", eventService.isConnected())
console.log("Socket ID:", eventService.getSocketId())
```

3. **Test connection** via API:
```bash
# Send test event
curl -X POST http://localhost:8000/api/v1/events/test \
  -H "Authorization: Bearer YOUR_TOKEN"

# Check connection stats
curl http://localhost:8000/api/v1/events/stats \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Events Not Received

1. **Verify subscription**:
```tsx
useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
  console.log("Event received:", event)
})
```

2. **Check event type matches** between backend and frontend

3. **Verify user_id** is set correctly when emitting targeted events

4. **Check browser network tab** for WebSocket connection (ws:// protocol)

## Performance Considerations

1. **Use Targeted Events**: Send events to specific users instead of broadcasting when possible
2. **Debounce Rapid Events**: If events fire rapidly, consider debouncing the handler
3. **Unsubscribe When Not Needed**: Use the `enabled` parameter to disable subscriptions
4. **Use Rooms**: For topic-based filtering, use rooms instead of filtering in handlers

## Security

- WebSocket connections require authentication (user_id in auth data)
- Users can only broadcast to themselves (unless superuser)
- Connection attempts without user_id are rejected
- Each user automatically joins their own room (`user_{user_id}`)

## File Reference

### Backend
- Models: `backend/app/models/event.py`
- Service: `backend/app/services/event_service.py`
- Routes: `backend/app/api/routes/events.py`
- Main app: `backend/app/main.py` (Socket.IO mount)

### Frontend
- Service: `frontend/src/services/eventService.ts`
- Hooks: `frontend/src/hooks/useEventBus.ts`

## Future Enhancements

1. **Event History**: Store recent events for late-joining clients
2. **Event Acknowledgment**: Confirm event receipt from clients
3. **Binary Events**: Support for binary data (file uploads, images)
4. **Event Filtering**: Server-side event filtering by criteria
5. **Rate Limiting**: Prevent event spam from misbehaving clients
6. **Persistent Subscriptions**: Remember subscriptions across reconnections
