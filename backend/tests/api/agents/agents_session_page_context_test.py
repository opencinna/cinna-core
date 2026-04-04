"""
Integration tests: Regular Sessions — page_context forwarding.

Covers the page_context pipeline for authenticated user sessions via the
standard /api/v1/sessions/{id}/messages/stream endpoint (not the webapp
chat route). This is the path used when dashboard block prompt actions
navigate to a session page and include page_context from a webapp iframe.

Business rules tested:
  1. page_context present → stored in message_metadata["page_context"];
     agent-env receives <page_context> block; message.content is clean text
  2. page_context absent → message.content is clean, no context block sent
  3. page_context over 10,000 chars → truncated to 10,000 before storage;
     agent-env receives the truncated block
  4. Second message with identical page_context → no context block sent
     (context diff optimization: identical context is omitted)
  5. Second message with changed page_context → <context_update> diff block
     sent to agent-env instead of a full <page_context> block
"""
import json
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR
_PAGE_CONTEXT_MAX_CHARS = 10_000


# ── Helpers ───────────────────────────────────────────────────────────────


def _send_message_with_context(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    content: str,
    page_context: str | None,
    stub_agent_env: StubAgentEnvConnector,
) -> dict:
    """
    Send a message to the regular session stream endpoint with optional
    page_context. Patches agent_env_connector and drains background tasks.
    Returns the parsed JSON response.
    """
    patch_target = "app.services.sessions.message_service.agent_env_connector"
    with patch(patch_target, stub_agent_env):
        payload: dict = {"content": content, "file_ids": []}
        if page_context is not None:
            payload["page_context"] = page_context

        r = client.post(
            f"{API}/sessions/{session_id}/messages/stream",
            headers=headers,
            json=payload,
        )
        assert r.status_code == 200, f"POST stream failed: {r.text}"
        drain_tasks()
    return r.json()


def _get_user_messages(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
) -> list[dict]:
    """Return all user-role messages for a session via the API."""
    r = client.get(
        f"{API}/sessions/{session_id}/messages",
        headers=headers,
    )
    assert r.status_code == 200, f"GET messages failed: {r.text}"
    return [m for m in r.json()["data"] if m["role"] == "user"]


def _setup_agent_and_session(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[str, str]:
    """
    Create an agent (with environment) and a conversation session.
    Returns (agent_id, session_id).
    """
    agent = create_agent_via_api(client, headers, name="Page Context Test Agent")
    drain_tasks()
    # Re-fetch to get active_environment_id
    r = client.get(f"{API}/agents/{agent['id']}", headers=headers)
    agent = r.json()
    agent_id = agent["id"]

    session = create_session_via_api(
        client, headers, agent_id, mode="conversation"
    )
    return agent_id, session["id"]


# ── Test 1: page_context present — full pipeline ───────────────────────────


def test_session_message_with_page_context_stored_and_injected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When page_context is included in a regular session message:
      1. Create agent + session
      2. Send message with page_context
      3. message.content is clean user text (no XML)
      4. message_metadata["page_context"] stores the context string
      5. Agent-env receives <page_context> block injected into the message
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)

    user_text = "What does this data show?"
    page_context = json.dumps({
        "page": {"url": "https://example.com/dashboard", "title": "Sales Dashboard"},
        "microdata": [{"type": "https://schema.org/QuantitativeValue", "properties": {"value": "1.2M"}}],
    })
    stub = StubAgentEnvConnector(response_text="Here is the analysis.")

    # ── Phase 2: Send message with page_context ───────────────────────────
    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content=user_text, page_context=page_context, stub_agent_env=stub,
    )

    # ── Phase 3: message.content is clean (no XML) ────────────────────────
    user_msgs = _get_user_messages(client, superuser_token_headers, session_id)
    assert len(user_msgs) == 1
    msg = user_msgs[0]
    assert msg["content"] == user_text, (
        f"Expected clean content, got: {msg['content']!r}"
    )
    assert "<page_context>" not in msg["content"]

    # ── Phase 4: page_context stored in message_metadata ─────────────────
    assert msg.get("message_metadata") is not None
    assert msg["message_metadata"].get("page_context") == page_context, (
        "page_context should be stored in message_metadata"
    )

    # ── Phase 5: Agent-env received <page_context> block ─────────────────
    assert len(stub.stream_calls) == 1, "Expected exactly one stream call to agent-env"
    agent_payload_message = stub.stream_calls[0]["payload"]["message"]
    assert "<page_context>" in agent_payload_message, (
        f"Expected <page_context> block in agent payload, got: {agent_payload_message!r}"
    )
    assert page_context in agent_payload_message, (
        "Full page_context JSON should appear in the agent payload"
    )


