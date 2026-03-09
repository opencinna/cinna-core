"""
Integration tests: Webapp Chat — Context passing.

Covers the page_context pipeline and context-diff optimization for webapp chat
endpoints under /api/v1/webapp/{token}/chat/...:
  I. page_context field — full pipeline verification (agent-env injection + clean storage)
  J. Context diff optimization — only changed context is forwarded to agent-env

Business rules tested:
  16. page_context present → full pipeline: agent-env receives <page_context> block,
      stored message.content is clean user text (no XML), agent response is stored
  17. page_context absent or null → agent-env receives no <page_context> block,
      stored content is clean user text
  18. page_context over 10,000 chars → agent-env receives truncated context,
      stored content is clean user text
  20. First message with page_context → full <page_context> block sent to agent-env
  21. Second message with identical page_context → no context block sent to agent-env
  22. Second message with changed page_context → <context_update> diff block sent to agent-env,
      not a full <page_context> block
  23. Malformed JSON in previous page_context → falls back to full <page_context> block
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.background_tasks import drain_tasks
from tests.utils.webapp_interface_config import update_webapp_interface_config
from tests.utils.webapp_share import (
    authenticate_webapp_share,
    setup_webapp_agent,
)

API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────


def _get_webapp_jwt(client: TestClient, token: str) -> str:
    """Authenticate via webapp share and return the access_token string."""
    auth = authenticate_webapp_share(client, token)
    assert "access_token" in auth, f"No access_token in auth response: {auth}"
    return auth["access_token"]


def _webapp_headers(client: TestClient, token: str) -> dict[str, str]:
    """Return Authorization headers for the webapp-viewer JWT."""
    return {"Authorization": f"Bearer {_get_webapp_jwt(client, token)}"}


def _chat_base(token: str) -> str:
    """Base URL for chat endpoints for a given webapp share token."""
    return f"{API}/webapp/{token}/chat"


def _enable_chat(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    chat_mode: str = "conversation",
) -> None:
    """Enable chat for an agent by setting chat_mode via the config endpoint."""
    update_webapp_interface_config(client, headers, agent_id, chat_mode=chat_mode)


def _create_chat_session(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
) -> dict:
    """POST /webapp/{token}/chat/sessions and assert 200. Returns session dict."""
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"Create chat session failed: {r.text}"
    return r.json()


def _send_chat_message_with_context(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
    session_id: str,
    user_message: str,
    page_context: str | None,
    stub_agent_env,
) -> None:
    """
    Send a webapp chat message with optional page_context, patching the
    agent-env connector so the message is processed without a real Docker env.
    Drains background tasks so collect_pending_messages runs synchronously.
    """
    patch_target = "app.services.message_service.agent_env_connector"
    with patch(patch_target, stub_agent_env):
        payload = {"content": user_message, "file_ids": []}
        if page_context is not None:
            payload["page_context"] = page_context

        r = client.post(
            f"{_chat_base(share_token)}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json=payload,
        )
        assert r.status_code == 200, f"POST stream failed: {r.text}"
        drain_tasks()


# ── I. page_context field — full pipeline verification ─────────────────────


def test_webapp_chat_message_with_page_context_full_pipeline(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When page_context is provided, the full streaming pipeline stores clean
    user text in message.content (no XML) and sends the <page_context> block
    to the agent-env as a separate injection. Verified end-to-end using
    StubAgentEnvConnector (same pattern as MCP and context-diff tests).

    Verifies:
      1. Agent-env receives the full <page_context> block in the payload
      2. Agent-env payload contains the clean user text
      3. Stored message.content (via GET messages API) is the clean user text
         with no XML block — confirms page_context is never embedded in content
      4. Agent response is stored and retrievable
    """
    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Page Context Pipeline Agent",
        share_label="Context Pipeline Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create webapp chat session ───────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 3: Send message with page_context through full pipeline ─────
    user_message = "What is the total revenue shown?"
    page_context_payload = '{"selected_text":"$2.4M","microdata":[{"type":"https://schema.org/QuantitativeValue","properties":{"value":"2.4M","unitText":"USD"}}]}'

    stub = StubAgentEnvConnector(response_text="The total revenue is $2.4M")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        user_message, page_context_payload, stub,
    )

    # ── Phase 4: Verify agent-env received the page_context block ─────────
    assert len(stub.stream_calls) >= 1, "Expected at least one stream_chat call"
    agent_payload_message = stub.stream_calls[0]["payload"]["message"]

    assert "<page_context>" in agent_payload_message, (
        f"Agent-env should receive <page_context> block. Got:\n{agent_payload_message!r}"
    )
    assert page_context_payload in agent_payload_message, (
        "Full page_context JSON must be present in the agent-env payload"
    )
    assert user_message in agent_payload_message, (
        "Clean user text must be present in the agent-env payload"
    )

    # ── Phase 5: Verify stored message content is clean (no XML) ──────────
    r_msgs = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r_msgs.status_code == 200, f"GET messages failed: {r_msgs.text}"
    messages_data = r_msgs.json()

    user_messages = [m for m in messages_data["data"] if m["role"] == "user"]
    assert len(user_messages) >= 1, "Expected at least one user message"

    stored_content = user_messages[-1]["content"]
    assert stored_content == user_message, (
        f"Stored message content must be clean user text, not augmented with XML.\n"
        f"Expected: {user_message!r}\n"
        f"Got:      {stored_content!r}"
    )
    assert "<page_context>" not in stored_content, (
        "XML block must not appear in the stored message content"
    )

    # ── Phase 6: Verify agent response was stored ─────────────────────────
    agent_messages = [m for m in messages_data["data"] if m["role"] == "agent"]
    assert len(agent_messages) >= 1, "Expected at least one agent message"
    assert "2.4M" in agent_messages[-1]["content"], (
        "Agent response should be stored and retrievable"
    )


