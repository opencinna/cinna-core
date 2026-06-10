"""
Integration tests: Non-LLM to LLM context bridging (Plan #4).

Tests cover:
  1. /files then LLM message — prior_commands block is prepended to LLM content
  2. /session-recover then LLM message — opted-out handler; no prior_commands block
  3. /webapp then LLM message — opted-out handler; no prior_commands block
  4. forwarded_to_llm_at gate: second LLM message does NOT re-include same command output
  5. New command pair after first LLM turn IS included in the next prior_commands block
  6. Per-block size cap: output > 16 KB is truncated with [output truncated] marker
  7. Total budget cap: when total exceeds 64 KB, the block that would push it over is dropped
  8. No eligible commands → no prior_commands block, no error
  9. /agent-status output appears in prior_commands block (opted-in handler)

Notes:
  - StubAgentEnvConnector captures stream_chat calls; payload["message"] contains the
    final user message content sent to the agent-env, including any <prior_commands> block.
  - The tests use drain_tasks() to process background streaming.
  - environment_adapter is auto-patched by conftest.py.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.services.sessions.message_service import (
    NON_LLM_BRIDGE_MAX_PER_BLOCK_BYTES,
    NON_LLM_BRIDGE_TRUNCATION_MARKER,
)
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api



def _llm_user_message_content(stub: StubAgentEnvConnector, call_index: int = 0) -> str:
    """Return the user message content sent to agent-env for the given stream_chat call index."""
    assert call_index < len(stub.stream_calls), (
        f"Expected at least {call_index + 1} stream_chat call(s), got {len(stub.stream_calls)}"
    )
    return stub.stream_calls[call_index]["payload"]["message"]


def test_files_command_included_in_prior_commands_block(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Scenario: /files then regular LLM message.

    The /files command output should appear in a <prior_commands> block
    prepended to the next LLM message content.

    Flow:
      1. Create agent + session
      2. Send /files → synchronous command response (no LLM call)
      3. Send regular message → LLM call fires
      4. Verify the LLM received <prior_commands>...</prior_commands> block
         containing the /files invocation and output
    """
    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    stub = StubAgentEnvConnector(response_text="I see the files.")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # ── Phase 2: Send /files ────────────────────────────────────────
        result = send_message(
            client, superuser_token_headers, session_id, content="/files",
        )
        drain_tasks()
        assert result.get("command_executed") is True
        assert len(stub.stream_calls) == 0, "/files must not call LLM"

        # ── Phase 3: Send regular message → LLM fires ──────────────────
        send_message(
            client, superuser_token_headers, session_id,
            content="What files are available?",
        )
        drain_tasks()

    # ── Phase 4: Verify <prior_commands> block in LLM payload ──────────
    assert len(stub.stream_calls) == 1, "Expected exactly 1 LLM call"

    user_content = _llm_user_message_content(stub, call_index=0)

    assert "<prior_commands>" in user_content, (
        f"Expected <prior_commands> block in LLM message, got:\n{user_content[:500]}"
    )
    assert "</prior_commands>" in user_content, "Expected closing </prior_commands> tag"
    assert 'name="/files"' in user_content, (
        f"Expected /files command name in block, got:\n{user_content[:500]}"
    )
    assert "<invocation>/files</invocation>" in user_content, (
        "Expected /files invocation tag in block"
    )
    # The block must come before the user's message
    prior_idx = user_content.index("<prior_commands>")
    question_idx = user_content.index("What files are available?")
    assert prior_idx < question_idx, (
        "<prior_commands> block must appear before user message content"
    )


def test_session_recover_excluded_from_prior_commands_block(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Scenario: /session-recover then regular LLM message.

    SessionRecoverCommandHandler has include_in_llm_context=False, so
    no <prior_commands> block should be prepended to the LLM message.

    Flow:
      1. Create agent + session
      2. Send /session-recover → synchronous command response
      3. Send regular message → LLM call fires
      4. Verify LLM message does NOT contain <prior_commands> block
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    stub = StubAgentEnvConnector(response_text="Recovered.")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_id, content="/session-recover",
        )
        drain_tasks()

        send_message(
            client, superuser_token_headers, session_id,
            content="Continue the conversation.",
        )
        drain_tasks()

    assert len(stub.stream_calls) == 1, "Expected 1 LLM call"

    user_content = _llm_user_message_content(stub, call_index=0)

    assert "<prior_commands>" not in user_content, (
        f"Expected NO <prior_commands> block for /session-recover, got:\n{user_content[:500]}"
    )


