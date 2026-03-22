"""
MCP progress and content streaming notification tests.

Verifies that during agent message processing, the MCP handler:
  - Sends progress notifications (report_progress) for phase tracking
  - Streams partial content via log notifications (ctx.info)
  - Caps progress at 100 and throttles content at 0.5s intervals
  - Never crashes the tool due to notification failures
  - Handles agent error events after partial notifications
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.mcp.request_handler import MCPRequestHandler
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import create_mcp_connector


# ── Helpers ──────────────────────────────────────────────────────────────────


def _build_multi_step_events(
    steps: list[dict],
    session_id: str | None = None,
    include_done: bool = True,
) -> list[dict]:
    """Build a multi-step SSE event sequence with preamble (session_created + tools_init).

    Args:
        steps: List of event dicts to include in the sequence
        session_id: Optional external session ID
        include_done: Whether to append a done event at the end
    """
    sid = session_id or str(uuid.uuid4())
    events = [
        {"type": "session_created", "content": "", "session_id": sid, "metadata": {}},
        {
            "type": "system",
            "subtype": "tools_init",
            "content": "",
            "data": {"tools": ["bash", "read", "write"]},
            "metadata": {},
        },
    ]
    events.extend(steps)
    if include_done:
        events.append({"type": "done"})
    return events


def _setup_agent_with_connector(client, token_headers, agent_name):
    """Create agent + connector. Returns (agent, connector)."""
    agent = create_agent_via_api(client, token_headers, name=agent_name)
    drain_tasks()
    agent = get_agent(client, token_headers, agent["id"])
    connector = create_mcp_connector(
        client, token_headers, agent["id"],
        name=f"{agent_name} Connector",
    )
    return agent, connector


def _make_mock_ctx(*, fail_progress=False, fail_info=False):
    """Create a mock MCP context with report_progress and info methods."""
    ctx = MagicMock()
    # Prevent the tools layer from extracting a MagicMock as mcp_session_id
    # (which would fail when stored in the DB)
    ctx.request_context = None
    if fail_progress:
        ctx.report_progress = AsyncMock(side_effect=Exception("progress send failed"))
    else:
        ctx.report_progress = AsyncMock()
    if fail_info:
        ctx.info = AsyncMock(side_effect=Exception("info send failed"))
    else:
        ctx.info = AsyncMock()
    return ctx


def _run_send_message_with_ctx(
    connector_id: str,
    message: str,
    agent_env_stub: StubAgentEnvConnector,
    mcp_ctx=None,
    mcp_session_id: str | None = None,
    context_id: str = "",
) -> dict:
    """Call handle_send_message with an MCP context for notification testing.

    Same pattern as _run_send_message in test_mcp_send_message.py but passes
    the mock MCP context through so notifications flow to the mock.
    """
    from app.mcp.tools import handle_send_message
    from app.mcp.server import mcp_connector_id_var, mcp_session_id_var

    async def _run():
        token_conn = mcp_connector_id_var.set(connector_id)
        token_sess = mcp_session_id_var.set(mcp_session_id)
        try:
            return await handle_send_message(message, context_id=context_id, ctx=mcp_ctx)
        finally:
            mcp_connector_id_var.reset(token_conn)
            mcp_session_id_var.reset(token_sess)

    with patch("app.services.message_service.agent_env_connector", agent_env_stub):
        result = asyncio.run(_run())
    drain_tasks()

    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"response": result, "context_id": ""}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_multi_step_agent_sends_progress_and_content_notifications(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Full user story: agent processes a multi-step request with thinking,
    tool use, and multiple assistant responses. MCP client receives progress
    and content notifications throughout.

      1. Create agent + connector
      2. Build multi-step events: thinking → assistant → tool(Bash) → tool(Read) → assistant
      3. Send message with mock MCP context
      4. Verify initial progress(0, 100, "Preparing agent environment...")
      5. Verify progress notifications for thinking, assistant, and tool events
      6. Verify progress values increase monotonically and stay ≤ 100
      7. Verify phase labels identify each step (Thinking, Processing, tool names)
      8. Verify content streamed via ctx.info() for first assistant chunk
      9. Verify final response combines both assistant chunks correctly
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers, "Progress Notify Agent",
    )
    connector_id = connector["id"]

    # ── Build multi-step events ──────────────────────────────────────────
    steps = [
        {"type": "thinking", "content": "Let me analyze this...", "metadata": {}},
        {"type": "assistant", "content": "First, I'll check the files.", "metadata": {"model": "test"}},
        {"type": "tool", "name": "bash", "content": "ls -la output", "metadata": {}},
        {"type": "tool", "name": "read", "content": "file contents here", "metadata": {}},
        {"type": "assistant", "content": "Based on my analysis, here are the results.", "metadata": {"model": "test"}},
    ]
    events = _build_multi_step_events(steps)
    stub = StubAgentEnvConnector(events=events)
    mock_ctx = _make_mock_ctx()

    # ── Send message ─────────────────────────────────────────────────────
    result = _run_send_message_with_ctx(
        connector_id, "Analyze the project", stub, mcp_ctx=mock_ctx,
    )

    # ── Verify final response ────────────────────────────────────────────
    assert "error" not in result, f"Unexpected error: {result}"
    assert "First, I'll check the files." in result["response"]
    assert "Based on my analysis" in result["response"]
    assert result["context_id"], "context_id should be non-empty"

    # ── Verify progress notifications ────────────────────────────────────
    progress_calls = mock_ctx.report_progress.call_args_list

    # Expected: initial(0) + thinking(10) + assistant(20) + bash(30) + read(40) + assistant(50) = 6
    assert len(progress_calls) >= 5, (
        f"Expected at least 5 progress calls (initial + streaming events), "
        f"got {len(progress_calls)}: {progress_calls}"
    )

    # First call: initial "Preparing agent environment..."
    first_call = progress_calls[0]
    assert first_call.args[0] == 0, f"Initial progress should be 0, got {first_call.args[0]}"
    assert first_call.args[1] == 100, f"Total should be 100, got {first_call.args[1]}"
    assert "Preparing" in first_call.args[2], (
        f"Initial label should mention 'Preparing', got '{first_call.args[2]}'"
    )

    # Streaming calls: verify monotonic increase and labels
    streaming_calls = progress_calls[1:]
    progress_values = [c.args[0] for c in streaming_calls]

    for i in range(1, len(progress_values)):
        assert progress_values[i] >= progress_values[i - 1], (
            f"Progress must increase monotonically: {progress_values}"
        )
    assert all(v <= 100 for v in progress_values), (
        f"Progress must not exceed 100: {progress_values}"
    )

    # Verify all phase labels are present
    labels = [c.args[2] for c in streaming_calls]
    assert any("Thinking" in lbl for lbl in labels), f"Expected 'Thinking' label in {labels}"
    assert any("Processing" in lbl for lbl in labels), f"Expected 'Processing' label in {labels}"
    assert any("bash" in lbl for lbl in labels), f"Expected tool 'bash' in labels: {labels}"
    assert any("read" in lbl for lbl in labels), f"Expected tool 'read' in labels: {labels}"

    # ── Verify content notifications ─────────────────────────────────────
    info_calls = mock_ctx.info.call_args_list
    assert len(info_calls) >= 1, "Expected at least one info() call for content streaming"
    # First content notification should be the first assistant chunk
    assert "First, I'll check the files." in info_calls[0].args[0]


def test_progress_caps_at_100_and_content_is_throttled(db) -> None:
    """
    Direct _send_mcp_progress tests for progress capping and content throttling:

      1. Send 12 assistant events → progress increments by 10 each, caps at 100
         (events 11-12 should NOT trigger report_progress)
      2. Verify exact progress sequence: 10, 20, 30, ..., 100
      3. Send 4 assistant events with controlled timing:
         t=0s → info fires, t=0.1s → throttled, t=0.3s → throttled, t=0.6s → fires
      4. Verify only 2 info() calls (at t=0 and t=0.6)
    """
    # ── Phase 1: Progress caps at 100 ────────────────────────────────────
    mock_ctx = _make_mock_ctx()
    progress = 0
    last_info_time = 0.0
    base_time = 1000.0

    with patch("app.mcp.request_handler.time") as mock_time:
        mock_time.monotonic.return_value = base_time

        for i in range(12):
            event = {"type": "assistant", "content": f"chunk {i}", "metadata": {}}
            progress, last_info_time = asyncio.run(
                MCPRequestHandler._send_mcp_progress(mock_ctx, event, progress, last_info_time)
            )

    assert progress == 100, f"Progress should cap at 100, got {progress}"

    # report_progress called 10 times (10→100), not 12
    report_calls = mock_ctx.report_progress.call_args_list
    assert len(report_calls) == 10, (
        f"Expected exactly 10 report_progress calls (capped at 100), got {len(report_calls)}"
    )

    # Verify exact sequence: 10, 20, ..., 100
    progress_values = [c.args[0] for c in report_calls]
    assert progress_values == list(range(10, 110, 10)), (
        f"Expected [10,20,...,100], got {progress_values}"
    )

    # ── Phase 2: Content throttling at 0.5s ──────────────────────────────
    mock_ctx2 = _make_mock_ctx()
    progress2 = 0
    last_info_time2 = 0.0

    with patch("app.mcp.request_handler.time") as mock_time:
        # t=1000.0: first event (gap from 0.0 = 1000.0 ≥ 0.5 → info fires)
        mock_time.monotonic.return_value = 1000.0
        progress2, last_info_time2 = asyncio.run(
            MCPRequestHandler._send_mcp_progress(
                mock_ctx2,
                {"type": "assistant", "content": "chunk A", "metadata": {}},
                progress2, last_info_time2,
            )
        )

        # t=1000.1: gap = 0.1 < 0.5 → throttled
        mock_time.monotonic.return_value = 1000.1
        progress2, last_info_time2 = asyncio.run(
            MCPRequestHandler._send_mcp_progress(
                mock_ctx2,
                {"type": "assistant", "content": "chunk B", "metadata": {}},
                progress2, last_info_time2,
            )
        )

        # t=1000.3: gap = 0.3 < 0.5 → still throttled
        mock_time.monotonic.return_value = 1000.3
        progress2, last_info_time2 = asyncio.run(
            MCPRequestHandler._send_mcp_progress(
                mock_ctx2,
                {"type": "assistant", "content": "chunk C", "metadata": {}},
                progress2, last_info_time2,
            )
        )

        # t=1000.6: gap from last info (1000.0) = 0.6 ≥ 0.5 → info fires
        mock_time.monotonic.return_value = 1000.6
        progress2, last_info_time2 = asyncio.run(
            MCPRequestHandler._send_mcp_progress(
                mock_ctx2,
                {"type": "assistant", "content": "chunk D", "metadata": {}},
                progress2, last_info_time2,
            )
        )

    info_calls = mock_ctx2.info.call_args_list
    assert len(info_calls) == 2, (
        f"Expected exactly 2 info() calls (A at t=0, D at t=0.6), got {len(info_calls)}: "
        f"{[c.args[0] for c in info_calls]}"
    )
    assert info_calls[0].args[0] == "chunk A"
    assert info_calls[1].args[0] == "chunk D"


def test_notification_failures_and_error_resilience(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Notification failures never crash the tool; error events handled correctly:

      1. Both report_progress and info raise exceptions → tool still returns
         valid response with full content and context_id
      2. Agent sends assistant → tool → error event → notifications sent for
         early events, error JSON returned with context_id
      3. No ctx provided (backward compat) → no crash, correct response
    """
    # ── Phase 1: All notification methods fail, tool still returns ────────
    agent1, connector1 = _setup_agent_with_connector(
        client, superuser_token_headers, "Failing Notifications Agent",
    )

    steps = [
        {"type": "thinking", "content": "thinking...", "metadata": {}},
        {"type": "assistant", "content": "Response despite failures.", "metadata": {"model": "test"}},
    ]
    stub1 = StubAgentEnvConnector(events=_build_multi_step_events(steps))
    failing_ctx = _make_mock_ctx(fail_progress=True, fail_info=True)

    result1 = _run_send_message_with_ctx(
        connector1["id"], "Hello", stub1, mcp_ctx=failing_ctx,
    )

    assert "error" not in result1, (
        f"Tool should succeed despite notification failures: {result1}"
    )
    assert "Response despite failures." in result1["response"]
    assert result1["context_id"], "context_id should be present"

    # Verify the failing methods were attempted (they raised but didn't crash the tool)
    assert failing_ctx.report_progress.await_count >= 1, (
        "report_progress should have been attempted"
    )
    assert failing_ctx.info.await_count >= 1, (
        "info should have been attempted"
    )

    # ── Phase 2: Agent error event after partial processing ──────────────
    agent2, connector2 = _setup_agent_with_connector(
        client, superuser_token_headers, "Error Event Agent",
    )

    error_steps = [
        {"type": "assistant", "content": "Starting analysis...", "metadata": {"model": "test"}},
        {"type": "tool", "name": "bash", "content": "running command", "metadata": {}},
        {"type": "error", "content": "Agent crashed: out of memory"},
    ]
    stub2 = StubAgentEnvConnector(
        events=_build_multi_step_events(error_steps, include_done=False),
    )
    error_ctx = _make_mock_ctx()

    result2 = _run_send_message_with_ctx(
        connector2["id"], "Run heavy computation", stub2, mcp_ctx=error_ctx,
    )

    assert "error" in result2, f"Expected error in result: {result2}"
    assert "out of memory" in result2["error"]
    assert result2["context_id"], "context_id should be present even on error"

    # Notifications should have been sent for events before the error
    progress_calls = error_ctx.report_progress.call_args_list
    # Expected: initial "Preparing..." + assistant + tool = 3
    assert len(progress_calls) >= 2, (
        f"Expected at least 2 progress calls before error, got {len(progress_calls)}"
    )

    labels = [c.args[2] for c in progress_calls]
    assert any("Preparing" in lbl for lbl in labels), (
        f"Expected initial 'Preparing' notification: {labels}"
    )

    # At least one info() call for the assistant chunk before the error
    info_calls = error_ctx.info.call_args_list
    assert len(info_calls) >= 1, "Expected info() for assistant chunk before error"
    assert "Starting analysis" in info_calls[0].args[0]

    # ── Phase 3: No ctx → no crash, correct response ────────────────────
    agent3, connector3 = _setup_agent_with_connector(
        client, superuser_token_headers, "No Context Agent",
    )

    stub3 = StubAgentEnvConnector(response_text="Works without ctx")
    result3 = _run_send_message_with_ctx(
        connector3["id"], "Hello", stub3, mcp_ctx=None,
    )

    assert "error" not in result3, f"Should work without ctx: {result3}"
    assert "Works without ctx" in result3["response"]
    assert result3["context_id"], "context_id should be present"
