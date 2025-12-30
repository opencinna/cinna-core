"""EventService for managing WebSocket-based real-time events."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import socketio

from app.models.event import EventPublic, EventBroadcast

logger = logging.getLogger(__name__)


class EventService:
    """Service for managing real-time events via WebSocket."""

    def __init__(self):
        """Initialize the event service with a Socket.IO async server."""
        # Create Socket.IO server with async mode
        self.sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins="*",  # Configure based on CORS settings
            logger=True,
            engineio_logger=True,
        )

        # Track active connections: {sid: ConnectionInfo}
        self.connections: dict[str, dict[str, Any]] = {}

        # Register event handlers
        self._register_handlers()

    def _register_handlers(self):
        """Register Socket.IO event handlers."""

        @self.sio.event
        async def connect(sid, environ, auth):
            """Handle client connection."""
            logger.info(f"Client connecting: {sid}")

            # Extract user_id from auth data
            user_id = auth.get("user_id") if auth else None

            if not user_id:
                logger.warning(f"Connection {sid} rejected: no user_id in auth")
                return False  # Reject connection

            # Store connection info
            self.connections[sid] = {
                "sid": sid,
                "user_id": UUID(user_id),
                "connected_at": datetime.utcnow(),
                "rooms": [],
            }

            # Join user-specific room
            user_room = f"user_{user_id}"
            await self.sio.enter_room(sid, user_room)
            self.connections[sid]["rooms"].append(user_room)

            logger.info(f"Client {sid} connected for user {user_id}, joined room: {user_room}")
            return True

        @self.sio.event
        async def disconnect(sid):
            """Handle client disconnection."""
            if sid in self.connections:
                user_id = self.connections[sid]["user_id"]
                logger.info(f"Client {sid} disconnected (user: {user_id})")
                del self.connections[sid]
            else:
                logger.info(f"Client {sid} disconnected (unknown)")

        @self.sio.event
        async def subscribe(sid, data):
            """Handle subscription to specific event types or rooms.

            Args:
                data: Dict with 'room' or 'event_type' to subscribe to
            """
            if sid not in self.connections:
                logger.warning(f"Subscribe request from unknown connection: {sid}")
                return {"status": "error", "message": "Not authenticated"}

            room = data.get("room")
            if room:
                await self.sio.enter_room(sid, room)
                self.connections[sid]["rooms"].append(room)
                logger.info(f"Client {sid} subscribed to room: {room}")
                return {"status": "success", "room": room}

            return {"status": "error", "message": "No room specified"}

        @self.sio.event
        async def unsubscribe(sid, data):
            """Handle unsubscription from specific rooms.

            Args:
                data: Dict with 'room' to unsubscribe from
            """
            if sid not in self.connections:
                logger.warning(f"Unsubscribe request from unknown connection: {sid}")
                return {"status": "error", "message": "Not authenticated"}

            room = data.get("room")
            if room:
                await self.sio.leave_room(sid, room)
                if room in self.connections[sid]["rooms"]:
                    self.connections[sid]["rooms"].remove(room)
                logger.info(f"Client {sid} unsubscribed from room: {room}")
                return {"status": "success", "room": room}

            return {"status": "error", "message": "No room specified"}

        @self.sio.event
        async def ping(sid):
            """Handle ping from client (for keepalive)."""
            return {"status": "pong", "timestamp": datetime.utcnow().isoformat()}

    async def emit_event(
        self,
        event_type: str,
        model_id: UUID | None = None,
        text_content: str | None = None,
        meta: dict[str, Any] | None = None,
        user_id: UUID | None = None,
        room: str | None = None,
    ):
        """Emit an event to connected clients.

        Args:
            event_type: Type of event (e.g., 'session_updated')
            model_id: ID of the related model
            text_content: Optional notification text
            meta: Additional metadata
            user_id: Target specific user (will send to user_{user_id} room)
            room: Target specific room (alternative to user_id)
        """
        event = EventPublic(
            type=event_type,
            model_id=model_id,
            text_content=text_content,
            meta=meta or {},
            user_id=user_id,
            timestamp=datetime.utcnow(),
        )

        event_data = event.model_dump(mode="json")

        # Determine target room
        target_room = room
        if user_id and not target_room:
            target_room = f"user_{user_id}"

        if target_room:
            # Send to specific room
            logger.info(f"Emitting event {event_type} to room {target_room}")
            await self.sio.emit("event", event_data, room=target_room)
        else:
            # Broadcast to all connected clients
            logger.info(f"Broadcasting event {event_type} to all clients")
            await self.sio.emit("event", event_data)

    async def broadcast_event(self, broadcast: EventBroadcast):
        """Broadcast an event using EventBroadcast model.

        Args:
            broadcast: EventBroadcast model with all event details
        """
        await self.emit_event(
            event_type=broadcast.type,
            model_id=broadcast.model_id,
            text_content=broadcast.text_content,
            meta=broadcast.meta,
            user_id=broadcast.user_id,
            room=broadcast.room,
        )

    def get_connected_users(self) -> list[UUID]:
        """Get list of currently connected user IDs."""
        return list({conn["user_id"] for conn in self.connections.values()})

    def is_user_connected(self, user_id: UUID) -> bool:
        """Check if a specific user is connected."""
        return any(conn["user_id"] == user_id for conn in self.connections.values())

    def get_connection_count(self) -> int:
        """Get total number of active connections."""
        return len(self.connections)

    def get_asgi_app(self):
        """Get the ASGI app for Socket.IO."""
        return socketio.ASGIApp(self.sio, socketio_path="/")


# Global event service instance
event_service = EventService()