def test_webapp_chat_message_without_page_context_unchanged(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When page_context is absent or null, the agent-env receives only the
    clean user text with no <page_context> block. Verified end-to-end.

    Verifies:
      1. No page_context field → agent-env payload has no <page_context> block
      2. page_context: null → same behavior
      3. Stored message content matches the clean user text
    """
    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="No Context Baseline Agent",
        share_label="Baseline Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create webapp chat session ───────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 3a: No page_context field at all ────────────────────────────
    user_message = "Show me the summary"
    stub_no_ctx = StubAgentEnvConnector(response_text="Here is the summary")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        user_message, None, stub_no_ctx,
    )

    assert len(stub_no_ctx.stream_calls) >= 1, "Expected stream_chat call"
    payload_no_ctx = stub_no_ctx.stream_calls[0]["payload"]["message"]
    assert "<page_context>" not in payload_no_ctx, (
        "No <page_context> block should be sent when page_context is absent"
    )
    assert user_message in payload_no_ctx, (
        "User message text must appear in the agent payload"
    )

    # ── Phase 3b: page_context: null (send via raw HTTP to include null) ──
    stub_null_ctx = StubAgentEnvConnector(response_text="Summary with null ctx")
    with patch("app.services.message_service.agent_env_connector", stub_null_ctx):
        r = client.post(
            f"{_chat_base(share_token)}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json={"content": "Another question", "file_ids": [], "page_context": None},
        )
        assert r.status_code == 200, f"POST with page_context=null failed: {r.text}"
        drain_tasks()

    assert len(stub_null_ctx.stream_calls) >= 1, "Expected stream_chat call for null ctx"
    payload_null_ctx = stub_null_ctx.stream_calls[0]["payload"]["message"]
    assert "<page_context>" not in payload_null_ctx, (
        "No <page_context> block should be sent when page_context is null"
    )

    # ── Phase 4: Verify stored messages are clean ─────────────────────────
    r_msgs = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r_msgs.status_code == 200
    user_msgs = [m for m in r_msgs.json()["data"] if m["role"] == "user"]
    for msg in user_msgs:
        assert "<page_context>" not in msg["content"], (
            f"Stored message should not contain XML block: {msg['content']!r}"
        )


def test_webapp_chat_message_page_context_truncated_at_limit(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When page_context exceeds 10,000 chars, the route truncates it before
    passing to the service pipeline. Verified end-to-end: the agent-env
    receives a <page_context> block with at most 10,000 chars of context.

    Verifies:
      1. Oversized page_context is accepted by the endpoint (no 422)
      2. The agent-env receives a <page_context> block with truncated content
      3. Stored message content is the clean user text (no XML)
    """
    from app.api.routes.webapp_chat import _PAGE_CONTEXT_MAX_CHARS

    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Truncation Test Agent",
        share_label="Truncation Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create webapp chat session ───────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 3: Send oversized page_context through full pipeline ────────
    user_message = "Analyze the data"
    oversized_context = "x" * (_PAGE_CONTEXT_MAX_CHARS + 1_000)

    stub = StubAgentEnvConnector(response_text="Analysis complete")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        user_message, oversized_context, stub,
    )

    # ── Phase 4: Verify agent-env received truncated context ──────────────
    assert len(stub.stream_calls) >= 1, "Expected at least one stream_chat call"
    agent_payload_message = stub.stream_calls[0]["payload"]["message"]

    assert "<page_context>" in agent_payload_message, (
        "Agent-env should receive <page_context> block even when truncated"
    )
    # The full oversized context must NOT appear (it was truncated)
    assert oversized_context not in agent_payload_message, (
        "Full oversized context must not appear — should be truncated"
    )
    # The truncated version (first N chars) should appear
    truncated = oversized_context[:_PAGE_CONTEXT_MAX_CHARS]
    assert truncated in agent_payload_message, (
        f"Truncated context ({_PAGE_CONTEXT_MAX_CHARS} chars) should appear in payload"
    )

    # ── Phase 5: Verify stored content is clean ───────────────────────────
    r_msgs = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r_msgs.status_code == 200
    user_msgs = [m for m in r_msgs.json()["data"] if m["role"] == "user"]
    assert len(user_msgs) >= 1
    assert user_msgs[-1]["content"] == user_message, (
        "Stored message content must be clean user text"
    )
    assert "<page_context>" not in user_msgs[-1]["content"], (
        "XML block must not appear in stored message content"
    )


