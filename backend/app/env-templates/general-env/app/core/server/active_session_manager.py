import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Context entries older than this are eligible for cleanup
_CONTEXT_TTL_SECONDS = 24 * 60 * 60  # 24 hours


@dataclass
class ActiveSession:
    """Represents an active streaming session"""
    session_id: str
    client: Any  # ClaudeSDKClient instance
    interrupt_requested: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SessionContext:
    """HMAC-verified session context with timestamp for TTL cleanup."""
    data: dict
    stored_at: float = field(default_factory=time.monotonic)


class ActiveSessionManager:
    """
    Manages active SDK sessions that are currently streaming.

    This allows interrupt requests to find the active client and call interrupt().
    The manager tracks sessions in memory and provides thread-safe access.
    Also stores HMAC-verified session context metadata per backend_session_id
    for the /session/context endpoint.
    """

    def __init__(self):
        self._active_sessions: Dict[str, ActiveSession] = {}
        self._contexts: Dict[str, SessionContext] = {}  # keyed by backend_session_id
        # Keep legacy _current_context for backward compat during transition
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
                started_at=datetime.now(UTC)
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

    # --- Per-session context (HMAC-verified) ---

    @staticmethod
    def _verify_hmac(context: dict, signature: str) -> bool:
        """Verify HMAC-SHA256 signature of session context using AGENT_AUTH_TOKEN."""
        signing_key = os.getenv("AGENT_AUTH_TOKEN")
        if not signing_key:
            logger.warning("AGENT_AUTH_TOKEN not set, skipping HMAC verification")
            return True  # Allow in dev/test environments without auth

        canonical = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(
            signing_key.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def set_session_context(self, backend_session_id: str, context: dict, signature: str | None = None):
        """
        Store HMAC-verified context for a specific backend session.

        If signature is provided and AGENT_AUTH_TOKEN is set, verifies HMAC before storing.
        Context persists across streams for the same backend_session_id.

        Args:
            backend_session_id: Backend database session ID (routing key)
            context: Session context dict
            signature: HMAC-SHA256 hex signature (optional for backward compat)
        """
        if signature and not self._verify_hmac(context, signature):
            logger.warning("HMAC verification failed for session %s — context NOT stored", backend_session_id)
            return

        async with self._lock:
            self._contexts[backend_session_id] = SessionContext(data=context)
            logger.info(
                "Set session context for %s: integration_type=%s",
                backend_session_id, context.get("integration_type"),
            )
            # Opportunistic cleanup of expired contexts
            self._cleanup_expired_contexts_unlocked()

    async def get_session_context(self, backend_session_id: str) -> Optional[dict]:
        """
        Get context for a specific backend session.

        Args:
            backend_session_id: Backend database session ID

        Returns:
            Session context dict if found, None otherwise
        """
        async with self._lock:
            entry = self._contexts.get(backend_session_id)
            return entry.data if entry else None

    async def cleanup_session_context(self, backend_session_id: str):
        """Remove context for a specific session (e.g., on session end)."""
        async with self._lock:
            if backend_session_id in self._contexts:
                del self._contexts[backend_session_id]
                logger.info("Cleaned up session context for %s", backend_session_id)

    def _cleanup_expired_contexts_unlocked(self):
        """Remove contexts older than TTL. Must be called under lock."""
        now = time.monotonic()
        expired = [
            sid for sid, ctx in self._contexts.items()
            if now - ctx.stored_at > _CONTEXT_TTL_SECONDS
        ]
        for sid in expired:
            del self._contexts[sid]
            logger.info("TTL-expired session context for %s", sid)

    # --- Legacy single-context API (backward compat) ---

    async def set_current_context(self, context: dict):
        """
        Store session context metadata when a stream starts.

        Also stores into per-session context if backend_session_id is present.

        Args:
            context: Session context dict with integration_type, agent_id, is_clone, etc.
        """
        async with self._lock:
            self._current_context = context
            logger.info(f"Set session context: integration_type={context.get('integration_type')}")

    async def get_current_context(self) -> Optional[dict]:
        """
        Retrieve the current session context (legacy single-session API).

        Returns:
            Session context dict if set, None otherwise
        """
        async with self._lock:
            return self._current_context

    async def clear_context(self):
        """Clear the legacy current session context when stream ends."""
        async with self._lock:
            self._current_context = None
            logger.info("Cleared session context")


# Global singleton instance
active_session_manager = ActiveSessionManager()
