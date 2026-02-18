import asyncio
import logging
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ActiveSession:
    """Represents an active streaming session"""
    session_id: str
    client: Any  # ClaudeSDKClient instance
    interrupt_requested: bool = False
    started_at: datetime = field(default_factory=datetime.utcnow)


class ActiveSessionManager:
    """
    Manages active SDK sessions that are currently streaming.

    This allows interrupt requests to find the active client and call interrupt().
    The manager tracks sessions in memory and provides thread-safe access.
    Also stores session context metadata for the /session/context endpoint.
    """

    def __init__(self):
        self._active_sessions: Dict[str, ActiveSession] = {}
        self._current_context: Optional[dict] = None
        self._lock = asyncio.Lock()

    async def register_session(self, session_id: str, client: Any):
        """
        Register a new active session.

        Args:
            session_id: External SDK session ID
            client: ClaudeSDKClient instance
        """
        async with self._lock:
            self._active_sessions[session_id] = ActiveSession(
                session_id=session_id,
                client=client,
                started_at=datetime.utcnow()
            )
            logger.info(f"Registered active session: {session_id}")

    async def unregister_session(self, session_id: str):
        """
        Remove session when streaming completes.

        Args:
            session_id: External SDK session ID to remove
        """
        async with self._lock:
            if session_id in self._active_sessions:
                del self._active_sessions[session_id]
                logger.info(f"Unregistered active session: {session_id}")

    async def request_interrupt(self, session_id: str) -> bool:
        """
        Request interrupt for an active session.

        This sets a flag that will be checked during message streaming.
        The actual SDK interrupt() is called from within the streaming loop.

        Args:
            session_id: External SDK session ID to interrupt

        Returns:
            True if interrupt was requested, False if session not found
        """
        async with self._lock:
            if session_id not in self._active_sessions:
                logger.warning(f"Cannot interrupt: session {session_id} not found or not active")
                return False

            session = self._active_sessions[session_id]
            session.interrupt_requested = True
            logger.info(f"Interrupt requested for session: {session_id}")
            return True

    async def check_interrupt_requested(self, session_id: str) -> bool:
        """
        Check if interrupt was requested for this session.

        Args:
            session_id: External SDK session ID to check

        Returns:
            True if interrupt was requested, False otherwise
        """
        async with self._lock:
            if session_id in self._active_sessions:
                return self._active_sessions[session_id].interrupt_requested
            return False

    async def get_active_client(self, session_id: str) -> Optional[Any]:
        """
        Get the active client for a session (for calling interrupt()).

        Args:
            session_id: External SDK session ID

        Returns:
            ClaudeSDKClient instance if session is active, None otherwise
        """
        async with self._lock:
            if session_id in self._active_sessions:
                return self._active_sessions[session_id].client
            return None

    async def set_current_context(self, context: dict):
        """
        Store session context metadata when a stream starts.

        Args:
            context: Session context dict with integration_type, agent_id, is_clone, etc.
        """
        async with self._lock:
            self._current_context = context
            logger.info(f"Set session context: integration_type={context.get('integration_type')}")

    async def get_current_context(self) -> Optional[dict]:
        """
        Retrieve the current session context.

        Returns:
            Session context dict if set, None otherwise
        """
        async with self._lock:
            return self._current_context

    async def clear_context(self):
        """Clear the current session context when stream ends."""
        async with self._lock:
            self._current_context = None
            logger.info("Cleared session context")


# Global singleton instance
active_session_manager = ActiveSessionManager()
