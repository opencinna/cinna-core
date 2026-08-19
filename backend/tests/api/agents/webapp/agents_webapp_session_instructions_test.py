"""
Integration tests: Webapp session-aware extra instructions injection.

Covers the one-time per-session instruction injection triggered by the first
webapp chat message that carries ``page_context``. This feature lets the agent
learn about available webapp actions without burdening every session's system
prompt.

Feature overview:
  - ``SessionService.activate_webapp_context()`` checks the
    ``session_metadata["webapp_actions_context_sent"]`` flag on a Session row.
  - On the first call it sets the flag (committed immediately) and returns True.
  - On subsequent calls it returns False — no double injection.
  - ``message_service.process_pending_messages()`` detects ``page_context`` on any
    pending message, calls ``activate_webapp_context()``, and when True includes
    ``include_extra_instructions`` and ``extra_instructions_prepend`` in the payload
    forwarded to agent-core via ``agent_env_connector.stream_chat()``.
  - Messages without ``page_context`` do not trigger any injection.
  - Regular (non-webapp) sessions using the standard messages endpoint are
    completely unaffected.

Test sections:
  L. activate_webapp_context() — flag lifecycle via API-observable side-effects
  M. Payload injection — first message with page_context sends extra instructions
  N. No injection on second message in same session
  O. Messages without page_context never trigger injection
  P. Non-webapp (regular) sessions are unaffected

Business rules verified:
  28. activate_webapp_context() returns True on the first call and persists the flag
  29. activate_webapp_context() returns False on all subsequent calls (idempotent)
  30. First webapp message with page_context → payload contains
      include_extra_instructions and extra_instructions_prepend
  31. Second webapp message in same session → payload contains neither field
  32. Webapp message without page_context → no extra instructions in payload
  33. Regular session (no webapp_share) without page_context → unaffected
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api
from tests.utils.webapp_interface_config import update_webapp_interface_config
from tests.utils.webapp_share import (
    authenticate_webapp_share,
    setup_webapp_agent,
)

API = settings.API_V1_STR

# The path agent-core is told to read on the first injection
_EXPECTED_EXTRA_INSTRUCTIONS_PATH = "/app/workspace/webapp/WEB_APP_ACTIONS.md"

# Substring that must be present in the prepend text
_EXPECTED_PREPEND_SUBSTRING = "This session is connected to a webapp"


# ── Helpers ────────────────────────────────────────────────────────────────


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


def _send_webapp_message(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
    session_id: str,
    content: str,
    stub_agent_env: StubAgentEnvConnector,
    page_context: str | None = None,
) -> None:
    """
    Send a webapp chat message, patching the agent-env connector.
    Drains background tasks so process_pending_messages runs synchronously.
    """
    payload: dict = {"content": content, "file_ids": []}
    if page_context is not None:
        payload["page_context"] = page_context

    with patch("app.services.sessions.message_service.agent_env_connector", stub_agent_env):
        r = client.post(
            f"{_chat_base(share_token)}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json=payload,
        )
        assert r.status_code == 200, f"POST stream failed: {r.text}"
        drain_tasks()


# ── L. activate_webapp_context() flag lifecycle ────────────────────────────


def test_webapp_context_flag_set_on_first_message_and_idempotent_on_second(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The ``webapp_actions_context_sent`` session flag drives the one-time injection.
    Its lifecycle is verified through observable API side-effects:

      1. Create agent + webapp share, enable chat
      2. Create a webapp chat session (flag absent initially)
      3. Send first message WITH page_context — agent-env payload must include
         ``include_extra_instructions`` (flag was just set, True returned)
      4. Send second message WITH page_context — agent-env payload must NOT include
         ``include_extra_instructions`` (flag already set, False returned)

    We verify the flag via the injection behaviour that it controls: presence or
    absence of ``include_extra_instructions`` in the agent-env payload is the
    observable API-level signal of the flag's True/False state.
    """
    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Context Flag Lifecycle Agent",
        share_label="Flag Lifecycle Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create webapp chat session ───────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    page_context = '{"selected_text":"$1.2M","page":{"url":"https://app.example.com","title":"Dashboard"}}'

    # ── Phase 3: First message with page_context → flag activated → injection
    stub_first = StubAgentEnvConnector(response_text="Hello from agent (first)")
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        "What is the revenue?", stub_first, page_context=page_context,
    )

    assert len(stub_first.stream_calls) >= 1, "Expected at least one stream_chat call"
    first_payload = stub_first.stream_calls[0]["payload"]

    assert "include_extra_instructions" in first_payload, (
        "First message with page_context must trigger flag activation and include "
        "include_extra_instructions in the agent-core payload.\n"
        f"Full payload keys: {list(first_payload.keys())}"
    )
    assert first_payload["include_extra_instructions"] == _EXPECTED_EXTRA_INSTRUCTIONS_PATH, (
        f"include_extra_instructions must be {_EXPECTED_EXTRA_INSTRUCTIONS_PATH!r}.\n"
        f"Got: {first_payload['include_extra_instructions']!r}"
    )
    assert "extra_instructions_prepend" in first_payload, (
        "extra_instructions_prepend must also be present alongside include_extra_instructions"
    )
    assert _EXPECTED_PREPEND_SUBSTRING in first_payload["extra_instructions_prepend"], (
        f"Prepend text must mention webapp connection. Got: "
        f"{first_payload['extra_instructions_prepend']!r}"
    )

    # ── Phase 4: Second message with page_context → flag already set → no injection
    stub_second = StubAgentEnvConnector(response_text="Hello from agent (second)")
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        "Tell me more.", stub_second, page_context=page_context,
    )

    assert len(stub_second.stream_calls) >= 1, "Expected at least one stream_chat call on second message"
    second_payload = stub_second.stream_calls[0]["payload"]

    assert "include_extra_instructions" not in second_payload, (
        "Second message must NOT include include_extra_instructions — flag already set.\n"
        f"Full payload keys: {list(second_payload.keys())}"
    )
    assert "extra_instructions_prepend" not in second_payload, (
        "Second message must NOT include extra_instructions_prepend — flag already set.\n"
        f"Full payload keys: {list(second_payload.keys())}"
    )


