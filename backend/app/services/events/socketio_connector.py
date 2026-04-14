"""
Injectable connector for Socket.IO outbound communication.

Follows the same DI pattern as smtp_connector / imap_connector:
- Module-level instance used by EventService
- Tests patch the module-level instance with a stub
"""
import logging
from typing import Any

import socketio

logger = logging.getLogger(__name__)


class SocketIOConnector:
    """Owns the Socket.IO server and wraps outbound emit operations.

    EventService accesses `self.sio` for handler registration and room
    management, and calls `emit()` for outbound messages.  Tests replace
    the module-level instance with a stub that captures emits.
    """

    def __init__(self):
        self.sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins="*",
            logger=True,
            engineio_logger=False,
        )

    async def emit(self, event: str, data: Any, room: str | None = None):
        """Emit a Socket.IO event to connected clients."""
        if room:
            await self.sio.emit(event, data, room=room)
        else:
            await self.sio.emit(event, data)

    def get_asgi_app(self):
        """Get the ASGI app for mounting Socket.IO."""
        return socketio.ASGIApp(self.sio, socketio_path="/")


# Module-level default instance (patched in tests)
socketio_connector = SocketIOConnector()
