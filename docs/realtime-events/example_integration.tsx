/**
 * Example: How to integrate the Event Bus into the LatestSessions component
 *
 * This file shows how the LatestSessions component can subscribe to real-time
 * session events and automatically update the UI when sessions are created,
 * updated, or deleted.
 */

import { useQuery, useQueryClient } from "@tantml:invoke"
import { SessionsService, type SessionPublic } from "@/client"
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { useCustomToast } from "@/hooks/useCustomToast"

export function LatestSessions() {
  const queryClient = useQueryClient()
  const { showInfoToast, showSuccessToast } = useCustomToast()

  // Fetch sessions data
  const {
    data: sessions,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["sessions", "latest"],
    queryFn: () => SessionsService.readSessions({ limit: 10 }),
  })

  // Subscribe to session_updated events
  useEventSubscription(EventTypes.SESSION_UPDATED, (event) => {
    console.log("[LatestSessions] Session updated:", event.model_id, event.meta)

    // Invalidate sessions query to trigger refetch
    queryClient.invalidateQueries({ queryKey: ["sessions"] })

    // Show notification if provided
    if (event.text_content) {
      showInfoToast(event.text_content)
    }
  })

  // Subscribe to session_created events
  useEventSubscription(EventTypes.SESSION_CREATED, (event) => {
    console.log("[LatestSessions] New session created:", event.model_id)

    // Invalidate sessions to show the new session
    queryClient.invalidateQueries({ queryKey: ["sessions"] })

    // Show success notification
    if (event.text_content) {
      showSuccessToast(event.text_content)
    } else {
      showSuccessToast("New session created")
    }
  })

  // Subscribe to session_deleted events
  useEventSubscription(EventTypes.SESSION_DELETED, (event) => {
    console.log("[LatestSessions] Session deleted:", event.model_id)

    // Remove deleted session from cache without refetching
    queryClient.setQueryData(["sessions", "latest"], (old: any) => {
      if (!old?.data) return old
      return {
        ...old,
        data: old.data.filter((s: SessionPublic) => s.id !== event.model_id),
      }
    })

    // Show notification
    if (event.text_content) {
      showInfoToast(event.text_content)
    }
  })

  if (isLoading) {
    return <div>Loading sessions...</div>
  }

  if (error) {
    return <div>Error loading sessions</div>
  }

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-bold">Latest Sessions</h2>
      <div className="grid gap-4">
        {sessions?.data.map((session) => (
          <div key={session.id} className="p-4 border rounded">
            <h3>{session.title || "Untitled Session"}</h3>
            <p className="text-sm text-gray-500">
              Last updated: {new Date(session.updated_at).toLocaleString()}
            </p>
          </div>
        ))}
      </div>
    </div>
  )
}


/**
 * Example: Backend integration for emitting events when sessions change
 */

/*
// In backend/app/api/routes/sessions.py

from app.services.event_service import event_service
from app.models.event import EventType

@router.put("/{session_id}", response_model=SessionPublic)
async def update_session(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
    session_in: SessionUpdate
) -> Any:
    """
    Update a session.
    """
    db_session = session.get(Session, session_id)
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (db_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Update session
    update_dict = session_in.model_dump(exclude_unset=True)
    db_session.sqlmodel_update(update_dict)
    session.add(db_session)
    session.commit()
    session.refresh(db_session)

    # Emit real-time event to notify connected clients
    await event_service.emit_event(
        event_type=EventType.SESSION_UPDATED,
        model_id=session_id,
        text_content=f"Session '{db_session.title}' has been updated",
        meta={
            "session_id": str(session_id),
            "title": db_session.title,
            "status": db_session.status,
            "updated_at": db_session.updated_at.isoformat()
        },
        user_id=current_user.id
    )

    return db_session


@router.post("/", response_model=SessionPublic)
async def create_session(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_in: SessionCreate
) -> Any:
    """
    Create new session.
    """
    session_data = session_in.model_dump()
    db_session = Session(**session_data, user_id=current_user.id)
    session.add(db_session)
    session.commit()
    session.refresh(db_session)

    # Emit session_created event
    await event_service.emit_event(
        event_type=EventType.SESSION_CREATED,
        model_id=db_session.id,
        text_content=f"New session '{db_session.title}' created",
        meta={
            "session_id": str(db_session.id),
            "agent_id": str(db_session.agent_id) if db_session.agent_id else None,
            "title": db_session.title
        },
        user_id=current_user.id
    )

    return db_session


@router.delete("/{session_id}")
async def delete_session(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID
) -> Message:
    """
    Delete a session.
    """
    db_session = session.get(Session, session_id)
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (db_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    session_title = db_session.title
    session.delete(db_session)
    session.commit()

    # Emit session_deleted event
    await event_service.emit_event(
        event_type=EventType.SESSION_DELETED,
        model_id=session_id,
        text_content=f"Session '{session_title}' has been deleted",
        meta={
            "session_id": str(session_id),
            "title": session_title
        },
        user_id=current_user.id
    )

    return Message(message="Session deleted successfully")
*/


