"""EventService for managing WebSocket-based real-time events."""

import logging
import asyncio
from datetime import datetime
from typing import Any, Callable, Awaitable
from uuid import UUID
import concurrent.futures

import socketio

from app.models.event import EventPublic, EventBroadcast
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)

# Type alias for event handler functions
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


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

        # Thread pool for background tasks (to avoid blocking event loop)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="event_service_bg"
        )

        # Backend event handlers registry: {event_type: [handler_functions]}
        self._backend_handlers: dict[str, list[EventHandler]] = {}

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

        @self.sio.event
        async def agent_usage_intent(sid, data):
            """
            Handle agent usage intent event from frontend.

            This event is sent when the user shows intention to use an agent:
            - Opening a session with that agent
            - Clicking on the agent in the dashboard
            - Navigating to agent's page

            Args:
                data: Dict with 'environment_id' and optionally 'agent_id'

            Returns:
                Status dict indicating if activation was triggered
            """
            if sid not in self.connections:
                logger.warning(f"agent_usage_intent from unknown connection: {sid}")
                return {"status": "error", "message": "Not authenticated"}

            user_id = self.connections[sid]["user_id"]
            environment_id = data.get("environment_id")

            if not environment_id:
                logger.warning(f"agent_usage_intent without environment_id from user {user_id}")
                return {"status": "error", "message": "environment_id required"}

            try:
                # Import here to avoid circular dependencies
                from app.core.db import engine as db_engine
                from sqlmodel import Session as DBSession
                from app.models.environment import AgentEnvironment
                from app.models.agent import Agent
                import asyncio

                # Check environment status and trigger activation if needed
                with DBSession(db_engine) as session:
                    environment = session.get(AgentEnvironment, UUID(environment_id))
                    if not environment:
                        logger.warning(f"Environment {environment_id} not found")
                        return {"status": "error", "message": "Environment not found"}

                    # Update last_activity_at
                    environment.last_activity_at = datetime.utcnow()
                    session.add(environment)
                    session.commit()

                    # If suspended, trigger activation in background
                    if environment.status == "suspended":
                        logger.info(f"User {user_id} triggered activation for suspended environment {environment_id}")

                        # Get agent
                        agent = session.get(Agent, environment.agent_id)
                        if not agent:
                            logger.error(f"Agent {environment.agent_id} not found for environment {environment_id}")
                            return {"status": "error", "message": "Agent not found"}

                        # Store IDs for background task (avoid passing detached ORM objects)
                        env_id_for_activation = environment.id
                        agent_id_for_activation = agent.id

                        # Activate in background using current event loop (NOT asyncio.run!)
                        # asyncio.run() creates a new event loop that gets destroyed, cancelling all tasks
                        async def _activate_async():
                            """Activate environment with fresh DB session in main event loop"""
                            from app.core.db import engine as db_engine
                            from sqlmodel import Session as DBSession
                            from app.models.environment import AgentEnvironment
                            from app.models.agent import Agent
                            from app.services.environment_lifecycle import EnvironmentLifecycleManager

                            with DBSession(db_engine) as fresh_session:
                                fresh_env = fresh_session.get(AgentEnvironment, env_id_for_activation)
                                fresh_agent = fresh_session.get(Agent, agent_id_for_activation)

                                if not fresh_env or not fresh_agent:
                                    logger.error(f"Environment or agent not found during activation")
                                    return False

                                lifecycle_manager = EnvironmentLifecycleManager()
                                result = await lifecycle_manager.activate_suspended_environment(
                                    db_session=fresh_session,
                                    environment=fresh_env,
                                    agent=fresh_agent,
                                    emit_events=True
                                )

                                logger.info(f"Background activation completed for environment {env_id_for_activation}")
                                return result

                        create_task_with_error_logging(
                            _activate_async(),
                            task_name=f"activate_from_usage_intent_{env_id_for_activation}"
                        )

                        return {
                            "status": "activating",
                            "message": "Environment activation started",
                            "environment_id": str(environment_id)
                        }

                    return {
                        "status": "ok",
                        "message": f"Environment status: {environment.status}",
                        "environment_id": str(environment_id)
                    }

            except Exception as e:
                logger.error(f"Error handling agent_usage_intent: {e}", exc_info=True)
                return {"status": "error", "message": str(e)}

    def register_handler(self, event_type: str, handler: EventHandler):
        """Register a backend handler for a specific event type.

        This allows backend services to react to events without using WebSockets.

        Args:
            event_type: Event type to listen for (e.g., 'stream_completed')
            handler: Async function that accepts event data dict
        """
        if event_type not in self._backend_handlers:
            self._backend_handlers[event_type] = []
        self._backend_handlers[event_type].append(handler)
        logger.info(f"Registered backend handler for event type: {event_type}")

    async def _call_backend_handlers(self, event_type: str, event_data: dict[str, Any]):
        """Call all registered backend handlers for an event type.

        Args:
            event_type: Event type
            event_data: Full event data including type, model_id, meta, etc.
        """
        handlers = self._backend_handlers.get(event_type, [])
        if not handlers:
            return

        logger.debug(f"Calling {len(handlers)} backend handler(s) for event type: {event_type}")

        # Call all handlers in background tasks (non-blocking)
        for i, handler in enumerate(handlers):
            try:
                # Create task to run handler without awaiting
                # Use error logging wrapper to prevent silent failures and premature cancellation
                create_task_with_error_logging(
                    handler(event_data),
                    task_name=f"event_handler_{event_type}_{i}"
                )
            except Exception as e:
                logger.error(f"Error calling backend handler for {event_type}: {e}", exc_info=True)

    async def emit_event(
        self,
        event_type: str,
        model_id: UUID | None = None,
        text_content: str | None = None,
        meta: dict[str, Any] | None = None,
        user_id: UUID | None = None,
        room: str | None = None,
    ):
        """Emit an event to connected clients and backend handlers.

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

        # Call backend handlers (non-blocking)
        await self._call_backend_handlers(event_type, event_data)

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

    def is_user_online(self, user_id: UUID) -> bool:
        """
        Check if a specific user is online (has active WebSocket connection).

        Args:
            user_id: User UUID

        Returns:
            True if user has at least one active connection
        """
        return any(conn["user_id"] == user_id for conn in self.connections.values())

    def is_user_connected(self, user_id: UUID) -> bool:
        """Check if a specific user is connected (alias for is_user_online)."""
        return self.is_user_online(user_id)

    def get_connection_count(self) -> int:
        """Get total number of active connections."""
        return len(self.connections)

    async def emit_stream_event(
        self,
        session_id: UUID,
        event_type: str,
        event_data: dict[str, Any],
    ):
        """
        Emit a streaming event to a session-specific room.

        Args:
            session_id: Session UUID
            event_type: Type of streaming event (assistant, tool, etc.)
            event_data: Full event data including content, metadata
        """
        room = f"session_{session_id}_stream"

        # Emit to session-specific streaming room
        await self.sio.emit(
            "stream_event",  # WebSocket event name
            {
                "session_id": str(session_id),
                "event_type": event_type,
                "data": event_data,
                "timestamp": datetime.utcnow().isoformat()
            },
            room=room
        )

        logger.debug(f"Emitted stream event {event_type} to room {room}")

    def _activate_environment_sync(self, environment_id: str, agent_id: str):
        """
        Synchronous background task to activate a suspended environment.
        Runs in a separate thread to avoid blocking the event loop.

        Args:
            environment_id: Environment UUID as string
            agent_id: Agent UUID as string
        """
        try:
            # Import here to avoid circular dependencies
            from app.core.db import engine as db_engine
            from sqlmodel import Session as DBSession
            from app.models.environment import AgentEnvironment
            from app.models.agent import Agent
            from app.services.environment_lifecycle import EnvironmentLifecycleManager

            # Use fresh DB session for background task
            with DBSession(db_engine) as session:
                environment = session.get(AgentEnvironment, UUID(environment_id))
                agent = session.get(Agent, UUID(agent_id))

                if not environment or not agent:
                    logger.error(f"Environment or agent not found: env={environment_id}, agent={agent_id}")
                    return

                # Activate the environment using asyncio.run for async operations
                lifecycle_manager = EnvironmentLifecycleManager()
                asyncio.run(
                    lifecycle_manager.activate_suspended_environment(
                        db_session=session,
                        environment=environment,
                        agent=agent,
                        emit_events=True
                    )
                )

                logger.info(f"Background activation completed for environment {environment_id}")

        except Exception as e:
            logger.error(f"Background activation failed for environment {environment_id}: {e}", exc_info=True)

    def shutdown(self):
        """Shutdown the event service and cleanup resources."""
        logger.info("Shutting down EventService executor...")
        self.executor.shutdown(wait=True, cancel_futures=True)
        logger.info("EventService executor shut down")

    def get_asgi_app(self):
        """Get the ASGI app for Socket.IO."""
        return socketio.ASGIApp(self.sio, socketio_path="/")


# Global event service instance
event_service = EventService()
