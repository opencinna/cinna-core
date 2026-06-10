"""
Integration tests: A2A inbound file-attachment path (cinna_file_ids).

Covers the full path for an A2A client that references previously-uploaded files
in an inbound message via ``message.metadata.cinna_file_ids``.  The helper
``_extract_file_ids_from_message`` parses the IDs, and the standard session
pipeline attaches the files to the user message and uploads them to the
agent environment.

Test scenarios
──────────────

H. Happy-path integration
   H1. A2A streaming message carrying ``cinna_file_ids`` for a file owned by the
       session user → session message created, file appears in the user message's
       ``files`` list with source='user_upload'.

S. Security guard (ownership boundary)
   S1. ``cinna_file_ids`` referencing a file owned by a *different* user →
       SSE stream carries a JSON-RPC error event (-32001) and the session message
       is NOT created.

Notes
─────
- File uploads go to disk; tests patch UPLOAD_BASE_PATH to a tmp directory so no
  real data is written outside the test tree.
- ``file_service.upload_files_to_agent_env`` uses ``EnvironmentService.get_lifecycle_manager()``
  so the autouse ``patch_environment_adapter`` fixture's singleton is respected automatically.
- The autouse conftest fixtures (``patch_create_session``, ``patch_environment_adapter``,
  ``background_tasks``, ``patch_external_services``) are inherited automatically.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    a2a_headers as _a2a_headers,
    parse_sse_events,
    setup_a2a_agent,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages
from tests.utils.user import create_random_user, user_authentication_headers

_API = settings.API_V1_STR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_streaming_request_with_file_ids(
    message_text: str,
    file_ids: list[str],
    task_id: str | None = None,
) -> dict:
    """Build a v1.0 SendStreamingMessage payload with cinna_file_ids in metadata."""
    message: dict = {
        "role": "user",
        "parts": [{"text": message_text}],
        "messageId": uuid.uuid4().hex,
        "metadata": {
            "cinna_file_ids": file_ids,
        },
    }
    if task_id:
        message["taskId"] = task_id

    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "SendStreamingMessage",
        "params": {
            "message": message,
        },
    }


def _upload_file(
    client: TestClient,
    auth_headers: dict[str, str],
    content: bytes = b"test file content",
    filename: str = "test.txt",
    content_type: str = "text/plain",
) -> dict:
    """Upload a file via POST /files/upload and return the response dict.

    Requires UPLOAD_BASE_PATH to already be patched to a tmp directory by
    the caller.
    """
    r = client.post(
        f"{_API}/files/upload",
        headers=auth_headers,
        files={"file": (filename, io.BytesIO(content), content_type)},
    )
    assert r.status_code == 200, f"File upload failed: {r.text}"
    return r.json()


def _extract_event_text(events: list[dict]) -> str:
    """Extract concatenated text from A2A SSE event status message parts."""
    parts_text: list[str] = []
    for e in events:
        msg = e.get("result", {}).get("status", {}).get("message")
        if not msg or "parts" not in msg:
            continue
        for part in msg["parts"]:
            text = part.get("text") or (part.get("root", {}) or {}).get("text", "")
            if text:
                parts_text.append(text)
    return "\n".join(parts_text)


# ---------------------------------------------------------------------------
# H1. Happy path — file owned by session user → attached to user message
# ---------------------------------------------------------------------------


def test_a2a_inbound_file_ids_happy_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path,
) -> None:
    """
    H1. A2A streaming message with cinna_file_ids for a file owned by the
        session user attaches the file to the user message.

      1. Setup: create A2A agent, upload a file as the agent-owner (superuser)
      2. Send A2A streaming message with metadata.cinna_file_ids = [file_id]
      3. Drain tasks — session message created, file uploaded to agent-env
      4. Verify SSE events are well-formed (working → completed)
      5. Verify: user message in session has the file in its ``files`` list
         with source='user_upload'
      6. Verify: agent replied and the stub received the enriched message content

    The autouse ``patch_environment_adapter`` fixture installs a test lifecycle
    manager via ``EnvironmentService._lifecycle_manager``.  Because
    ``file_service.upload_files_to_agent_env`` now calls
    ``EnvironmentService.get_lifecycle_manager()``, the fixture's adapter is
    used automatically — no manual patching of the lifecycle manager is needed.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        # ── Phase 1: Setup A2A agent ──────────────────────────────────────
        agent, token_data = setup_a2a_agent(
            client, superuser_token_headers, name="A2A File Attachment Agent",
        )
        agent_id = agent["id"]
        a2a_token = token_data["token"]

        # ── Phase 2: Upload a file as the superuser ───────────────────────
        file_content = b"Attachment payload for A2A test"
        uploaded = _upload_file(
            client, superuser_token_headers,
            content=file_content,
            filename="inbound_attachment.txt",
        )
        file_id = uploaded["id"]
        assert uploaded["status"] == "temporary", (
            f"Freshly uploaded file must have status='temporary', got: {uploaded['status']!r}"
        )

        # ── Phase 3: Send A2A streaming message with cinna_file_ids ───────
        agent_response_text = "I received your file, here is my analysis."
        stub = StubAgentEnvConnector(response_text=agent_response_text)

        request = _build_streaming_request_with_file_ids(
            message_text="Please analyse the attached file.",
            file_ids=[file_id],
        )

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            resp = client.post(
                f"{_API}/a2a/{agent_id}/",
                headers=_a2a_headers(a2a_token),
                json=request,
            )
            drain_tasks()

        assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"

        # ── Phase 4: Verify SSE events are well-formed ────────────────────
        # Mirror the access pattern from test_a2a_v1_streaming_full_flow:
        # every event is a JSON-RPC object; events that carry results have
        # the "result" key; the first result event is the initial "working"
        # status-update with taskId; the last is the "completed" final event.
        events = parse_sse_events(resp.text)
        assert len(events) >= 1, (
            f"Expected at least one SSE event. Response: {resp.text}"
        )

        # Collect only result-carrying events (skip any error events if present,
        # and surface a helpful message if there are none).
        result_events = [e for e in events if "result" in e]
        assert result_events, (
            f"Expected at least one JSON-RPC result event. "
            f"All events: {events}\nRaw response: {resp.text}"
        )

        # First result event: initial "working" status-update
        first = result_events[0]["result"]
        assert first.get("kind") == "status-update", (
            f"First result event must be a status-update. Got: {first!r}"
        )
        task_id = first.get("taskId")
        assert task_id is not None, (
            f"First status-update must carry taskId. Got: {first!r}"
        )

        # Last result event: final "completed" status
        last = result_events[-1]["result"]
        assert last.get("status", {}).get("state") == "completed", (
            f"Last result event must have status.state='completed'. Got: {last!r}"
        )
        assert last.get("final") is True, (
            f"Last result event must have final=True. Got: {last!r}"
        )

        # Agent text appears somewhere in the stream
        agent_text = _extract_event_text(events)
        assert agent_response_text in agent_text, (
            f"Expected agent response in SSE events. Got: {agent_text!r}"
        )

        # ── Phase 5: Verify user message has file attached ─────────────────
        all_msgs = list_messages(client, superuser_token_headers, task_id)
        user_msgs = [m for m in all_msgs if m["role"] == "user"]
        assert user_msgs, "Expected at least one user message in the session"

        user_msg = user_msgs[0]
        assert "Please analyse the attached file." in user_msg["content"], (
            f"User message content must include the original text. "
            f"Got: {user_msg['content']!r}"
        )

        msg_files = user_msg.get("files", [])
        assert msg_files, (
            f"User message must have files attached when cinna_file_ids was provided. "
            f"files: {msg_files}"
        )

        user_upload_files = [f for f in msg_files if f.get("source") == "user_upload"]
        assert len(user_upload_files) >= 1, (
            f"Expected at least one file with source='user_upload'. "
            f"files: {msg_files}"
        )

        attached_file = user_upload_files[0]
        assert attached_file["filename"] == "inbound_attachment.txt", (
            f"Attached file must be the uploaded file. Got: {attached_file['filename']!r}"
        )
        assert attached_file["file_size"] == len(file_content), (
            f"File size must match uploaded content. "
            f"Expected {len(file_content)}, got {attached_file['file_size']!r}"
        )

        # ── Phase 6: Verify the stub received enriched agent-bound content ─
        # prepare_user_message_with_files prepends "Uploaded files:\n..." to the
        # agent-bound content — stub.stream_calls[0]["payload"]["message"] shows
        # this enrichment, which includes the uploaded filename.
        assert len(stub.stream_calls) >= 1, (
            "Agent-env stub should have been called at least once for the LLM stream"
        )
        agent_payload_message = stub.stream_calls[0]["payload"]["message"]
        assert "inbound_attachment.txt" in agent_payload_message, (
            f"Agent-env message payload must reference the uploaded filename. "
            f"Payload message: {agent_payload_message!r}"
        )