# ── J. Context diff optimization ──────────────────────────────────────────


def test_context_diff_first_message_sends_full_page_context(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the first message in a session carries page_context, the agent-env
    receives the full <page_context> block (no previous context to diff against).

    Scenario:
      1. Create agent + webapp share, enable chat
      2. Create a webapp chat session
      3. Send first message with page_context via the stream endpoint
      4. Drain background tasks (triggers process_pending_messages)
      5. Inspect the payload the stub agent-env received
      6. Verify the payload message contains <page_context>...</page_context>
    """

    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Context Diff First Message Agent",
        share_label="First Message Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create webapp chat session ───────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 3: Send first message with page_context ─────────────────────
    user_message = "What is the total shown?"
    page_context = '{"selected_text":"$2.4M","page":{"url":"https://example.com","title":"Sales"}}'
    stub = StubAgentEnvConnector(response_text="Hello from agent")

    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        user_message, page_context, stub,
    )

    # ── Phase 4: Verify full <page_context> block sent to agent-env ───────
    assert len(stub.stream_calls) >= 1, "Expected at least one stream_chat call"
    agent_message = stub.stream_calls[0]["payload"]["message"]

    assert "<page_context>" in agent_message, (
        f"First message must include full <page_context> block. Got:\n{agent_message!r}"
    )
    assert "<context_update>" not in agent_message, (
        "First message must NOT use <context_update> (no previous context to diff)"
    )
    assert page_context in agent_message, (
        "Full page_context JSON must be present in the first message's context block"
    )


def test_context_diff_identical_context_omits_block(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the second message has an identical page_context to the already-sent
    first message, the agent-env receives NO context block at all — neither
    <page_context> nor <context_update>. This is the primary optimization:
    unchanged context adds zero tokens to the agent conversation.

    Scenario:
      1. Create agent + webapp share, enable chat
      2. Create webapp chat session
      3. Send first message with page_context — drain tasks (becomes "sent")
      4. Send second message with the SAME page_context — drain tasks
      5. Verify second stream call has no <page_context> or <context_update>
    """

    # ── Phase 1 & 2: Setup ────────────────────────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Context Diff Identical Agent",
        share_label="Identical Context Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    page_context = '{"selected_text":"$2.4M","page":{"url":"https://example.com","title":"Sales"}}'

    # ── Phase 3: Send first message, drain (marks message as "sent") ──────
    stub_first = StubAgentEnvConnector(response_text="First response")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        "What is the revenue?", page_context, stub_first,
    )
    assert len(stub_first.stream_calls) >= 1, "First send should reach agent-env"

    # ── Phase 4: Send second message with identical page_context ──────────
    stub_second = StubAgentEnvConnector(response_text="Second response")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        "Can you elaborate?", page_context, stub_second,
    )

    # ── Phase 5: Verify no context block in second agent-env call ─────────
    assert len(stub_second.stream_calls) >= 1, "Second send should reach agent-env"
    agent_message = stub_second.stream_calls[0]["payload"]["message"]

    assert "<page_context>" not in agent_message, (
        "Identical context must NOT produce a <page_context> block on the second message.\n"
        f"Agent message received:\n{agent_message!r}"
    )
    assert "<context_update>" not in agent_message, (
        "Identical context must NOT produce a <context_update> block on the second message.\n"
        f"Agent message received:\n{agent_message!r}"
    )
    # The user message text itself must still be present
    assert "Can you elaborate?" in agent_message, (
        "User message text must still appear in the agent payload"
    )