# ── M. First message payload injection ────────────────────────────────────


def test_first_webapp_message_with_page_context_includes_extra_instructions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the very first message in a fresh session carries page_context, the
    agent-core payload includes both ``include_extra_instructions`` (the path to
    WEB_APP_ACTIONS.md) and ``extra_instructions_prepend`` (the orientation text).

    Also verifies that the page_context block itself is still present — confirming
    that the regular context-passing pipeline is not disrupted by the injection.

    Scenario:
      1. Create agent + webapp share, enable chat
      2. Create a webapp chat session
      3. Send first message with page_context
      4. Verify payload has include_extra_instructions, extra_instructions_prepend,
         and the usual <page_context> block
    """
    # ── Phase 1 & 2: Setup ────────────────────────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="First Message Injection Agent",
        share_label="First Injection Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    user_message = "Show me the sales figures."
    page_context = '{"selected_text":"Q3 results","page":{"url":"https://app.example.com/sales"}}'

    # ── Phase 3: Send first message with page_context ─────────────────────
    stub = StubAgentEnvConnector(response_text="Sales data is ...")
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        user_message, stub, page_context=page_context,
    )

    # ── Phase 4: Verify payload ───────────────────────────────────────────
    assert len(stub.stream_calls) >= 1, "Expected at least one stream_chat call"
    payload = stub.stream_calls[0]["payload"]

    # Extra instructions fields must be present
    assert "include_extra_instructions" in payload, (
        "First message with page_context must include include_extra_instructions in payload.\n"
        f"Payload keys: {list(payload.keys())}"
    )
    assert payload["include_extra_instructions"] == _EXPECTED_EXTRA_INSTRUCTIONS_PATH, (
        f"Expected path {_EXPECTED_EXTRA_INSTRUCTIONS_PATH!r}, got {payload['include_extra_instructions']!r}"
    )
    assert "extra_instructions_prepend" in payload, (
        "extra_instructions_prepend must be present alongside include_extra_instructions"
    )
    prepend = payload["extra_instructions_prepend"]
    assert _EXPECTED_PREPEND_SUBSTRING in prepend, (
        f"Prepend text must reference webapp connection. Got: {prepend!r}"
    )

    # The page_context pipeline must still function normally alongside the injection
    agent_message_body = payload["message"]
    assert "<page_context>" in agent_message_body, (
        "page_context block must still be present in the message body — "
        "extra instructions injection must not break the context pipeline"
    )
    assert page_context in agent_message_body, (
        "Full page_context JSON must appear in the message body"
    )
    assert user_message in agent_message_body, (
        "Clean user text must also appear in the message body"
    )


# ── N. No injection on second message ─────────────────────────────────────


def test_second_webapp_message_with_page_context_omits_extra_instructions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    After the first page_context message has been processed, the
    webapp_actions_context_sent flag is set. A second message with page_context
    (even with changed context) must NOT include extra instructions in the payload.

    This is the idempotency guarantee: instructions are injected exactly once per
    session lifetime, regardless of how many page_context messages follow.

    Scenario:
      1. Setup agent, webapp share, enable chat
      2. Create session
      3. First message with page_context → injection happens, flag set
      4. Second message with page_context → no injection, flag remains set
      5. Third message with (different) page_context → no injection still
    """
    # ── Phase 1 & 2: Setup ────────────────────────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Idempotent Injection Agent",
        share_label="Idempotent Injection Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    context_a = '{"selected_text":"Revenue: $1M","page":{"url":"https://example.com"}}'
    context_b = '{"selected_text":"Revenue: $2M","page":{"url":"https://example.com"}}'
    context_c = '{"selected_text":"Revenue: $3M","page":{"url":"https://example.com"}}'

    # ── Phase 3: First message → injection expected ───────────────────────
    stub_first = StubAgentEnvConnector(response_text="First agent reply")
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        "What is Q1 revenue?", stub_first, page_context=context_a,
    )
    assert len(stub_first.stream_calls) >= 1
    assert "include_extra_instructions" in stub_first.stream_calls[0]["payload"], (
        "First message must trigger extra instructions injection"
    )

    # ── Phase 4: Second message → no injection ────────────────────────────
    stub_second = StubAgentEnvConnector(response_text="Second agent reply")
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        "What about Q2?", stub_second, page_context=context_b,
    )
    assert len(stub_second.stream_calls) >= 1
    second_payload = stub_second.stream_calls[0]["payload"]

    assert "include_extra_instructions" not in second_payload, (
        "Second message must NOT include include_extra_instructions — one-time injection only.\n"
        f"Payload keys: {list(second_payload.keys())}"
    )
    assert "extra_instructions_prepend" not in second_payload, (
        "Second message must NOT include extra_instructions_prepend.\n"
        f"Payload keys: {list(second_payload.keys())}"
    )

    # ── Phase 5: Third message → still no injection ───────────────────────
    stub_third = StubAgentEnvConnector(response_text="Third agent reply")
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        "And Q3?", stub_third, page_context=context_c,
    )
    assert len(stub_third.stream_calls) >= 1
    third_payload = stub_third.stream_calls[0]["payload"]

    assert "include_extra_instructions" not in third_payload, (
        "Third message must also NOT include include_extra_instructions — already injected once.\n"
        f"Payload keys: {list(third_payload.keys())}"
    )
    assert "extra_instructions_prepend" not in third_payload, (
        "Third message must also NOT include extra_instructions_prepend.\n"
        f"Payload keys: {list(third_payload.keys())}"
    )


