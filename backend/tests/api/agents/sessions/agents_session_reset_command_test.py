"""
Integration test: /session-reset command.

Exercises the session reset command (clean slate, no recovery context):
- User sends message, agent replies
- User sends /session-reset → SDK metadata cleared, no auto-resend
- Next message starts a fresh conversation (no recovery context injected)
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages, send_message
from tests.utils.session import create_session_via_api


def test_session_reset_basic(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Basic /session-reset flow:
      1. Create agent and session
      2. Send first user message → agent replies successfully
      3. Send /session-reset command
      4. Verify: command_executed, no LLM call, "Session reset" system message,
         command metadata on agent response
    """
    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Send first message → agent replies ───────────────────
    first_user_msg = "Hello there"
    first_agent_response = "Hi! How can I help?"
    stub_success = StubAgentEnvConnector(response_text=first_agent_response)

    with patch("app.services.sessions.message_service.agent_env_connector", stub_success):
        send_message(client, superuser_token_headers, session_id, content=first_user_msg)
        drain_tasks()

    # Verify first exchange
    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) == 1
    assert first_agent_response in agent_msgs[0]["content"]

    # ── Phase 3: Send /session-reset command ──────────────────────────
    stub_noop = StubAgentEnvConnector(response_text="should not be called")

    with patch("app.services.sessions.message_service.agent_env_connector", stub_noop):
        result = send_message(
            client, superuser_token_headers, session_id, content="/session-reset",
        )
        drain_tasks()

    # Command was executed (not sent to LLM)
    assert result.get("command_executed") is True

    # No LLM call was triggered
    assert len(stub_noop.stream_calls) == 0

    # Command response is a system message with command metadata
    all_msgs = list_messages(client, superuser_token_headers, session_id)
    cmd_msgs = [
        m for m in all_msgs
        if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
    ]
    assert len(cmd_msgs) == 1
    assert "Session reset" in cmd_msgs[0]["content"]
    assert cmd_msgs[0]["message_metadata"]["command"] is True
    assert cmd_msgs[0]["message_metadata"]["command_name"] == "/session-reset"

    # "Session reset" system message exists
    all_msgs = list_messages(client, superuser_token_headers, session_id)
    reset_sys = [
        m for m in all_msgs
        if m["role"] == "system" and m["content"] == "Session reset"
    ]
    assert len(reset_sys) == 1


def test_session_reset_then_message_no_recovery_context(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    After /session-reset, the next message has no recovery context:
      1. Create agent and session
      2. Send first user message → agent replies
      3. Send /session-reset
      4. Send follow-up message
      5. Verify: no [SESSION RECOVERY] in payload, session_id is None (fresh),
         raw message sent as-is
    """
    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Send first message → agent replies ───────────────────
    first_user_msg = "Hello there"
    first_agent_response = "Hi! How can I help?"
    stub_success = StubAgentEnvConnector(response_text=first_agent_response)

    with patch("app.services.sessions.message_service.agent_env_connector", stub_success):
        send_message(client, superuser_token_headers, session_id, content=first_user_msg)
        drain_tasks()

    # ── Phase 3: Send /session-reset ──────────────────────────────────
    stub_noop = StubAgentEnvConnector(response_text="should not be called")

    with patch("app.services.sessions.message_service.agent_env_connector", stub_noop):
        result = send_message(
            client, superuser_token_headers, session_id, content="/session-reset",
        )
        drain_tasks()

    assert result.get("command_executed") is True

    # ── Phase 4: Send follow-up message → no recovery context ─────────
    followup_msg = "Continue our conversation"
    followup_response = "Sure, what would you like to discuss?"
    stub_followup = StubAgentEnvConnector(response_text=followup_response)

    with patch("app.services.sessions.message_service.agent_env_connector", stub_followup):
        send_message(client, superuser_token_headers, session_id, content=followup_msg)
        drain_tasks()

    # Verify the follow-up was sent to agent-env
    assert len(stub_followup.stream_calls) == 1
    payload = stub_followup.stream_calls[0]["payload"]

    # Fresh SDK session (no external_session_id)
    assert payload.get("session_id") is None, (
        "Reset should create a fresh SDK session (no external_session_id)"
    )

    # No recovery context injected — message sent as-is
    sent_message = payload["message"]
    assert "[SESSION RECOVERY]" not in sent_message, (
        "/session-reset should NOT inject recovery context"
    )
    assert followup_msg in sent_message


def test_session_reset_after_error_no_auto_resend(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /session-reset after an error does NOT auto-resend:
      1. Create agent and session
      2. Send first message → agent replies
      3. Send second message → agent-env returns error
      4. Send /session-reset
      5. Verify: no auto-resend (0 stream calls), "Session reset" system message,
         no "Session recovered" message
    """
    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Send first message → agent replies ───────────────────
    first_user_msg = "Hello, help me please"
    first_agent_response = "Hello! I can help you with that."
    stub_success = StubAgentEnvConnector(response_text=first_agent_response)

    with patch("app.services.sessions.message_service.agent_env_connector", stub_success):
        send_message(client, superuser_token_headers, session_id, content=first_user_msg)
        drain_tasks()

    # ── Phase 3: Send second message → agent-env returns error ────────
    second_user_msg = "Tell me more about this"
    error_events = [
        {
            "type": "error",
            "content": "SDK session not found",
            "error_type": "SessionNotFound",
        }
    ]
    stub_error = StubAgentEnvConnector(events=error_events)

    with patch("app.services.sessions.message_service.agent_env_connector", stub_error):
        send_message(client, superuser_token_headers, session_id, content=second_user_msg)
        drain_tasks()

    # Verify error state
    all_messages = list_messages(client, superuser_token_headers, session_id)
    system_errors = [
        m for m in all_messages if m["role"] == "system" and m.get("status") == "error"
    ]
    assert len(system_errors) >= 1, "Expected at least one system error message"

    # ── Phase 4: Send /session-reset ──────────────────────────────────
    stub_noop = StubAgentEnvConnector(response_text="should not be called")

    with patch("app.services.sessions.message_service.agent_env_connector", stub_noop):
        result = send_message(
            client, superuser_token_headers, session_id, content="/session-reset",
        )
        drain_tasks()

    assert result.get("command_executed") is True

    # No auto-resend triggered (0 stream calls)
    assert len(stub_noop.stream_calls) == 0, (
        "/session-reset should NOT auto-resend failed messages"
    )

    # Verify final messages
    final_messages = list_messages(client, superuser_token_headers, session_id)

    # "Session reset" system message exists
    reset_sys = [
        m for m in final_messages
        if m["role"] == "system" and m["content"] == "Session reset"
    ]
    assert len(reset_sys) == 1, "Expected exactly one 'Session reset' system message"

    # No "Session recovered" message (this is reset, not recover)
    recovered_sys = [
        m for m in final_messages
        if m["role"] == "system" and m["content"] == "Session recovered"
    ]
    assert len(recovered_sys) == 0, (
        "/session-reset should NOT create a 'Session recovered' message"
    )
