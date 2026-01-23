"""
Active Streaming Manager - Tracks ongoing message streams between backend and agent environments.

This manager decouples frontend connections from backend-to-agent-env streaming,
allowing the backend to continue processing even if the frontend disconnects.
"""

import asyncio
import logging
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class ActiveStream:
    """Represents an active streaming session between backend and agent env"""
    session_id: UUID
    external_session_id: Optional[str]
    started_at: datetime = field(default_factory=datetime.utcnow)
    is_interrupted: bool = False
    is_completed: bool = False
    interrupt_pending: bool = False  # Interrupt requested before external_session_id available
    # In-memory event buffer for incremental persistence
    streaming_events: list = field(default_factory=list)
    last_flushed_seq: int = 0
    # Accumulated assistant text content
    accumulated_content: str = ""


class ActiveStreamingManager:
    """
    Manages active streaming sessions between backend and agent environments.

    This allows:
    - Tracking which sessions are currently streaming
    - Frontend to reconnect to ongoing streams
    - Graceful handling of frontend disconnections
    - Proper interrupt propagation without breaking streams
    """

    def __init__(self):
        self._active_streams: Dict[UUID, ActiveStream] = {}
        self._lock = asyncio.Lock()

    async def register_stream(
        self,
        session_id: UUID,
        external_session_id: Optional[str] = None
    ):
        """
        Register a new active streaming session.

        Args:
            session_id: Backend session UUID
            external_session_id: External SDK session ID (may be None for new sessions)
        """
        async with self._lock:
            self._active_streams[session_id] = ActiveStream(
                session_id=session_id,
                external_session_id=external_session_id,
                started_at=datetime.utcnow()
            )
            logger.info(f"Registered active stream for session {session_id}")

    async def update_external_session_id(
        self,
        session_id: UUID,
        external_session_id: str
    ) -> bool:
        """
        Update external session ID for an active stream.

        This is called when we get the external_session_id from the first response.

        Args:
            session_id: Backend session UUID
            external_session_id: External SDK session ID

        Returns:
            True if there's a pending interrupt that needs to be forwarded
        """
        async with self._lock:
            if session_id in self._active_streams:
                stream = self._active_streams[session_id]
                stream.external_session_id = external_session_id
                logger.info(f"Updated external_session_id for session {session_id}: {external_session_id}")
                # Return whether interrupt is pending
                return stream.interrupt_pending
            return False

    async def unregister_stream(self, session_id: UUID):
        """
        Remove stream when it completes.

        Args:
            session_id: Backend session UUID to remove
        """
        async with self._lock:
            if session_id in self._active_streams:
                stream = self._active_streams[session_id]
                duration = (datetime.utcnow() - stream.started_at).total_seconds()
                del self._active_streams[session_id]
                logger.info(
                    f"Unregistered stream for session {session_id} "
                    f"(duration={duration:.1f}s, interrupted={stream.is_interrupted})"
                )

    async def mark_interrupted(self, session_id: UUID) -> bool:
        """
        Mark a stream as interrupted (but don't unregister it).

        The stream will continue until completion, just with interrupted flag set.

        Args:
            session_id: Backend session UUID

        Returns:
            True if marked successfully, False if session not found
        """
        async with self._lock:
            if session_id not in self._active_streams:
                logger.warning(f"Cannot mark interrupted: session {session_id} not streaming")
                return False

            self._active_streams[session_id].is_interrupted = True
            logger.info(f"Marked session {session_id} as interrupted")
            return True

    async def request_interrupt(self, session_id: UUID) -> dict:
        """
        Request interrupt for a streaming session.

        This works even if external_session_id is not yet available.
        If external_session_id exists, returns it for immediate forwarding.
        If not, marks interrupt as pending.

        Args:
            session_id: Backend session UUID

        Returns:
            {
                "found": bool,
                "external_session_id": str | None,
                "pending": bool  # True if interrupt queued for later
            }
        """
        async with self._lock:
            if session_id not in self._active_streams:
                logger.warning(f"Cannot interrupt: session {session_id} not streaming")
                return {"found": False, "external_session_id": None, "pending": False}

            stream = self._active_streams[session_id]

            if stream.external_session_id:
                # External session ID available - can forward immediately
                stream.is_interrupted = True
                logger.info(f"Interrupt requested for session {session_id} (external_id: {stream.external_session_id})")
                return {
                    "found": True,
                    "external_session_id": stream.external_session_id,
                    "pending": False
                }
            else:
                # External session ID not yet available - queue interrupt
                stream.interrupt_pending = True
                logger.info(f"Interrupt queued for session {session_id} (waiting for external_session_id)")
                return {
                    "found": True,
                    "external_session_id": None,
                    "pending": True
                }

    async def is_streaming(self, session_id: UUID) -> bool:
        """
        Check if a session is currently streaming.

        Args:
            session_id: Backend session UUID

        Returns:
            True if session is actively streaming
        """
        async with self._lock:
            return session_id in self._active_streams

    async def get_stream_info(self, session_id: UUID) -> Optional[dict]:
        """
        Get information about an active stream.

        Args:
            session_id: Backend session UUID

        Returns:
            Stream info dict or None if not streaming
        """
        async with self._lock:
            if session_id not in self._active_streams:
                return None

            stream = self._active_streams[session_id]
            duration = (datetime.utcnow() - stream.started_at).total_seconds()

            return {
                "session_id": str(stream.session_id),
                "external_session_id": stream.external_session_id,
                "started_at": stream.started_at.isoformat(),
                "duration_seconds": duration,
                "is_interrupted": stream.is_interrupted,
                "is_completed": stream.is_completed
            }

    async def append_streaming_event(self, session_id: UUID, event: dict) -> None:
        """
        Append a streaming event to the in-memory buffer for an active stream.

        Args:
            session_id: Backend session UUID
            event: Event dict with event_seq already assigned
        """
        async with self._lock:
            if session_id in self._active_streams:
                stream = self._active_streams[session_id]
                stream.streaming_events.append(event)
                # Track accumulated assistant text
                if event.get("type") == "assistant" and event.get("content"):
                    stream.accumulated_content += event["content"]

    async def update_last_flushed_seq(self, session_id: UUID, seq: int) -> None:
        """
        Update the last flushed sequence number for a stream.

        Args:
            session_id: Backend session UUID
            seq: Last flushed event_seq
        """
        async with self._lock:
            if session_id in self._active_streams:
                self._active_streams[session_id].last_flushed_seq = seq

    async def get_stream_events(self, session_id: UUID) -> Optional[dict]:
        """
        Get the streaming events buffer and accumulated content for an active stream.

        Args:
            session_id: Backend session UUID

        Returns:
            Dict with streaming_events, accumulated_content, and last_flushed_seq, or None
        """
        async with self._lock:
            if session_id not in self._active_streams:
                return None
            stream = self._active_streams[session_id]
            return {
                "streaming_events": list(stream.streaming_events),
                "accumulated_content": stream.accumulated_content,
                "last_flushed_seq": stream.last_flushed_seq,
            }

    async def get_all_active_streams(self) -> list[dict]:
        """
        Get all active streams (for debugging/monitoring).

        Returns:
            List of stream info dicts
        """
        async with self._lock:
            result = []
            for session_id in self._active_streams.keys():
                stream = self._active_streams[session_id]
                duration = (datetime.utcnow() - stream.started_at).total_seconds()
                result.append({
                    "session_id": str(stream.session_id),
                    "external_session_id": stream.external_session_id,
                    "started_at": stream.started_at.isoformat(),
                    "duration_seconds": duration,
                    "is_interrupted": stream.is_interrupted,
                    "is_completed": stream.is_completed
                })
            return result


# Global singleton instance
active_streaming_manager = ActiveStreamingManager()
