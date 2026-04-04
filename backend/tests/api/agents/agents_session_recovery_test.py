"""
Integration test: session recovery after system error.

Exercises the full session recovery flow through FastAPI TestClient:
- User sends message, agent replies
- User sends another message, agent-env returns error
- User triggers session recovery via the recover endpoint OR /session-recover command
- Backend re-sends the failed message with conversation history injected
- Agent-env receives a fresh session request with recovery context
- A "Session recovered" system message is added to the chat
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages, send_message
from tests.utils.session import create_session_via_api


def test_session_recovery_auto_resend(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full session recovery scenario with auto-resend:
      1. Create agent and session
      2. Send first user message → agent replies successfully
      3. Send second user message → agent-env returns error
      4. Verify system error message appears in chat
      5. Call POST /sessions/{id}/recover
      6. Verify: failed message re-sent with recovery context to agent-env
      7. Verify: "Session recovered" system message, agent responds after recovery
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

    with patch("app.services.message_service.agent_env_connector", stub_success):
        send_message(client, superuser_token_headers, session_id, content=first_user_msg)
        drain_tasks()

    # Verify first exchange
    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) == 1
    assert first_agent_response in agent_msgs[0]["content"]

    user_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "user")
    assert len(user_msgs) == 1

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

    with patch("app.services.message_service.agent_env_connector", stub_error):
        send_message(client, superuser_token_headers, session_id, content=second_user_msg)
        drain_tasks()

    # ── Phase 4: Verify error state ──────────────────────────────────
    all_messages = list_messages(client, superuser_token_headers, session_id)
    system_errors = [
        m for m in all_messages if m["role"] == "system" and m.get("status") == "error"
    ]
    assert len(system_errors) >= 1, "Expected at least one system error message"
    assert "SDK session not found" in system_errors[0]["content"]

    user_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "user")
    assert len(user_msgs) == 2

    # ── Phase 5: Recover session ──────────────────────────────────────
    recovery_agent_response = "Of course! Continuing from where we left off."
    stub_recovery = StubAgentEnvConnector(response_text=recovery_agent_response)

    with patch("app.services.message_service.agent_env_connector", stub_recovery):
        r = client.post(
            f"{settings.API_V1_STR}/sessions/{session_id}/recover",
            headers=superuser_token_headers,
        )
        assert r.status_code == 200
        body = r.json()
        # Auto-resend detected: response mentions resending
        assert "Resending" in body["message"]

        drain_tasks()

    # ── Phase 6: Verify recovery context sent to agent-env ────────────
    assert len(stub_recovery.stream_calls) == 1, (
        "Expected exactly one stream call for recovery"
    )
    recovery_payload = stub_recovery.stream_calls[0]["payload"]

    # Fresh SDK session: no external_session_id
    assert recovery_payload.get("session_id") is None, (
        "Recovery should create a fresh SDK session (no external_session_id)"
    )

    # Recovery context is prepended to the message
    sent_message = recovery_payload["message"]
    assert "[SESSION RECOVERY]" in sent_message
    assert "[END SESSION RECOVERY]" in sent_message
    assert "Previous conversation history:" in sent_message

    # Conversation history includes both user and agent messages
    assert f"User: {first_user_msg}" in sent_message
    assert f"Assistant: {first_agent_response}" in sent_message
    assert f"User: {second_user_msg}" in sent_message

    # The actual user message follows the recovery context
    after_recovery = sent_message.split("[END SESSION RECOVERY]")[1]
    assert second_user_msg in after_recovery

    # ── Phase 7: Verify final message state ───────────────────────────
    final_messages = list_messages(client, superuser_token_headers, session_id)

    # "Session recovered" system message exists
    system_msgs = [m for m in final_messages if m["role"] == "system"]
    recovery_sys_msgs = [
        m for m in system_msgs if m["content"] == "Session recovered"
    ]
    assert len(recovery_sys_msgs) == 1, (
        "Expected exactly one 'Session recovered' system message"
    )

    # Agent replied after recovery (two agent messages total)
    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) == 2, "Expected two agent messages (original + recovery)"
    assert recovery_agent_response in agent_msgs[1]["content"]


def test_session_recovery_via_command(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Session recovery via /session-recover command (A2A-friendly):
      1. Create agent and session
      2. Send first user message → agent replies successfully
      3. Send second user message → agent-env returns error
      4. Verify system error message appears in chat
      5. Send /session-recover command
      6. Verify: command_executed response returned
      7. Verify: failed message re-sent with recovery context to agent-env
      8. Verify: "Session recovered" system message, agent responds after recovery
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

    with patch("app.services.message_service.agent_env_connector", stub_success):
        send_message(client, superuser_token_headers, session_id, content=first_user_msg)
        drain_tasks()

    # Verify first exchange
    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) == 1
    assert first_agent_response in agent_msgs[0]["content"]

    user_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "user")
    assert len(user_msgs) == 1

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

    with patch("app.services.message_service.agent_env_connector", stub_error):
        send_message(client, superuser_token_headers, session_id, content=second_user_msg)
        drain_tasks()

    # ── Phase 4: Verify error state ──────────────────────────────────
    all_messages = list_messages(client, superuser_token_headers, session_id)
    system_errors = [
        m for m in all_messages if m["role"] == "system" and m.get("status") == "error"
    ]
    assert len(system_errors) >= 1, "Expected at least one system error message"
    assert "SDK session not found" in system_errors[0]["content"]

    user_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "user")
    assert len(user_msgs) == 2

    # ── Phase 5: Send /session-recover command ─────────────────────────
    recovery_agent_response = "Of course! Continuing from where we left off."
    stub_recovery = StubAgentEnvConnector(response_text=recovery_agent_response)

    with patch("app.services.message_service.agent_env_connector", stub_recovery):
        result = send_message(
            client, superuser_token_headers, session_id, content="/session-recover",
        )
        drain_tasks()

    # Command was executed (not sent to LLM)
    assert result.get("command_executed") is True

    # ── Phase 6: Verify recovery context sent to agent-env ────────────
    assert len(stub_recovery.stream_calls) == 1, (
        "Expected exactly one stream call for recovery resend"
    )
    recovery_payload = stub_recovery.stream_calls[0]["payload"]

    # Fresh SDK session: no external_session_id
    assert recovery_payload.get("session_id") is None, (
        "Recovery should create a fresh SDK session (no external_session_id)"
    )

    # Recovery context is prepended to the message
    sent_message = recovery_payload["message"]
    assert "[SESSION RECOVERY]" in sent_message
    assert "[END SESSION RECOVERY]" in sent_message
    assert "Previous conversation history:" in sent_message

    # Conversation history includes both user and agent messages
    assert f"User: {first_user_msg}" in sent_message
    assert f"Assistant: {first_agent_response}" in sent_message
    assert f"User: {second_user_msg}" in sent_message

    # The actual user message follows the recovery context
    after_recovery = sent_message.split("[END SESSION RECOVERY]")[1]
    assert second_user_msg in after_recovery

    # ── Phase 7: Verify final message state ───────────────────────────
    final_messages = list_messages(client, superuser_token_headers, session_id)

    # "Session recovered" system message exists
    system_msgs = [m for m in final_messages if m["role"] == "system"]
    recovery_sys_msgs = [
        m for m in system_msgs if m["content"] == "Session recovered"
    ]
    assert len(recovery_sys_msgs) == 1, (
        "Expected exactly one 'Session recovered' system message"
    )

    # Command response is a system message with command metadata
    all_final = list_messages(client, superuser_token_headers, session_id)
    cmd_msgs = [
        m for m in all_final
        if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
    ]
    assert len(cmd_msgs) == 1
    assert "Session recovered" in cmd_msgs[0]["content"]
    assert cmd_msgs[0]["message_metadata"]["command_name"] == "/session-recover"

    # Agent messages: original reply + recovery reply (command response is system now)
    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) == 2, (
        f"Expected 2 agent messages (original + recovery), got {len(agent_msgs)}"
    )
    assert recovery_agent_response in agent_msgs[1]["content"]

    # User messages: original + failed + command
    user_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "user")
    assert len(user_msgs) == 3
    assert user_msgs[2]["content"] == "/session-recover"


def test_session_recover_command_no_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /session-recover when there's no system error — no auto-resend,
    but session is still marked for recovery.
      1. Create agent and session
      2. Send first message → agent replies normally
      3. Send /session-recover (no prior error)
      4. Verify: command_executed, no LLM call triggered, recovery message
      5. Send a follow-up message → recovery context injected
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

    with patch("app.services.message_service.agent_env_connector", stub_success):
        send_message(client, superuser_token_headers, session_id, content=first_user_msg)
        drain_tasks()

    # ── Phase 3: Send /session-recover without prior error ────────────
    # No LLM stub needed since no resendable message exists — no stream triggered
    stub_noop = StubAgentEnvConnector(response_text="should not be called")

    with patch("app.services.message_service.agent_env_connector", stub_noop):
        result = send_message(
            client, superuser_token_headers, session_id, content="/session-recover",
        )
        drain_tasks()

    assert result.get("command_executed") is True

    # No LLM call was triggered (no resendable message)
    assert len(stub_noop.stream_calls) == 0

    # Command response is a system message with command metadata
    all_msgs = list_messages(client, superuser_token_headers, session_id)
    cmd_msgs = [
        m for m in all_msgs
        if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
    ]
    assert len(cmd_msgs) == 1
    assert "Send a new message" in cmd_msgs[0]["content"]
    assert cmd_msgs[0]["message_metadata"]["command"] is True

    # "Session recovered" system message exists
    all_msgs = list_messages(client, superuser_token_headers, session_id)
    recovery_sys = [m for m in all_msgs if m["role"] == "system" and m["content"] == "Session recovered"]
    assert len(recovery_sys) == 1

    # ── Phase 4: Send follow-up → recovery context injected ───────────
    followup_msg = "Continue our conversation"
    followup_response = "Sure, picking up where we left off."
    stub_followup = StubAgentEnvConnector(response_text=followup_response)

    with patch("app.services.message_service.agent_env_connector", stub_followup):
        send_message(client, superuser_token_headers, session_id, content=followup_msg)
        drain_tasks()

    # Recovery context was injected into the follow-up message
    assert len(stub_followup.stream_calls) == 1
    sent = stub_followup.stream_calls[0]["payload"]["message"]
    assert "[SESSION RECOVERY]" in sent
    assert f"User: {first_user_msg}" in sent
    assert f"Assistant: {first_agent_response}" in sent
    assert followup_msg in sent

    # Fresh SDK session (no external_session_id)
    assert stub_followup.stream_calls[0]["payload"].get("session_id") is None