# ── O. Messages without page_context do not trigger injection ──────────────


def test_webapp_message_without_page_context_never_triggers_injection(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Messages sent through the webapp chat endpoint that carry no page_context
    must never trigger extra instructions injection — even on the first message
    in a session.

    This verifies the detection predicate: only messages with page_context
    trigger the activation check. A session where every message lacks
    page_context will never have the flag set and will never receive injection.

    Scenario:
      1. Setup agent, webapp share, enable chat
      2. Create session
      3. Send multiple messages WITHOUT page_context
      4. Verify none of the agent-env payloads contain extra instructions fields
      5. Send a message WITH page_context → injection now happens (flag unset)
      6. Verify the injection payload appears on this first page_context message
    """
    # ── Phase 1 & 2: Setup ────────────────────────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="No Context No Injection Agent",
        share_label="No Context Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 3: Multiple messages without page_context ───────────────────
    for i, question in enumerate(["Hello!", "What can you do?", "Tell me more."]):
        stub = StubAgentEnvConnector(response_text=f"Agent reply {i}")
        _send_webapp_message(
            client, webapp_hdrs, share_token, session_id,
            question, stub, page_context=None,
        )
        assert len(stub.stream_calls) >= 1, f"Expected stream call for message {i}"
        payload = stub.stream_calls[0]["payload"]

        # ── Phase 4: No injection fields in payload ────────────────────────
        assert "include_extra_instructions" not in payload, (
            f"Message {i} without page_context must NOT include include_extra_instructions.\n"
            f"Payload keys: {list(payload.keys())}"
        )
        assert "extra_instructions_prepend" not in payload, (
            f"Message {i} without page_context must NOT include extra_instructions_prepend.\n"
            f"Payload keys: {list(payload.keys())}"
        )

    # ── Phase 5: First message WITH page_context → injection fires now ────
    stub_with_ctx = StubAgentEnvConnector(response_text="Context-aware reply")
    page_context = '{"selected_text":"Total: $5M","page":{"url":"https://app.example.com"}}'
    _send_webapp_message(
        client, webapp_hdrs, share_token, session_id,
        "What does the chart show?", stub_with_ctx, page_context=page_context,
    )
    assert len(stub_with_ctx.stream_calls) >= 1

    # ── Phase 6: Injection appears on the first page_context message ───────
    payload_with_ctx = stub_with_ctx.stream_calls[0]["payload"]
    assert "include_extra_instructions" in payload_with_ctx, (
        "First message with page_context (even after prior context-free messages) "
        "must trigger extra instructions injection — flag was never set before.\n"
        f"Payload keys: {list(payload_with_ctx.keys())}"
    )
    assert "extra_instructions_prepend" in payload_with_ctx, (
        "extra_instructions_prepend must accompany include_extra_instructions"
    )


# ── P. Non-webapp (regular) sessions are unaffected ───────────────────────


def test_regular_session_message_never_includes_extra_instructions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Regular chat sessions (created via the standard /sessions/ endpoint, not the
    webapp chat endpoint) send messages without page_context in message_metadata.
    The extra instructions injection must never fire for these sessions.

    This verifies the clean separation between webapp-connected sessions and
    regular sessions: the feature must have zero impact on non-webapp flows.

    Scenario:
      1. Create agent, create a standard (non-webapp) session
      2. Send messages via the regular /sessions/{id}/messages/stream endpoint
      3. Verify agent-env payloads never contain include_extra_instructions or
         extra_instructions_prepend
    """
    # ── Phase 1: Create agent and regular session ─────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="Regular Session Agent")
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]

    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Send messages via the regular sessions endpoint ──────────
    for i, content in enumerate(["Hello!", "How are you?", "What can you help with?"]):
        stub = StubAgentEnvConnector(response_text=f"Regular reply {i}")

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            send_message(client, superuser_token_headers, session_id, content=content)
            drain_tasks()

        assert len(stub.stream_calls) >= 1, f"Expected stream call for message {i}"
        payload = stub.stream_calls[0]["payload"]

        # ── Phase 3: No injection fields in any payload ────────────────────
        assert "include_extra_instructions" not in payload, (
            f"Regular session message {i} must NEVER include include_extra_instructions.\n"
            f"Payload keys: {list(payload.keys())}"
        )
        assert "extra_instructions_prepend" not in payload, (
            f"Regular session message {i} must NEVER include extra_instructions_prepend.\n"
            f"Payload keys: {list(payload.keys())}"
        )
        # Also verify the user message itself was sent correctly
        assert content in payload["message"], (
            f"User message text must appear in the regular session payload"
        )