# ---------------------------------------------------------------------------
# S1. Security guard — file owned by different user → SSE JSON-RPC error
# ---------------------------------------------------------------------------


def test_a2a_inbound_file_ids_foreign_file_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path,
) -> None:
    """
    S1. A2A message referencing a file NOT owned by the session user is rejected.

    Security boundary: ``prepare_user_message_with_files`` enforces
    ``file.user_id == session.user_id`` (403). The A2A streaming handler
    surfaces this as a JSON-RPC error event with code -32001.

      1. Setup: create A2A agent (owned by superuser)
      2. Create a second user; they upload a file (owned by second user)
      3. Send A2A message from the agent (user_id = superuser) with the
         second user's file_id in cinna_file_ids
      4. Verify: SSE stream contains a JSON-RPC error event
      5. Verify: no user message with files was created in the session
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        # ── Phase 1: Setup A2A agent ──────────────────────────────────────
        agent, token_data = setup_a2a_agent(
            client, superuser_token_headers, name="A2A Auth Guard Agent",
        )
        agent_id = agent["id"]
        a2a_token = token_data["token"]

        # ── Phase 2: Second user uploads a file ──────────────────────────
        second_user = create_random_user(client)
        second_user_headers = user_authentication_headers(
            client=client,
            email=second_user["email"],
            password=second_user["_password"],
        )

        other_file = _upload_file(
            client, second_user_headers,
            content=b"File owned by second user",
            filename="other_user_file.txt",
        )
        other_file_id = other_file["id"]
        assert other_file["status"] == "temporary", (
            f"Freshly uploaded file must be 'temporary', got: {other_file['status']!r}"
        )

        # ── Phase 3: Send A2A message with the other user's file id ───────
        # The A2A route sets user_id = agent.owner_id = superuser.id (since no
        # session user is involved — only the A2A access token is present).
        # Therefore the file owned by second_user is not authorised.
        request = _build_streaming_request_with_file_ids(
            message_text="Can you read this file?",
            file_ids=[other_file_id],
        )

        stub = StubAgentEnvConnector(response_text="Should not be reached")

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            resp = client.post(
                f"{_API}/a2a/{agent_id}/",
                headers=_a2a_headers(a2a_token),
                json=request,
            )
            drain_tasks()

        assert resp.status_code == 200, (
            f"A2A endpoint must return HTTP 200 even on JSON-RPC errors. "
            f"Got {resp.status_code}: {resp.text}"
        )

        # ── Phase 4: SSE stream must carry a JSON-RPC error ────────────────
        events = parse_sse_events(resp.text)
        assert events, f"Expected at least one SSE event. Response: {resp.text}"

        # The error is emitted as the only SSE event: a JSON-RPC error object.
        error_events = [e for e in events if "error" in e]
        assert error_events, (
            "Expected at least one JSON-RPC error event in the SSE stream. "
            f"Events: {events}"
        )

        error_event = error_events[0]
        assert error_event["error"]["code"] == -32001, (
            f"Expected JSON-RPC error code -32001 (generic A2A service error). "
            f"Got: {error_event['error']}"
        )
        error_message = error_event["error"].get("message", "")
        # The error message from prepare_user_message_with_files propagates
        # through send_session_message (action=error) to the SSE layer.
        assert error_message, (
            f"JSON-RPC error must carry a non-empty message. Got: {error_event['error']!r}"
        )

        # ── Phase 5: No user message with files was persisted ─────────────
        # We need the task_id to fetch messages. When the error fires inside
        # send_session_message (Phase 3, after session creation), a session
        # IS created but the user message with files is NOT committed.
        #
        # We identify the session by the taskId carried on the *first*
        # non-error SSE event (the initial working status-update). If the
        # error fires before any session event was yielded, there is no
        # taskId to check — in that case the absence of any message list
        # result is sufficient.
        task_id = None
        for e in events:
            result = e.get("result", {})
            if result.get("taskId"):
                task_id = result["taskId"]
                break

        if task_id:
            # A session was created before the error; verify no file-bearing
            # user message was committed.
            all_msgs = list_messages(client, superuser_token_headers, task_id)
            user_msgs = [m for m in all_msgs if m["role"] == "user"]
            for um in user_msgs:
                files_with_uploads = [
                    f for f in um.get("files", [])
                    if f.get("source") == "user_upload"
                ]
                assert not files_with_uploads, (
                    f"No user message should have the foreign file attached. "
                    f"Found: {files_with_uploads}"
                )
        # If task_id is None the session was never created → no messages to check.
        # The presence of the error event is the primary assertion.

        # ── Phase 6: Agent-env stub was NOT called (error fired before LLM) ─
        assert len(stub.stream_calls) == 0, (
            "Agent-env stub must NOT be called when file ownership check fails. "
            f"Got {len(stub.stream_calls)} call(s)."
        )
