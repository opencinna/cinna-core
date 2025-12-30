# Real-Time Events System - Quick Start Guide

## What is this?

The Real-Time Events System provides WebSocket-based communication between your backend and frontend, allowing components to react immediately to changes without polling.

**Example use case**: When a session is updated in the database, all connected users viewing that session receive an instant notification and their UI updates automatically.

## Quick Start

### 1. Initialize the Event Bus (Frontend)

Add the event bus connection to your root layout or main App component:

```tsx
// In frontend/src/routes/__root.tsx or main app component
import { useEventBusConnection } from "@/hooks/useEventBus"

function RootComponent() {
  // This automatically connects when user is authenticated
  useEventBusConnection()

  return <div>{/* Your app */}</div>
}
```

### 2. Subscribe to Events in Your Component (Frontend)

```tsx
// In frontend/src/components/Sessions/LatestSessions.tsx
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { useQueryClient } from "@tanstack/react-query"

export function LatestSessions() {
  const queryClient = useQueryClient()

  // Subscribe to session_updated events
  useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
    console.log("Session updated:", event.model_id)
    // Refresh the sessions list
    queryClient.invalidateQueries({ queryKey: ["sessions"] })
  })

  // ... rest of component
}
```

### 3. Emit Events from Backend (Backend)

```python
# In backend/app/api/routes/sessions.py
from app.services.event_service import event_service
from app.models.event import EventType

@router.put("/{session_id}", response_model=SessionPublic)
async def update_session(
    session_id: uuid.UUID,
    session_in: SessionUpdate,
    current_user: CurrentUser
):
    # Update session logic...

    # Emit event to notify connected clients
    await event_service.emit_event(
        event_type=EventType.SESSION_UPDATED,
        model_id=session_id,
        text_content="Session updated successfully",
        user_id=current_user.id
    )

    return updated_session
```

## That's it!

Your component will now automatically update when sessions are modified, without any polling or manual refresh.

## Available Event Types

```typescript
// Session events
EventTypes.SESSION_CREATED
EventTypes.SESSION_UPDATED
EventTypes.SESSION_DELETED

// Message events
EventTypes.MESSAGE_CREATED
EventTypes.MESSAGE_UPDATED
EventTypes.MESSAGE_DELETED

// Activity events
EventTypes.ACTIVITY_CREATED
EventTypes.ACTIVITY_UPDATED
EventTypes.ACTIVITY_DELETED

// Agent events
EventTypes.AGENT_CREATED
EventTypes.AGENT_UPDATED
EventTypes.AGENT_DELETED

// Streaming events
EventTypes.STREAM_STARTED
EventTypes.STREAM_COMPLETED
EventTypes.STREAM_ERROR

// Generic notification
EventTypes.NOTIFICATION
```

## Common Patterns

### 1. Invalidate Queries on Event

```tsx
useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
  queryClient.invalidateQueries({ queryKey: ["sessions"] })
})
```

### 2. Show Toast Notification

```tsx
useEventSubscription(EventTypes.SESSION_CREATED, (event) => {
  if (event.text_content) {
    toast.success(event.text_content)
  }
})
```

### 3. Update Specific Item

```tsx
useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
  if (event.model_id === currentSessionId) {
    queryClient.invalidateQueries({
      queryKey: ["sessions", currentSessionId]
    })
  }
})
```

### 4. Subscribe to Multiple Events

```tsx
useMultiEventSubscription(
  ['session_created', 'session_updated', 'session_deleted'],
  (event) => {
    queryClient.invalidateQueries({ queryKey: ["sessions"] })
  }
)
```

## Testing

### Test WebSocket Connection

```bash
# Send a test event to yourself
curl -X POST http://localhost:8000/api/v1/events/test \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Check Connection Stats

```bash
curl http://localhost:8000/api/v1/events/stats \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Manual Event Broadcasting (for testing)

```bash
curl -X POST http://localhost:8000/api/v1/events/broadcast \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "notification",
    "text_content": "Test notification",
    "meta": {"test": true}
  }'
```

## Architecture Overview

```
Backend (FastAPI)
├── EventService (event_service.py)
│   ├── Socket.IO Server
│   ├── Connection Management
│   └── Event Broadcasting
├── Event Models (event.py)
│   ├── EventType (constants)
│   ├── EventPublic (event structure)
│   └── EventBroadcast (broadcast request)
└── Event Routes (events.py)
    ├── POST /events/broadcast
    ├── GET /events/stats
    └── POST /events/test

Frontend (React + TypeScript)
├── eventService (eventService.ts)
│   ├── Socket.IO Client
│   ├── Connection Management
│   └── Subscription Management
└── Hooks (useEventBus.ts)
    ├── useEventBusConnection
    ├── useEventSubscription
    ├── useMultiEventSubscription
    └── useRoomSubscription
```

## Documentation

- **[Complete Documentation](./event_bus_system.md)** - Comprehensive guide with all features
- **[Integration Examples](./example_integration.tsx)** - Real-world usage examples
- **[Production Deployment](./production_setup.md)** - Production setup without Traefik

## Key Features

✅ **Event-Driven Architecture** - Components subscribe to specific event types
✅ **User-Specific Events** - Events can be targeted to specific users
✅ **Auto-Reconnection** - Automatic reconnection with exponential backoff
✅ **Type-Safe Events** - TypeScript types for all events
✅ **React Hooks** - Easy integration with React components
✅ **Room Support** - Topic-based subscriptions (e.g., `session_123`)
✅ **Authentication** - Secure WebSocket connections with JWT
✅ **Connection Stats** - Monitor active connections and users

## Troubleshooting

### WebSocket not connecting?

1. Check browser console for connection errors
2. Verify `VITE_API_URL` is set correctly in frontend `.env`
3. Check backend logs: `docker-compose logs -f backend | grep EventService`
4. Test with `/api/v1/events/test` endpoint

### Events not received?

1. Verify event type matches between backend and frontend
2. Check subscription is active: `console.log` inside handler
3. Verify `user_id` is set correctly when emitting events
4. Check browser Network tab for WebSocket connection

### Connection keeps dropping?

1. Check for CORS issues in backend logs
2. Verify firewall/proxy allows WebSocket connections
3. Check backend health: `/api/v1/utils/health-check`

## Next Steps

1. **Add to Root Layout**: Initialize `useEventBusConnection()` in your root component
2. **Update Components**: Add event subscriptions to components that need real-time updates
3. **Emit Events**: Add event emission to backend routes that modify data
4. **Test**: Use the test endpoints to verify everything works
5. **Monitor**: Check connection stats to see active users

---

**Need help?** See the full documentation in [event_bus_system.md](./event_bus_system.md)