def test_webapp_command_excluded_from_prior_commands_block(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Scenario: /webapp then regular LLM message.

    WebappCommandHandler has include_in_llm_context=False, so
    no <prior_commands> block should appear in the next LLM message.

    Flow:
      1. Create agent + session
      2. Send /webapp → synchronous command response (just a URL message)
      3. Send regular message → LLM call fires
      4. Verify LLM message does NOT contain <prior_commands> block
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    stub = StubAgentEnvConnector(response_text="Here you go.")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_id, content="/webapp",
        )
        drain_tasks()

        send_message(
            client, superuser_token_headers, session_id,
            content="What is the webapp URL?",
        )
        drain_tasks()

    assert len(stub.stream_calls) == 1

    user_content = _llm_user_message_content(stub, call_index=0)

    assert "<prior_commands>" not in user_content, (
        f"Expected NO <prior_commands> block for /webapp, got:\n{user_content[:500]}"
    )


def test_forwarded_to_llm_at_gate_prevents_re_inclusion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Scenario: /files then two consecutive LLM messages.

    The first LLM turn should include <prior_commands> and mark the command output.
    The second LLM turn should NOT re-include the same command output.

    Flow:
      1. Create agent + session
      2. Send /files → command response
      3. Send LLM message #1 → receives <prior_commands>
      4. Send LLM message #2 → NO <prior_commands> (already forwarded)
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    stub = StubAgentEnvConnector(response_text="Understood.")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # Step 1: /files command
        send_message(
            client, superuser_token_headers, session_id, content="/files",
        )
        drain_tasks()

        # Step 2: First LLM message
        send_message(
            client, superuser_token_headers, session_id,
            content="Tell me about the files.",
        )
        drain_tasks()

        # Step 3: Second LLM message
        send_message(
            client, superuser_token_headers, session_id,
            content="Now tell me something else.",
        )
        drain_tasks()

    assert len(stub.stream_calls) == 2, "Expected 2 LLM calls"

    # First LLM call should have prior_commands
    first_content = _llm_user_message_content(stub, call_index=0)
    assert "<prior_commands>" in first_content, (
        f"Expected <prior_commands> in first LLM call, got:\n{first_content[:500]}"
    )

    # Second LLM call should NOT have prior_commands (already forwarded)
    second_content = _llm_user_message_content(stub, call_index=1)
    assert "<prior_commands>" not in second_content, (
        f"Expected NO <prior_commands> in second LLM call (already forwarded), "
        f"got:\n{second_content[:500]}"
    )