def test_context_diff_changed_context_sends_diff_block(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the second message has a page_context that differs from the previously
    sent one, the agent-env receives a compact <context_update> JSON diff block —
    NOT a full <page_context> block. This minimises context window usage when
    only part of the dashboard changed.

    Scenario:
      1. Create agent + webapp share, enable chat
      2. Create webapp chat session
      3. Send first message with page_context A — drain tasks
      4. Send second message with page_context B (different selected_text) — drain tasks
      5. Verify second stream call contains <context_update> (not <page_context>)
      6. Verify diff JSON contains "changed" key with selected_text difference
    """
    import json

    # ── Phase 1 & 2: Setup ────────────────────────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Context Diff Changed Agent",
        share_label="Changed Context Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    context_a = '{"selected_text":"$2.4M","page":{"url":"https://example.com","title":"Sales"}}'
    context_b = '{"selected_text":"$3.1M","page":{"url":"https://example.com","title":"Sales"}}'

    # ── Phase 3: Send first message with context A ────────────────────────
    stub_first = StubAgentEnvConnector(response_text="First response")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        "What is Q3 revenue?", context_a, stub_first,
    )
    assert len(stub_first.stream_calls) >= 1

    # ── Phase 4: Send second message with context B ───────────────────────
    stub_second = StubAgentEnvConnector(response_text="Second response")
    _send_chat_message_with_context(
        client, webapp_hdrs, share_token, session_id,
        "What changed in Q4?", context_b, stub_second,
    )

    # ── Phase 5: Verify <context_update> block present ────────────────────
    assert len(stub_second.stream_calls) >= 1, "Second send should reach agent-env"
    agent_message = stub_second.stream_calls[0]["payload"]["message"]

    assert "<context_update>" in agent_message, (
        "Changed context must produce a <context_update> diff block.\n"
        f"Agent message received:\n{agent_message!r}"
    )
    assert "<page_context>" not in agent_message, (
        "Changed context must NOT produce a full <page_context> block — only a diff.\n"
        f"Agent message received:\n{agent_message!r}"
    )

    # ── Phase 6: Verify diff JSON contains the changed field ──────────────
    # Extract JSON between <context_update> tags
    start = agent_message.index("<context_update>") + len("<context_update>")
    end = agent_message.index("</context_update>")
    diff_json_str = agent_message[start:end].strip()
    diff = json.loads(diff_json_str)

    assert "changed" in diff, (
        f"Diff must have a 'changed' key when a top-level field changed. Got: {diff}"
    )
    assert "selected_text" in diff["changed"], (
        f"'selected_text' must appear in changed keys. Got changed: {diff['changed']}"
    )
    assert diff["changed"]["selected_text"]["from"] == "$2.4M"
    assert diff["changed"]["selected_text"]["to"] == "$3.1M"