# ── Test 2: page_context absent — no injection ────────────────────────────


def test_session_message_without_page_context_no_injection(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When page_context is absent, the pipeline behaves normally:
      1. message.content is the user's text
      2. Agent-env receives no <page_context> block
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)

    user_text = "Tell me about the agent."
    stub = StubAgentEnvConnector(response_text="Sure, here you go.")

    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content=user_text, page_context=None, stub_agent_env=stub,
    )

    user_msgs = _get_user_messages(client, superuser_token_headers, session_id)
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == user_text
    assert "<page_context>" not in user_msgs[0]["content"]

    assert len(stub.stream_calls) == 1
    assert "<page_context>" not in stub.stream_calls[0]["payload"]["message"], (
        "Agent-env should receive no <page_context> block when page_context is absent"
    )


# ── Test 3: page_context truncated at 10,000 chars ────────────────────────


def test_session_message_page_context_truncated(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    page_context over 10,000 chars is truncated before storage and injection.
    The message.content remains clean.
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)

    user_text = "What does the data say?"
    # Build a context string longer than the limit
    oversized_context = "x" * (_PAGE_CONTEXT_MAX_CHARS + 1_000)
    expected_truncated = oversized_context[:_PAGE_CONTEXT_MAX_CHARS]
    stub = StubAgentEnvConnector(response_text="Analysis complete.")

    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content=user_text, page_context=oversized_context, stub_agent_env=stub,
    )

    user_msgs = _get_user_messages(client, superuser_token_headers, session_id)
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == user_text
    # Stored page_context should be truncated
    stored_context = user_msgs[0]["message_metadata"].get("page_context", "")
    assert len(stored_context) == _PAGE_CONTEXT_MAX_CHARS, (
        f"Stored page_context length is {len(stored_context)}, expected {_PAGE_CONTEXT_MAX_CHARS}"
    )
    assert stored_context == expected_truncated


# ── Test 4: Context diff — identical context omitted ──────────────────────


def test_session_message_identical_context_omitted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When two consecutive messages carry identical page_context, the second
    message's agent-env payload omits the context block entirely.
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)

    page_context = json.dumps({
        "page": {"url": "https://example.com/dash", "title": "Dashboard"},
        "microdata": [{"type": "https://schema.org/DataFeedItem", "properties": {"value": "42"}}],
    })

    stub1 = StubAgentEnvConnector(response_text="First answer.")
    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content="First question", page_context=page_context, stub_agent_env=stub1,
    )

    stub2 = StubAgentEnvConnector(response_text="Second answer.")
    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content="Follow-up question", page_context=page_context, stub_agent_env=stub2,
    )

    # First message → full <page_context> block
    assert "<page_context>" in stub1.stream_calls[0]["payload"]["message"], (
        "First message should include full <page_context> block"
    )
    # Second message → context omitted (identical)
    assert "<page_context>" not in stub2.stream_calls[0]["payload"]["message"], (
        "Second message with identical context should have no <page_context> block"
    )
    assert "<context_update>" not in stub2.stream_calls[0]["payload"]["message"], (
        "Second message with identical context should have no <context_update> block"
    )


# ── Test 5: Context diff — changed context sends diff block ───────────────


def test_session_message_changed_context_sends_diff(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When a subsequent message has a changed page_context, the agent-env
    payload contains a <context_update> diff block, not a full <page_context>.
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)

    context_v1 = json.dumps({
        "page": {"url": "https://example.com/dash", "title": "Q3 Dashboard"},
        "microdata": [{"type": "https://schema.org/QuantitativeValue", "properties": {"value": "1.0M"}}],
    })
    context_v2 = json.dumps({
        "page": {"url": "https://example.com/dash", "title": "Q4 Dashboard"},
        "microdata": [{"type": "https://schema.org/QuantitativeValue", "properties": {"value": "1.5M"}}],
    })

    stub1 = StubAgentEnvConnector(response_text="Q3 answer.")
    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content="What happened in Q3?", page_context=context_v1, stub_agent_env=stub1,
    )

    stub2 = StubAgentEnvConnector(response_text="Q4 answer.")
    _send_message_with_context(
        client, superuser_token_headers, session_id,
        content="What about Q4?", page_context=context_v2, stub_agent_env=stub2,
    )

    # First message → full context block
    assert "<page_context>" in stub1.stream_calls[0]["payload"]["message"], (
        "First message should include full <page_context> block"
    )
    # Second message → diff block, not full context
    second_payload = stub2.stream_calls[0]["payload"]["message"]
    assert "<context_update>" in second_payload, (
        "Second message with changed context should include <context_update> diff block"
    )
    assert "<page_context>" not in second_payload, (
        "Second message with changed context should NOT include full <page_context> block"
    )