/**
 * Example: Real-time activity updates
 */

/*
export function ActivitiesFeed() {
  const queryClient = useQueryClient()
  const { showInfoToast } = useCustomToast()

  // Subscribe to activity events
  useEventSubscription(EventTypes.ACTIVITY_CREATED, (event) => {
    console.log("[Activities] New activity:", event.model_id)

    // Invalidate activities query
    queryClient.invalidateQueries({ queryKey: ["activities"] })

    // Show notification if action is required
    if (event.meta?.action_required) {
      showInfoToast(event.text_content || "New activity requires your attention", {
        variant: "destructive"
      })
    }
  })

  // ... rest of component
}
*/


/**
 * Example: Real-time message updates in chat
 */

/*
export function ChatMessages({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient()

  // Subscribe to message_created events only for this session
  useEventSubscription(EventTypes.MESSAGE_CREATED, (event) => {
    // Filter events for this session
    if (event.meta?.session_id === sessionId) {
      console.log("[Chat] New message in session:", sessionId)

      // Invalidate messages query for this session
      queryClient.invalidateQueries({
        queryKey: ["sessions", sessionId, "messages"]
      })
    }
  })

  // Subscribe to stream events
  useEventSubscription(EventTypes.STREAM_STARTED, (event) => {
    if (event.meta?.session_id === sessionId) {
      console.log("[Chat] Stream started for session:", sessionId)
      // Show typing indicator
    }
  })

  useEventSubscription(EventTypes.STREAM_COMPLETED, (event) => {
    if (event.meta?.session_id === sessionId) {
      console.log("[Chat] Stream completed for session:", sessionId)
      // Hide typing indicator and refresh messages
      queryClient.invalidateQueries({
        queryKey: ["sessions", sessionId, "messages"]
      })
    }
  })

  // ... rest of component
}
*/


/**
 * Example: Global event listener for debugging
 */

/*
export function GlobalEventLogger() {
  // Subscribe to all events (useful for debugging)
  useEventSubscription("*", (event) => {
    console.log(`[GlobalEvents] ${event.type}:`, event)

    // You can also log to external services
    // analytics.track(`event_${event.type}`, {
    //   model_id: event.model_id,
    //   timestamp: event.timestamp
    // })
  })

  return null // This component doesn't render anything
}

// Add to root layout:
// <GlobalEventLogger />
*/


/**
 * Example: Connection status indicator
 */

/*
import { useEventBusStatus } from "@/hooks/useEventBus"

export function ConnectionStatusIndicator() {
  const { isConnected } = useEventBusStatus()

  if (!isConnected) {
    return (
      <div className="bg-yellow-100 text-yellow-800 px-3 py-1 rounded text-sm">
        ⚠️ Reconnecting to real-time updates...
      </div>
    )
  }

  return (
    <div className="bg-green-100 text-green-800 px-3 py-1 rounded text-sm">
      ✓ Real-time updates active
    </div>
  )
}
*/
