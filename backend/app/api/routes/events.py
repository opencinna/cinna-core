"""WebSocket event routes for real-time communication."""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser
from app.models.events.event import EventBroadcast
from app.services.events.event_service import event_service

router = APIRouter(prefix="/events", tags=["events"])


@router.post("/broadcast")
async def broadcast_event(
    *, current_user: CurrentUser, event: EventBroadcast
) -> Any:
    """
    Broadcast an event to connected WebSocket clients.

    This endpoint allows broadcasting events to:
    - Specific users (via user_id)
    - Specific rooms (via room)
    - All connected clients (if neither user_id nor room is specified)

    Example event types:
    - session_updated
    - message_created
    - activity_created
    - notification
    """
    # If user_id is specified, ensure current user has permission
    # (only allow broadcasting to self unless superuser)
    if event.user_id and not current_user.is_superuser:
        if event.user_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="Not enough permissions to broadcast to other users"
            )

    await event_service.broadcast_event(event)

    return {
        "status": "success",
        "message": "Event broadcasted",
        "event_type": event.type,
    }


@router.get("/stats")
async def get_connection_stats(*, current_user: CurrentUser) -> Any:
    """
    Get WebSocket connection statistics.

    Returns:
        - connection_count: Total number of active WebSocket connections
        - connected_users: List of user IDs currently connected
        - is_current_user_connected: Whether the current user is connected
    """
    connected_users = event_service.get_connected_users()
    connection_count = event_service.get_connection_count()
    is_connected = event_service.is_user_connected(current_user.id)

    return {
        "connection_count": connection_count,
        "connected_users": [str(uid) for uid in connected_users],
        "is_current_user_connected": is_connected,
    }


@router.post("/test")
async def test_event(*, current_user: CurrentUser) -> Any:
    """
    Send a test event to the current user.

    Useful for testing WebSocket connectivity.
    """
    await event_service.emit_event(
        event_type="notification",
        text_content=f"Test notification for user {current_user.full_name or current_user.email}",
        meta={"source": "test_endpoint", "test": True},
        user_id=current_user.id,
    )

    return {
        "status": "success",
        "message": "Test event sent",
        "user_id": str(current_user.id),
    }
