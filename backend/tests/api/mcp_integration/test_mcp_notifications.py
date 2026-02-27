"""
MCP resource change notification tests.

Verifies:
  - listChanged capability is declared during MCP initialize
  - send_message sends resource list changed notification
  - notification failure after send_message is non-fatal
  - broadcast_resource_list_changed works with active sessions
  - broadcast_resource_list_changed is a no-op with no sessions
  - session registration and cleanup in MCPServerRegistry
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.mcp.server import (
    create_mcp_server_for_connector,
    MCPServerRegistry,
    mcp_connector_id_var,
    mcp_session_id_var,
)
from app.mcp.notifications import (
    notify_resource_list_changed,
    broadcast_resource_list_changed,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _make_mock_session(*, fail: bool = False):
    """Create a mock MCP ServerSession with send_resource_list_changed."""
    session = MagicMock()
    if fail:
        session.send_resource_list_changed = AsyncMock(
            side_effect=Exception("connection lost")
        )
    else:
        session.send_resource_list_changed = AsyncMock()
    return session


# ── Capability declaration ───────────────────────────────────────────────────


def test_resource_list_changed_capability_declared(db):
    """create_mcp_server_for_connector() produces init options with
    resources.listChanged: true.
    """
    server = create_mcp_server_for_connector("00000000-0000-0000-0000-000000000001")
    init_options = server._mcp_server.create_initialization_options()

    capabilities = init_options.capabilities
    assert capabilities.resources is not None, (
        "resources capability should be declared"
    )
    assert capabilities.resources.listChanged is True, (
        f"resources.listChanged should be True, got {capabilities.resources.listChanged}"
    )


# ── notify_resource_list_changed ─────────────────────────────────────────────


def test_notify_resource_list_changed_calls_session(db):
    """notify_resource_list_changed calls session.send_resource_list_changed."""
    session = _make_mock_session()
    _run_async(notify_resource_list_changed(session))
    session.send_resource_list_changed.assert_awaited_once()


# ── send_message notification ────────────────────────────────────────────────


def test_send_message_sends_resource_notification(db):
    """After handle_send_message, the tool wrapper calls
    session.send_resource_list_changed via ctx.session.
    """
    from app.mcp.tools import register_mcp_tools

    mock_session = _make_mock_session()
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session

    # Create a FastMCP server and register tools
    server = create_mcp_server_for_connector("00000000-0000-0000-0000-000000000002")
    register_mcp_tools(server)

    # Get the registered send_message tool function
    tool_manager = server._tool_manager
    tool = tool_manager.get_tool("send_message")
    assert tool is not None, "send_message tool should be registered"

    # Mock handle_send_message to return immediately
    with patch("app.mcp.tools.handle_send_message", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = '{"response": "ok", "context_id": "abc"}'

        async def _run():
            conn_token = mcp_connector_id_var.set("00000000-0000-0000-0000-000000000002")
            sess_token = mcp_session_id_var.set("test-mcp-session-id")
            try:
                return await tool.fn(message="test", context_id="", ctx=mock_ctx)
            finally:
                mcp_connector_id_var.reset(conn_token)
                mcp_session_id_var.reset(sess_token)

        result = asyncio.run(_run())

    assert result == '{"response": "ok", "context_id": "abc"}'
    mock_session.send_resource_list_changed.assert_awaited_once()


def test_send_message_notification_failure_non_fatal(db):
    """If notification raises, the tool still returns the valid result."""
    from app.mcp.tools import register_mcp_tools

    mock_session = _make_mock_session(fail=True)
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session

    server = create_mcp_server_for_connector("00000000-0000-0000-0000-000000000003")
    register_mcp_tools(server)

    tool_manager = server._tool_manager
    tool = tool_manager.get_tool("send_message")

    with patch("app.mcp.tools.handle_send_message", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = '{"response": "result", "context_id": "xyz"}'

        async def _run():
            conn_token = mcp_connector_id_var.set("00000000-0000-0000-0000-000000000003")
            sess_token = mcp_session_id_var.set("test-session")
            try:
                return await tool.fn(message="test", context_id="", ctx=mock_ctx)
            finally:
                mcp_connector_id_var.reset(conn_token)
                mcp_session_id_var.reset(sess_token)

        result = asyncio.run(_run())

    # Result should still be returned despite notification failure
    assert result == '{"response": "result", "context_id": "xyz"}'
    mock_session.send_resource_list_changed.assert_awaited_once()


def test_send_message_no_notification_without_ctx(db):
    """When ctx is None, no notification is attempted."""
    from app.mcp.tools import register_mcp_tools

    server = create_mcp_server_for_connector("00000000-0000-0000-0000-000000000004")
    register_mcp_tools(server)

    tool_manager = server._tool_manager
    tool = tool_manager.get_tool("send_message")

    with patch("app.mcp.tools.handle_send_message", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = '{"response": "ok", "context_id": "abc"}'

        async def _run():
            conn_token = mcp_connector_id_var.set("00000000-0000-0000-0000-000000000004")
            try:
                # ctx defaults to None
                return await tool.fn(message="test", context_id="")
            finally:
                mcp_connector_id_var.reset(conn_token)

        result = asyncio.run(_run())

    assert result == '{"response": "ok", "context_id": "abc"}'
    # No assertion on session calls because ctx is None — just verifying no crash


# ── broadcast_resource_list_changed ──────────────────────────────────────────


def test_broadcast_with_active_sessions(db):
    """broadcast_resource_list_changed notifies all sessions for a connector."""
    session1 = _make_mock_session()
    session2 = _make_mock_session()

    with patch("app.mcp.server.mcp_registry") as mock_registry:
        mock_registry.get_sessions_for_connector.return_value = [session1, session2]
        _run_async(broadcast_resource_list_changed("connector-1"))

    session1.send_resource_list_changed.assert_awaited_once()
    session2.send_resource_list_changed.assert_awaited_once()


def test_broadcast_no_sessions(db):
    """broadcast_resource_list_changed is a no-op when there are no sessions."""
    with patch("app.mcp.server.mcp_registry") as mock_registry:
        mock_registry.get_sessions_for_connector.return_value = []
        # Should not raise
        _run_async(broadcast_resource_list_changed("connector-no-sessions"))


def test_broadcast_partial_failure(db):
    """If one session fails, other sessions are still notified."""
    session_ok = _make_mock_session()
    session_fail = _make_mock_session(fail=True)
    session_ok2 = _make_mock_session()

    with patch("app.mcp.server.mcp_registry") as mock_registry:
        mock_registry.get_sessions_for_connector.return_value = [
            session_ok, session_fail, session_ok2,
        ]
        # Should not raise despite session_fail
        _run_async(broadcast_resource_list_changed("connector-partial"))

    session_ok.send_resource_list_changed.assert_awaited_once()
    session_fail.send_resource_list_changed.assert_awaited_once()
    session_ok2.send_resource_list_changed.assert_awaited_once()


# ── Session registration and cleanup ─────────────────────────────────────────


def test_session_registration_and_retrieval(db):
    """register_session stores sessions; get_sessions_for_connector retrieves them."""
    registry = MCPServerRegistry()
    session1 = _make_mock_session()
    session2 = _make_mock_session()

    registry.register_session("conn-a", "sess-1", session1)
    registry.register_session("conn-a", "sess-2", session2)

    sessions = registry.get_sessions_for_connector("conn-a")
    assert len(sessions) == 2
    assert session1 in sessions
    assert session2 in sessions


def test_session_registration_idempotent(db):
    """Re-registering the same session ID overwrites without duplicates."""
    registry = MCPServerRegistry()
    session_old = _make_mock_session()
    session_new = _make_mock_session()

    registry.register_session("conn-a", "sess-1", session_old)
    registry.register_session("conn-a", "sess-1", session_new)

    sessions = registry.get_sessions_for_connector("conn-a")
    assert len(sessions) == 1
    assert sessions[0] is session_new


def test_get_sessions_empty_connector(db):
    """get_sessions_for_connector returns empty list for unknown connector."""
    registry = MCPServerRegistry()
    assert registry.get_sessions_for_connector("nonexistent") == []


def test_session_cleanup_on_remove(db):
    """remove() cleans up active sessions for the connector."""
    registry = MCPServerRegistry()
    session = _make_mock_session()
    registry.register_session("conn-a", "sess-1", session)

    registry.remove("conn-a")

    assert registry.get_sessions_for_connector("conn-a") == []


def test_session_cleanup_on_clear(db):
    """clear() cleans up all active sessions."""
    registry = MCPServerRegistry()
    registry.register_session("conn-a", "sess-1", _make_mock_session())
    registry.register_session("conn-b", "sess-2", _make_mock_session())

    registry.clear()

    assert registry.get_sessions_for_connector("conn-a") == []
    assert registry.get_sessions_for_connector("conn-b") == []


def test_send_message_registers_session(db):
    """send_message registers ctx.session in mcp_registry for later broadcast."""
    from app.mcp.tools import register_mcp_tools

    mock_session = _make_mock_session()
    mock_ctx = MagicMock()
    mock_ctx.session = mock_session

    server = create_mcp_server_for_connector("00000000-0000-0000-0000-000000000005")
    register_mcp_tools(server)

    tool = server._tool_manager.get_tool("send_message")

    with patch("app.mcp.tools.handle_send_message", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = '{"response": "ok", "context_id": "abc"}'
        with patch("app.mcp.server.mcp_registry") as mock_registry:
            async def _run():
                conn_token = mcp_connector_id_var.set("00000000-0000-0000-0000-000000000005")
                sess_token = mcp_session_id_var.set("transport-sess-42")
                try:
                    return await tool.fn(message="test", context_id="", ctx=mock_ctx)
                finally:
                    mcp_connector_id_var.reset(conn_token)
                    mcp_session_id_var.reset(sess_token)

            asyncio.run(_run())

    mock_registry.register_session.assert_called_once_with(
        "00000000-0000-0000-0000-000000000005",
        "transport-sess-42",
        mock_session,
    )
