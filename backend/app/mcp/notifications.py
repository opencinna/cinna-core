"""
MCP resource change notification helpers.

After agent work (send_message) or file uploads, the workspace may have changed.
These helpers send `notifications/resources/list_changed` to connected MCP clients
so they re-fetch the resource list.
"""
import logging

logger = logging.getLogger(__name__)


async def notify_resource_list_changed(session: object) -> None:
    """Send a resource list changed notification via a specific MCP ServerSession.

    Args:
        session: An MCP ServerSession instance with send_resource_list_changed().
    """
    await session.send_resource_list_changed()


async def broadcast_resource_list_changed(connector_id: str) -> None:
    """Send resource list changed notification to ALL active sessions of a connector.

    Used by contexts that don't have direct MCP session access (e.g. upload route).
    Catches exceptions per session so one broken session doesn't prevent others
    from being notified.
    """
    from app.mcp.server import mcp_registry

    sessions = mcp_registry.get_sessions_for_connector(connector_id)
    if not sessions:
        return

    for session in sessions:
        try:
            await notify_resource_list_changed(session)
        except Exception:
            logger.debug(
                "[MCP] Failed to send resource list changed notification to a session "
                "for connector %s (session may have disconnected)",
                connector_id,
                exc_info=True,
            )