def test_new_command_after_first_llm_turn_is_included(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Scenario: /files before LLM turn #1, /files-all before LLM turn #2.

    LLM turn #1 gets <prior_commands> with /files output.
    LLM turn #2 gets <prior_commands> with /files-all output (new since turn #1).
    Turn #2 does NOT re-include /files (already forwarded).

    Flow:
      1. Create agent + session
      2. Send /files → command response
      3. Send LLM message #1 → <prior_commands> with /files
      4. Send /files-all → new command response
      5. Send LLM message #2 → <prior_commands> with /files-all only
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    stub = StubAgentEnvConnector(response_text="Got it.")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # Before LLM turn #1: /files command
        send_message(client, superuser_token_headers, session_id, content="/files")
        drain_tasks()

        # LLM turn #1
        send_message(
            client, superuser_token_headers, session_id,
            content="First question about files.",
        )
        drain_tasks()

        # Before LLM turn #2: /files-all command
        send_message(client, superuser_token_headers, session_id, content="/files-all")
        drain_tasks()

        # LLM turn #2
        send_message(
            client, superuser_token_headers, session_id,
            content="Second question about all files.",
        )
        drain_tasks()

    assert len(stub.stream_calls) == 2, "Expected 2 LLM calls"

    first_content = _llm_user_message_content(stub, call_index=0)
    second_content = _llm_user_message_content(stub, call_index=1)

    # LLM turn #1: has /files
    assert "<prior_commands>" in first_content
    assert 'name="/files"' in first_content

    # LLM turn #2: has /files-all, does NOT have /files again
    assert "<prior_commands>" in second_content, (
        f"Expected <prior_commands> in second LLM call, got:\n{second_content[:500]}"
    )
    assert 'name="/files-all"' in second_content, (
        f"Expected /files-all in second LLM call, got:\n{second_content[:500]}"
    )
    # /files was already forwarded in turn #1, so it must NOT appear again
    assert second_content.count('name="/files"') == 0, (
        "Expected /files NOT to be re-included in second LLM turn (already forwarded)"
    )


def test_no_eligible_commands_produces_no_prior_commands_block(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Scenario: No command invocations before the first LLM message.

    No <prior_commands> block should be prepended — the LLM receives the
    raw user message without any prefix.

    Flow:
      1. Create agent + session
      2. Send regular LLM message immediately (no commands first)
      3. Verify LLM message has NO <prior_commands> block
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    stub = StubAgentEnvConnector(response_text="Hello!")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_id,
            content="Hello, no prior commands.",
        )
        drain_tasks()

    assert len(stub.stream_calls) == 1

    user_content = _llm_user_message_content(stub, call_index=0)

    assert "<prior_commands>" not in user_content, (
        f"Expected NO <prior_commands> when no commands were sent, got:\n{user_content[:500]}"
    )
    assert "Hello, no prior commands." in user_content


def test_per_block_size_cap_truncates_large_output(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
) -> None:
    """
    Scenario: Command produces output larger than NON_LLM_BRIDGE_MAX_PER_BLOCK_BYTES (16 KB).

    The <output> block should be truncated and the [output truncated] marker appended.

    Flow:
      1. Create agent + session + /files command
      2. Manually override the system message content with oversized output
      3. Send LLM message → <prior_commands> block is built
      4. Verify truncation marker appears in LLM payload
    """
    from app.models.sessions.session import SessionMessage
    from sqlmodel import select

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    stub = StubAgentEnvConnector(response_text="Done.")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        # Send /files to create command message pair
        send_message(client, superuser_token_headers, session_id, content="/files")
        drain_tasks()

    # Manually override the /files system message content with oversized output
    # (20 KB of data, exceeds the 16 KB per-block limit)
    oversized_content = "x" * (NON_LLM_BRIDGE_MAX_PER_BLOCK_BYTES + 4096)

    stmt = (
        select(SessionMessage)
        .where(
            SessionMessage.session_id == uuid.UUID(session_id),
            SessionMessage.role == "system",
        )
        .order_by(SessionMessage.sequence_number.desc())
        .limit(1)
    )
    sys_msg = db.exec(stmt).first()
    assert sys_msg is not None, "Expected a /files system message to exist"

    sys_msg.content = oversized_content
    db.add(sys_msg)
    db.commit()

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_id,
            content="Show me the files.",
        )
        drain_tasks()

    assert len(stub.stream_calls) == 1, "Expected 1 LLM call"

    user_content = _llm_user_message_content(stub, call_index=0)

    assert "<prior_commands>" in user_content
    assert NON_LLM_BRIDGE_TRUNCATION_MARKER.strip() in user_content, (
        f"Expected truncation marker '{NON_LLM_BRIDGE_TRUNCATION_MARKER.strip()}' in LLM content"
    )

    # The output should be shorter than the oversized input
    assert oversized_content not in user_content, "Oversized content should have been truncated"


# Unit test for the include_in_llm_context handler class attributes lives in
# tests/unit/test_command_handler_attributes.py (pure attribute introspection).


def test_get_handler_returns_correct_handler_or_none() -> None:
    """
    Unit-level check: CommandService.get_handler returns the registered handler
    for known command names and None for unknown names.
    """
    # Importing the commands package registers all handlers (its module-level
    # side effects call CommandService.register). This is the same import the
    # service performs lazily before listing handlers.
    import app.services.agents.commands  # noqa: F401
    from app.services.agents.command_service import CommandService
    from app.services.agents.commands.files_command import FilesCommandHandler

    # /files must resolve to the registered FilesCommandHandler.
    handler = CommandService.get_handler("/files")
    assert isinstance(handler, FilesCommandHandler), (
        f"Expected /files to resolve to FilesCommandHandler, got {handler!r}"
    )

    # An unknown command name returns None.
    result_none = CommandService.get_handler("/definitely-not-a-real-command-xyz")
    assert result_none is None, "Expected None for unknown command name"
