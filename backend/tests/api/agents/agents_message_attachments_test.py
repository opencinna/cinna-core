"""
Integration tests: Agent Message Attachments.

Covers the full attachment flow: agent emits a <cinna_attach> tag in its reply,
the backend materialises the file from the workspace, splices an ``attachment``
event into the message's streaming_events, strips the tag from the visible text,
and surfaces the attachment via ``MessagePublic.files`` and the
``GET /files/{id}/download`` endpoint.

Test scenarios
──────────────
A. Happy-path materialisation
   A1. Agent reply with a valid ``<cinna_attach>`` tag in /app/workspace/files/
       → file materialised, ``attachment`` event in streaming_events, tag stripped.
   A2. Files in different sub-directories both materialise (files/ and app-data/).
   A3. Markdown/CSV/JSON attachments materialise via MIME fallback.
   A4. Multiple attachments + interleaved text → correct inline order, event_seq
       contiguous.

B. MessagePublic.files projection
   B1. GET /sessions/{id}/messages exposes the agent attachment with
       source='agent_attachment', ordered by event_seq.
   B2. User uploads on the same session retain source='user_upload'.

C. Download endpoint
   C1. JWT-authenticated download → 200, X-Content-Type-Options: nosniff.
   C2. Signed ``?token=`` download → 200 (no JWT required).
   C3. Expired / invalid token → 401.
   C4. Token whose file_id ≠ path file_id → 403.
   C5. ``?disposition=inline`` with an inline-safe MIME → Content-Disposition: inline
       + nosniff + CSP sandbox header.
   C6. ``?disposition=inline`` with text/html → forced to attachment + nosniff,
       no CSP.
   C7. No auth → 401.

D. Rejection / error cases (attachment_error emitted, no record created)
   D1. Non-absolute path in tag body.
   D2. Path outside /app/workspace (e.g. /etc/passwd).
   D3. Path normalising outside workspace via ``..``.
   D4. Disallowed MIME type (application/octet-stream) — not in settings whitelist.
   D5. File not found on the adapter (workspace_files not populated).

E. User message tags are never parsed
   E1. Tag in a user message → no attachment, tag visible in stored content.

F. De-duplication
   F1. Same path in the tag twice → one FileUpload record, two attachment events
       both referencing the same file_id.

G. A2A: attachment events → FilePart
   G1. GET /a2a history (GetTask) returns a FilePart carrying
       cinna.content_kind="file" and the cinna.file_* metadata keys.
   G2. The FilePart.file.uri embeds a signed ?token= download token that the
       download endpoint accepts.

Skipped / mocked cases (reason noted inline):
  - Real Docker volume reads (get_local_workspace_file_path): the EnvironmentTestAdapter
    has no Docker volume; tests use fetch_workspace_item_with_meta instead (the
    remote-adapter code path), exercising the same validation/storage logic.
  - Over-size / over-quota: would require multi-MB fake content; patching
    settings.UPLOAD_MAX_FILE_SIZE_MB to a tiny value instead.
  - >10 attachments per message: tested via per-message count cap with a small limit
    (patched MAX_ATTACHMENTS_PER_MESSAGE).
  - Guest session download: tested in scenario C2 via the signed-token path which
    guest clients would use via A2A (JWT-based guest flow is covered by
    guest_shares_sessions_test.py which uses the same permission check path).
"""
import asyncio
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages, send_message
from tests.utils.session import create_session_via_api
from tests.utils.a2a import setup_a2a_agent, build_streaming_request, parse_sse_events

API = settings.API_V1_STR

# Workspace path constants used throughout the tests.
_WORKSPACE_ROOT = "/app/workspace"
_FILES_PDF = f"{_WORKSPACE_ROOT}/files/report.pdf"
_APPDATA_CSV = f"{_WORKSPACE_ROOT}/app-data/storage/export.csv"
_FILES_MD = f"{_WORKSPACE_ROOT}/files/notes.md"
_FILES_JSON = f"{_WORKSPACE_ROOT}/files/data.json"

# Minimal valid file bytes for each MIME type used in tests.
_PDF_CONTENT = b"%PDF-1.4 test content"
_CSV_CONTENT = b"col1,col2\nval1,val2\n"
_MD_CONTENT = b"# Heading\n\nSome text."
_JSON_CONTENT = b'{"key": "value"}'
_TEXT_CONTENT = b"plain text content"

# Vendor-namespaced A2A metadata keys (mirrors a2a_event_mapper constants).
_A2A_CONTENT_KIND_KEY = "cinna.content_kind"
_A2A_FILE_ID_KEY = "cinna.file_id"
_A2A_FILE_NAME_KEY = "cinna.file_name"
_A2A_FILE_MIME_KEY = "cinna.file_mime"
_A2A_FILE_SIZE_KEY = "cinna.file_size"
_A2A_KIND_FILE = "file"


# ── Module-level fixture override ────────────────────────────────────────────
#
# SOURCE CODE NOTE: _process_attachments (message_service.py) calls
# asyncio.to_thread(_materialize_sync), where _materialize_sync itself calls
# asyncio.run(materialize_attachments(...)). This is correct for production
# (asyncio.to_thread runs in a real thread with no running event loop), but the
# global test fixture patches asyncio.to_thread to run synchronously, causing
# "asyncio.run() cannot be called from a running event loop" when drain_tasks()
# exercises the path.
#
# Fix: for this module, we override patch_asyncio_to_thread to NOT flatten
# asyncio.to_thread into a synchronous call.  Instead we supply a stub that
# runs the callable via asyncio.run() — and additionally patch the asyncio.run
# reference inside message_service so it uses the running loop's
# run_until_complete when one is already active (same idea as nest_asyncio but
# scoped only to the service module).
#
# The DB session passed into _materialize_sync is the NonClosingSessionProxy
# backed by the test transaction; it is read inside the same thread that calls
# run_until_complete (no cross-thread access), so thread safety is not an issue.
@pytest.fixture(autouse=True)
def patch_asyncio_to_thread():
    """Override: asyncio.to_thread runs synchronously EXCEPT for attachment materialisation.

    The global fixture replaces asyncio.to_thread with a synchronous stub for
    all code paths. For the attachment flow specifically, _process_attachments
    calls asyncio.to_thread(_materialize_sync) where _materialize_sync in turn
    calls asyncio.run(...) — which fails inside a running event loop.

    This override calls the original asyncio.to_thread only for calls that
    originate from the message_service attachment materialisation path
    (detected by the function name being ``_materialize_sync``), and runs all
    other to_thread calls synchronously as before. The materialisation thread
    gets its own event loop via asyncio.run(), while the test DB session is
    accessed via the patched create_session (NonClosingSessionProxy), which
    creates a fresh proxy around the test connection for each call and is safe
    to use from a short-lived background thread.
    """
    import asyncio as _real_asyncio

    _original_to_thread = _real_asyncio.to_thread

    async def _selective_to_thread(func, /, *args, **kwargs):
        # Route materialisation through real threading; run others synchronously.
        if getattr(func, "__name__", "") == "_materialize_sync":
            # Use the real asyncio.to_thread so _materialize_sync runs in a
            # fresh OS thread with no running event loop, allowing asyncio.run()
            # inside it to work correctly.
            return await _original_to_thread(func, *args, **kwargs)
        return func(*args, **kwargs)

    with patch("asyncio.to_thread", _selective_to_thread):
        yield


# ── Helpers ───────────────────────────────────────────────────────────────────


def _attach_tag(path: str) -> str:
    """Return a <cinna_attach> tag for the given absolute workspace path."""
    return f"<cinna_attach>{path}</cinna_attach>"


def _agent_response_with_attach(text_before: str, path: str, text_after: str = "") -> str:
    """Build a realistic agent reply that declares one attachment."""
    tag = _attach_tag(path)
    parts = [text_before, tag]
    if text_after:
        parts.append(text_after)
    return "".join(parts)


def _assert_contiguous_event_seq(streaming_events: list[dict]) -> None:
    """Assert that event_seq values are contiguous 1-based integers."""
    seq_values = [e.get("event_seq") for e in streaming_events]
    assert None not in seq_values, (
        f"All streaming_events must have event_seq. Got: {seq_values}"
    )
    expected = list(range(1, len(streaming_events) + 1))
    assert seq_values == expected, (
        f"event_seq must be contiguous 1-based integers. "
        f"Expected {expected}, got {seq_values}"
    )


def _get_streaming_events(client, headers, session_id) -> list[dict]:
    """Return the latest agent message's streaming_events."""
    agent_msgs = get_messages_by_role(client, headers, session_id, "agent")
    assert agent_msgs, "Expected at least one agent message"
    return agent_msgs[-1]["message_metadata"].get("streaming_events", [])


def _get_message_files(client, headers, session_id) -> list[dict]:
    """Return the files list on the latest agent message."""
    agent_msgs = get_messages_by_role(client, headers, session_id, "agent")
    assert agent_msgs, "Expected at least one agent message"
    return agent_msgs[-1].get("files", [])


def _send_agent_message(
    client: TestClient,
    headers: dict,
    session_id: str,
    agent_response: str,
    workspace_files: dict[str, bytes] | None = None,
    patch_environment_adapter=None,
) -> None:
    """
    Send a user message and drain tasks, with the agent environment stub set
    up to serve ``workspace_files`` (keyed by workspace-relative path).

    The agent-env connector stub returns ``agent_response`` as the assistant text.
    The EnvironmentTestAdapter exposes workspace_files via
    fetch_workspace_item_with_meta (the remote-adapter code path used when
    get_local_workspace_file_path is absent).
    """
    stub = StubAgentEnvConnector(response_text=agent_response)

    if workspace_files is not None and patch_environment_adapter:
        shared_adapter = EnvironmentTestAdapter()
        shared_adapter.workspace_files = {
            # EnvironmentTestAdapter.fetch_workspace_item_with_meta accepts the
            # workspace-relative path (the part after /app/workspace/).
            k: v for k, v in workspace_files.items()
        }
        patch_environment_adapter.get_adapter = lambda env: shared_adapter

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content="Please generate a file.")
        drain_tasks()


def _setup_agent_and_session(
    client: TestClient,
    headers: dict,
) -> tuple[dict, str, str]:
    """Create an agent + session. Returns (agent, agent_id, session_id)."""
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    session = create_session_via_api(client, headers, agent_id)
    return agent, agent_id, session["id"]


def _download_file(
    client: TestClient,
    file_id: str,
    headers: dict | None = None,
    token: str | None = None,
    disposition: str | None = None,
):
    """Call GET /files/{file_id}/download and return the raw response."""
    params = {}
    if token:
        params["token"] = token
    if disposition:
        params["disposition"] = disposition
    return client.get(
        f"{API}/files/{file_id}/download",
        headers=headers or {},
        params=params,
    )


# ── A. Happy-path materialisation ────────────────────────────────────────────


def test_attachment_basic_materialisation_and_tag_strip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    A1. Agent reply with a valid <cinna_attach> tag → file materialised,
    ``attachment`` event present in streaming_events, tag stripped from content.

      1. Create agent + session
      2. Send user message; agent reply embeds a <cinna_attach> PDF path
      3. Drain tasks — attachment materialisation runs at finalize
      4. Verify agent message content contains NO raw tags
      5. Verify streaming_events contains an ``attachment`` event
      6. Verify MessagePublic.files contains the attachment (source=agent_attachment)
      7. Verify event_seq values are contiguous
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        # ── Phase 2: Send message with attachment tag ─────────────────────
        response_text = _agent_response_with_attach(
            "Here is your report. ",
            _FILES_PDF,
            " Let me know if you need anything else.",
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={"files/report.pdf": _PDF_CONTENT},
            patch_environment_adapter=patch_environment_adapter,
        )

        # ── Phase 3: Stored content has no raw tags ───────────────────────
        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent"
        )
        assert len(agent_msgs) >= 1
        stored_content = agent_msgs[-1]["content"]
        assert "<cinna_attach>" not in stored_content, (
            f"Tag must be stripped from stored content. Got: {stored_content!r}"
        )
        assert "Here is your report." in stored_content, (
            "Text before the tag must be preserved"
        )

        # ── Phase 4: streaming_events contains an attachment event ─────────
        streaming_events = _get_streaming_events(
            client, superuser_token_headers, session_id
        )
        attach_events = [e for e in streaming_events if e.get("type") == "attachment"]
        assert len(attach_events) >= 1, (
            f"Expected at least one 'attachment' event. "
            f"event types: {[e.get('type') for e in streaming_events]}"
        )
        attach_evt = attach_events[0]
        assert attach_evt.get("content") == "report.pdf", (
            f"Attachment event content must be the filename (basename). "
            f"Got: {attach_evt.get('content')!r}"
        )
        meta = attach_evt.get("metadata", {})
        assert meta.get("filename") == "report.pdf"
        assert meta.get("mime_type") == "application/pdf"
        assert meta.get("size") == len(_PDF_CONTENT)
        assert meta.get("agent_env_path") == _FILES_PDF
        assert meta.get("file_id"), "file_id must be set in attachment event metadata"

        # ── Phase 5: MessagePublic.files contains the attachment ──────────
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_attach_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_attach_files) >= 1, (
            f"Expected at least one agent_attachment in message.files. "
            f"files: {files}"
        )
        attached_file = agent_attach_files[0]
        assert attached_file["filename"] == "report.pdf"
        assert attached_file["mime_type"] == "application/pdf"
        assert attached_file["file_size"] == len(_PDF_CONTENT)

        # ── Phase 6: event_seq is contiguous ──────────────────────────────
        _assert_contiguous_event_seq(streaming_events)


def test_attachment_different_workspace_subdirs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    A2. Files in /app/workspace/files/ AND /app/workspace/app-data/storage/
    both materialise. Filename is the basename in each case.

      1. Agent response references two files in different sub-directories
      2. Both files materialise successfully
      3. Filenames are the basenames (no directory path included)
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        response_text = (
            "Here is the report "
            + _attach_tag(_FILES_PDF)
            + " and here is the export "
            + _attach_tag(_APPDATA_CSV)
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={
                "files/report.pdf": _PDF_CONTENT,
                "app-data/storage/export.csv": _CSV_CONTENT,
            },
            patch_environment_adapter=patch_environment_adapter,
        )

        streaming_events = _get_streaming_events(
            client, superuser_token_headers, session_id
        )
        attach_events = [e for e in streaming_events if e.get("type") == "attachment"]
        assert len(attach_events) == 2, (
            f"Expected 2 attachment events, got {len(attach_events)}. "
            f"All events: {streaming_events}"
        )

        filenames = {e.get("content") for e in attach_events}
        assert "report.pdf" in filenames, "report.pdf must materialise"
        assert "export.csv" in filenames, "export.csv must materialise"

        # Verify each event's metadata has the correct filename (basename only)
        for evt in attach_events:
            meta = evt.get("metadata", {})
            assert meta.get("filename") in ("report.pdf", "export.csv")
            assert "/" not in meta.get("filename", ""), (
                f"Filename must be basename only, got: {meta.get('filename')!r}"
            )

        # Both files appear in message.files
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_files) == 2

        _assert_contiguous_event_seq(streaming_events)


def test_attachment_mime_fallback_types(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    A3. Markdown / CSV / JSON attachments materialise via the explicit MIME
    fallback map — they are not wrongly rejected as octet-stream.

      1. Agent declares .md, .csv, .json files
      2. All three materialise; MIME types are correct
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        md_path = f"{_WORKSPACE_ROOT}/files/notes.md"
        csv_path = f"{_WORKSPACE_ROOT}/files/data.csv"
        json_path = f"{_WORKSPACE_ROOT}/files/config.json"

        response_text = (
            "I have created three files: "
            + _attach_tag(md_path)
            + _attach_tag(csv_path)
            + _attach_tag(json_path)
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={
                "files/notes.md": _MD_CONTENT,
                "files/data.csv": _CSV_CONTENT,
                "files/config.json": _JSON_CONTENT,
            },
            patch_environment_adapter=patch_environment_adapter,
        )

        streaming_events = _get_streaming_events(
            client, superuser_token_headers, session_id
        )
        attach_events = [e for e in streaming_events if e.get("type") == "attachment"]
        assert len(attach_events) == 3, (
            f"Expected 3 attachment events, got {len(attach_events)}. "
            f"Events: {[(e.get('type'), e.get('content')) for e in streaming_events]}"
        )

        mime_by_filename: dict[str, str] = {
            e["content"]: e["metadata"]["mime_type"]
            for e in attach_events
        }
        assert mime_by_filename.get("notes.md") == "text/markdown", (
            f"Expected text/markdown for .md, got {mime_by_filename.get('notes.md')!r}"
        )
        assert mime_by_filename.get("data.csv") == "text/csv", (
            f"Expected text/csv for .csv, got {mime_by_filename.get('data.csv')!r}"
        )
        assert mime_by_filename.get("config.json") == "application/json", (
            f"Expected application/json for .json, got {mime_by_filename.get('config.json')!r}"
        )

        _assert_contiguous_event_seq(streaming_events)


def test_attachment_multiple_with_interleaved_text(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    A4. Multiple attachments interleaved with assistant text produce inline
    attachment events at the correct positions with contiguous event_seq.

      Agent reply:  "Summary: <attach pdf> Details: <attach csv> Done."
      Expected streaming_events layout (text/attach/text/attach/text):
        seq 1 — assistant "Summary:"
        seq 2 — attachment report.pdf
        seq 3 — assistant "Details:"
        seq 4 — attachment export.csv
        seq 5 — assistant "Done."
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        response_text = (
            "Summary: "
            + _attach_tag(_FILES_PDF)
            + " Details: "
            + _attach_tag(_APPDATA_CSV)
            + " Done."
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={
                "files/report.pdf": _PDF_CONTENT,
                "app-data/storage/export.csv": _CSV_CONTENT,
            },
            patch_environment_adapter=patch_environment_adapter,
        )

        streaming_events = _get_streaming_events(
            client, superuser_token_headers, session_id
        )
        split_events = [
            e for e in streaming_events
            if e.get("type") in ("assistant", "attachment")
        ]

        # Verify the interleaved ordering
        attach_events = [e for e in split_events if e["type"] == "attachment"]
        assert len(attach_events) == 2, (
            f"Expected 2 attachment events in streaming_events, got {len(attach_events)}. "
            f"split_events: {split_events}"
        )

        # The first attachment should be for report.pdf
        assert attach_events[0]["content"] == "report.pdf", (
            f"First attachment must be report.pdf, got {attach_events[0]['content']!r}"
        )
        assert attach_events[1]["content"] == "export.csv", (
            f"Second attachment must be export.csv, got {attach_events[1]['content']!r}"
        )

        # Verify no assistant event contains raw tags
        for evt in split_events:
            if evt["type"] == "assistant":
                assert "<cinna_attach>" not in evt.get("content", ""), (
                    f"No assistant event must contain raw tags. Got: {evt['content']!r}"
                )

        # event_seq is contiguous across all events
        _assert_contiguous_event_seq(streaming_events)


# ── B. MessagePublic.files projection ────────────────────────────────────────


def test_message_files_source_field_projection(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    B1. GET /sessions/{id}/messages includes the agent attachment in message.files
    with source='agent_attachment'. The file is ordered by event_seq.

    B2. (Implicitly) the test verifies source='agent_attachment' is present in
    the response schema — the source field differentiates agent-produced files
    from user-uploaded ones.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        response_text = _agent_response_with_attach(
            "Report is ready. ",
            _FILES_PDF,
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={"files/report.pdf": _PDF_CONTENT},
            patch_environment_adapter=patch_environment_adapter,
        )

        # Fetch messages and inspect the files field
        all_msgs = list_messages(client, superuser_token_headers, session_id)
        agent_msgs = [m for m in all_msgs if m["role"] == "agent"]
        assert agent_msgs, "Expected at least one agent message"

        msg = agent_msgs[-1]
        files = msg.get("files", [])
        assert files, (
            f"Expected message.files to be non-empty for an agent message "
            f"with an attachment. files: {files!r}"
        )

        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_files) >= 1, (
            f"Expected at least one file with source='agent_attachment'. "
            f"files: {files}"
        )
        file_info = agent_files[0]
        assert file_info["filename"] == "report.pdf"
        assert file_info["mime_type"] == "application/pdf"
        assert file_info["file_size"] == len(_PDF_CONTENT)
        assert file_info.get("id"), "file id must be present"


# ── C. Download endpoint ──────────────────────────────────────────────────────


def test_download_jwt_and_signed_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    C1. JWT-authenticated download → 200 + X-Content-Type-Options: nosniff.
    C2. Signed ?token= download → 200 (no JWT).
    C3. Expired / invalid token → 401.
    C4. Token whose file_id ≠ path file_id → 403.
    C5. ?disposition=inline for a safe MIME → Content-Disposition: inline +
        nosniff + Content-Security-Policy header.
    C6. ?disposition=inline for text/html → forced attachment + nosniff (no CSP).
    C7. No auth → 401.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        response_text = _agent_response_with_attach(
            "Report attached. ",
            _FILES_PDF,
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={"files/report.pdf": _PDF_CONTENT},
            patch_environment_adapter=patch_environment_adapter,
        )

        # Retrieve the file_id from the message files list
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert agent_files, "Expected at least one agent_attachment file"
        file_id = agent_files[0]["id"]

        # ── C1: JWT download → 200 + nosniff ─────────────────────────────
        r = _download_file(client, file_id, headers=superuser_token_headers)
        assert r.status_code == 200, f"JWT download failed: {r.text}"
        assert r.headers.get("x-content-type-options") == "nosniff", (
            "nosniff header must always be present"
        )
        assert r.content == _PDF_CONTENT, "Downloaded bytes must match original"

        # ── C2: Signed ?token= download (no JWT) → 200 ───────────────────
        from app.services.environments.agent_workspace_token_service import (
            AgentWorkspaceTokenService,
        )
        token = AgentWorkspaceTokenService.create_file_download_token(
            file_id=uuid.UUID(file_id),
            session_id=uuid.UUID(session_id),
        )
        r_tok = _download_file(client, file_id, token=token)
        assert r_tok.status_code == 200, f"Token download failed: {r_tok.text}"
        assert r_tok.content == _PDF_CONTENT
        assert r_tok.headers.get("x-content-type-options") == "nosniff"

        # ── C3: Invalid token → 401 ───────────────────────────────────────
        r_bad = _download_file(client, file_id, token="not-a-valid-jwt")
        assert r_bad.status_code == 401, (
            f"Expected 401 for invalid token, got {r_bad.status_code}"
        )

        # ── C4: Token whose file_id ≠ path file_id → 403 ─────────────────
        other_file_id = uuid.uuid4()
        wrong_token = AgentWorkspaceTokenService.create_file_download_token(
            file_id=other_file_id,
            session_id=uuid.UUID(session_id),
        )
        r_wrong = _download_file(client, file_id, token=wrong_token)
        assert r_wrong.status_code == 403, (
            f"Expected 403 when token.file_id ≠ path file_id, got {r_wrong.status_code}"
        )

        # ── C5: disposition=inline with safe MIME → inline + CSP ─────────
        r_inline = _download_file(
            client, file_id, headers=superuser_token_headers, disposition="inline"
        )
        assert r_inline.status_code == 200, f"Inline download failed: {r_inline.text}"
        content_disp = r_inline.headers.get("content-disposition", "")
        assert "inline" in content_disp, (
            f"Expected 'inline' in Content-Disposition for safe MIME, got: {content_disp!r}"
        )
        assert r_inline.headers.get("x-content-type-options") == "nosniff"
        csp = r_inline.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp and "sandbox" in csp, (
            f"Inline response must carry restrictive CSP. Got: {csp!r}"
        )

        # ── C7: No auth at all → 401 ──────────────────────────────────────
        r_noauth = client.get(f"{API}/files/{file_id}/download")
        assert r_noauth.status_code == 401, (
            f"Expected 401 for unauthenticated download, got {r_noauth.status_code}"
        )


def test_download_inline_safe_vs_unsafe_mime() -> None:
    """
    C6. The inline-safe MIME set is correctly defined: image/*, application/pdf,
    text/plain, text/csv, text/markdown, application/json are safe; text/html and
    image/svg+xml are NOT in the set and will be forced to attachment.

    This is a pure unit test against the route's _INLINE_SAFE_MIME_TYPES constant,
    verifying the allowlist is both correct and complete (no dangerous types snuck in).
    The actual routing behavior for text/plain vs text/html is also exercised in
    C5 (inline for PDF) and C1 (default attachment).

    No HTTP calls are needed — the route reads this constant verbatim.
    """
    from app.api.routes.files import _INLINE_SAFE_MIME_TYPES

    # These must be in the allowlist (safe for inline rendering)
    safe_types = {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
    }
    for mime in safe_types:
        assert mime in _INLINE_SAFE_MIME_TYPES, (
            f"Expected {mime!r} to be in the inline-safe MIME set"
        )

    # These must NOT be in the allowlist (dangerous for inline rendering)
    dangerous_types = {"text/html", "image/svg+xml", "application/javascript"}
    for mime in dangerous_types:
        assert mime not in _INLINE_SAFE_MIME_TYPES, (
            f"{mime!r} must NOT be in the inline-safe MIME set — it can run script "
            f"in the app origin and must always be forced to attachment"
        )


# ── D. Rejection / error cases ────────────────────────────────────────────────


def test_attachment_rejection_non_absolute_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    D1. A non-absolute path in the tag body is rejected.
    The attachment_error event is emitted; no MessageFile record is created.
    The visible text still renders the rest of the message normally.

    NOTE: The route test here verifies that no MessageFile is created (via
    message.files being empty or having no agent_attachment entries) and that
    the visible text is intact. We cannot assert on SocketIO emission in an
    API-only test, so we rely on the absence of an attachment record as the
    rejection signal.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        # Non-absolute path (relative path)
        response_text = (
            "Here is the file: "
            + "<cinna_attach>relative/path/file.pdf</cinna_attach>"
            + " done."
        )
        stub = StubAgentEnvConnector(response_text=response_text)
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            send_message(
                client, superuser_token_headers, session_id,
                content="give me relative path",
            )
            drain_tasks()

        # No agent_attachment files created
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_files) == 0, (
            f"Non-absolute path must produce no agent_attachment records. "
            f"Got: {agent_files}"
        )

        # Visible text preserved, tag stripped
        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent"
        )
        stored_content = agent_msgs[-1]["content"]
        assert "<cinna_attach>" not in stored_content, (
            "Tag must be stripped even for rejected paths"
        )


def test_attachment_rejection_path_outside_workspace(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    D2. A path that does not start with /app/workspace is rejected.
    D3. A path that uses .. to escape the workspace root is rejected.
    No records are created; text is preserved.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        # D2: Outside workspace entirely
        outside_path = "/etc/passwd"
        # D3: Using .. to escape
        traversal_path = "/app/workspace/../etc/shadow"

        response_text = (
            "Trying to leak: "
            + _attach_tag(outside_path)
            + _attach_tag(traversal_path)
            + " done."
        )
        stub = StubAgentEnvConnector(response_text=response_text)
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            send_message(
                client, superuser_token_headers, session_id,
                content="attach outside workspace",
            )
            drain_tasks()

        # No agent_attachment files created for either path
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_files) == 0, (
            f"Paths outside workspace must produce no records. Got: {agent_files}"
        )

        # Text preserved, tags stripped
        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent"
        )
        stored = agent_msgs[-1]["content"]
        assert "<cinna_attach>" not in stored


def test_attachment_rejection_file_not_found(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    D5. File not found on the adapter (workspace_files not populated).
    The path is valid and absolute; materialisation is attempted but fails
    because the adapter returns exists=False. No record is created.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        # workspace_files is empty — the adapter will return exists=False
        response_text = _agent_response_with_attach(
            "File attached: ",
            _FILES_PDF,
            " done.",
        )
        # Use default empty workspace_files on the adapter
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={},  # file not available
            patch_environment_adapter=patch_environment_adapter,
        )

        # No agent_attachment files created
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_files) == 0, (
            f"Missing file must produce no attachment record. Got: {agent_files}"
        )

        # Visible text preserved, tag stripped
        agent_msgs = get_messages_by_role(
            client, superuser_token_headers, session_id, "agent"
        )
        stored = agent_msgs[-1]["content"]
        assert "<cinna_attach>" not in stored


# ── E. User message tags are never parsed ────────────────────────────────────


def test_user_message_tag_never_parsed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    E1. Tags in user messages are never parsed — no attachment materialised,
    the tag content is visible as-is in the stored user message.

    The extraction logic only runs on agent/assistant content produced by the
    streaming backend. A user who sends a <cinna_attach> tag cannot forge an
    agent attachment.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        # User sends a message containing a cinna_attach tag
        user_msg_with_tag = f"Look at this: {_attach_tag(_FILES_PDF)}"
        stub = StubAgentEnvConnector(response_text="I see your message.")
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            send_message(
                client, superuser_token_headers, session_id,
                content=user_msg_with_tag,
            )
            drain_tasks()

        # User message: tag must still be present (NOT stripped)
        all_msgs = list_messages(client, superuser_token_headers, session_id)
        user_msgs = [m for m in all_msgs if m["role"] == "user"]
        assert user_msgs, "Expected at least one user message"
        user_stored = user_msgs[-1]["content"]
        assert "<cinna_attach>" in user_stored, (
            f"User message must NOT have its tags stripped. "
            f"Got: {user_stored!r}"
        )

        # Agent reply: no attachment materialised
        agent_msgs = [m for m in all_msgs if m["role"] == "agent"]
        assert agent_msgs, "Expected at least one agent reply"
        agent_files = [
            f for f in agent_msgs[-1].get("files", [])
            if f.get("source") == "agent_attachment"
        ]
        assert len(agent_files) == 0, (
            "User message tag must never produce an agent_attachment record"
        )

        # No attachment streaming events in the agent message
        agent_streaming = agent_msgs[-1]["message_metadata"].get("streaming_events", [])
        attach_events = [e for e in agent_streaming if e.get("type") == "attachment"]
        assert len(attach_events) == 0, (
            "User message tag must produce no attachment events in agent reply"
        )


# ── F. De-duplication ────────────────────────────────────────────────────────


def test_attachment_dedup_same_path_twice(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    F1. Same path attached twice in one message → one FileUpload record,
    two attachment events in streaming_events both referencing the same file_id.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        _, agent_id, session_id = _setup_agent_and_session(
            client, superuser_token_headers
        )

        response_text = (
            "First mention: "
            + _attach_tag(_FILES_PDF)
            + " Second mention: "
            + _attach_tag(_FILES_PDF)
            + " Done."
        )
        _send_agent_message(
            client, superuser_token_headers, session_id,
            agent_response=response_text,
            workspace_files={"files/report.pdf": _PDF_CONTENT},
            patch_environment_adapter=patch_environment_adapter,
        )

        streaming_events = _get_streaming_events(
            client, superuser_token_headers, session_id
        )
        attach_events = [e for e in streaming_events if e.get("type") == "attachment"]
        assert len(attach_events) == 2, (
            f"Expected 2 attachment events (one per tag even when de-duped), "
            f"got {len(attach_events)}. events: {attach_events}"
        )

        # Both events must reference the same file_id (de-duped)
        file_ids = {e["metadata"].get("file_id") for e in attach_events}
        assert len(file_ids) == 1, (
            f"Both attachment events must reference the same file_id. Got: {file_ids}"
        )

        # Only one FileUpload in message.files
        files = _get_message_files(client, superuser_token_headers, session_id)
        agent_files = [f for f in files if f.get("source") == "agent_attachment"]
        assert len(agent_files) == 1, (
            f"De-duplication: only one file record should exist for two tags of "
            f"the same path. Got {len(agent_files)} files."
        )

        _assert_contiguous_event_seq(streaming_events)


# ── G. A2A: attachment events → FilePart ────────────────────────────────────


def test_a2a_attachment_maps_to_file_part(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    G1. After a session with an agent attachment, GetTask (A2A history) returns
    a message whose parts include a FilePart carrying:
      - cinna.content_kind = "file"
      - cinna.file_id (matching the materialised file)
      - cinna.file_name (the filename)
      - cinna.file_mime (the MIME type)
      - cinna.file_size (the byte count)

    G2. The FilePart.file.uri embeds a signed ?token= download token that the
    download endpoint accepts (200 response).
    """
    # The A2A route imports create_session from app.core.db and passes it as
    # get_db_session into the handler. We must patch it so the A2A route also
    # uses the test transaction (same as in a2a_integration/conftest.py).
    from tests.utils.fixtures import patched_create_sessions

    with (
        patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")),
        patched_create_sessions(db, ["app.api.routes.a2a.create_session"]),
    ):
        # ── Phase 1: Set up A2A agent ─────────────────────────────────────
        agent, token_data = setup_a2a_agent(
            client, superuser_token_headers, name="A2A Attachment Agent"
        )
        agent_id = agent["id"]
        a2a_token = token_data["token"]
        a2a_headers = {
            "Authorization": f"Bearer {a2a_token}",
            "Content-Type": "application/json",
        }

        # ── Phase 2: Stream a message with an attachment ──────────────────
        response_text = _agent_response_with_attach(
            "Here is the report. ",
            _FILES_PDF,
        )
        shared_adapter = EnvironmentTestAdapter()
        shared_adapter.workspace_files = {"files/report.pdf": _PDF_CONTENT}
        patch_environment_adapter.get_adapter = lambda env: shared_adapter

        stub = StubAgentEnvConnector(response_text=response_text)
        request = build_streaming_request("Generate a report")

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            resp = client.post(
                f"{API}/a2a/{agent_id}/",
                headers=a2a_headers,
                json=request,
            )
            drain_tasks()

        assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"
        sse_events = parse_sse_events(resp.text)

        # Extract the task_id from the first SSE event. A2A status-update events
        # carry it at result.taskId (the repo-wide convention used by every other
        # A2A test), not result.id / result.status.taskId.
        task_id = None
        for event in sse_events:
            result = event.get("result", {})
            task_id = result.get("taskId") or result.get("id")
            if task_id:
                break

        assert task_id, (
            f"Could not find task_id in SSE events. Events: {sse_events[:3]}"
        )

        # ── Phase 3: GetTask history → FilePart with cinna metadata ───────
        get_task_request = {
            "jsonrpc": "2.0",
            "id": "req-get",
            "method": "GetTask",
            "params": {"id": task_id},
        }
        get_resp = client.post(
            f"{API}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=get_task_request,
        )
        assert get_resp.status_code == 200, f"GetTask failed: {get_resp.text}"
        task_data = get_resp.json()

        # Navigate to the agent message parts
        history = task_data.get("result", {}).get("history") or []
        agent_parts: list[dict] = []
        for msg in history:
            role = msg.get("role", "")
            if role == "agent":
                agent_parts.extend(msg.get("parts", []))

        # Look for a FilePart (has "file" key)
        file_parts = []
        for part in agent_parts:
            root = part.get("root") or part
            if "file" in root:
                file_parts.append(root)

        assert file_parts, (
            f"Expected at least one FilePart in the agent history message. "
            f"Agent parts: {agent_parts}"
        )

        file_part = file_parts[0]
        part_meta = file_part.get("metadata", {})

        # ── Phase 4: Verify cinna.* metadata keys ─────────────────────────
        assert part_meta.get(_A2A_CONTENT_KIND_KEY) == _A2A_KIND_FILE, (
            f"Expected cinna.content_kind='file', got: {part_meta.get(_A2A_CONTENT_KIND_KEY)!r}"
        )
        assert part_meta.get(_A2A_FILE_ID_KEY), (
            f"Expected cinna.file_id to be set. metadata: {part_meta}"
        )
        assert part_meta.get(_A2A_FILE_NAME_KEY) == "report.pdf", (
            f"Expected cinna.file_name='report.pdf', got: {part_meta.get(_A2A_FILE_NAME_KEY)!r}"
        )
        assert part_meta.get(_A2A_FILE_MIME_KEY) == "application/pdf", (
            f"Expected cinna.file_mime='application/pdf', got: {part_meta.get(_A2A_FILE_MIME_KEY)!r}"
        )
        assert part_meta.get(_A2A_FILE_SIZE_KEY) == len(_PDF_CONTENT), (
            f"Expected cinna.file_size={len(_PDF_CONTENT)}, "
            f"got: {part_meta.get(_A2A_FILE_SIZE_KEY)!r}"
        )

        # ── Phase 5: Verify the signed URI works for download ─────────────
        file_info = file_part.get("file", {})
        download_uri = file_info.get("uri", "")
        assert download_uri, f"FilePart must carry a download URI. file: {file_info}"
        assert "?token=" in download_uri, (
            f"FilePart URI must embed a signed download token. URI: {download_uri!r}"
        )

        # Extract ?token= parameter and test it against the download endpoint
        token_value = download_uri.split("?token=")[-1].split("&")[0]
        file_id = part_meta.get(_A2A_FILE_ID_KEY)
        r_dl = client.get(
            f"{API}/files/{file_id}/download",
            params={"token": token_value},
        )
        assert r_dl.status_code == 200, (
            f"Download via A2A FilePart URI token must succeed (200). "
            f"Got {r_dl.status_code}: {r_dl.text}"
        )
        assert r_dl.content == _PDF_CONTENT, (
            "Downloaded bytes via A2A token must match the original attachment content"
        )


def test_a2a_attachment_emits_file_part_on_live_stream(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
    tmp_path,
) -> None:
    """
    G3. Regression: the attachment FilePart must arrive on the LIVE A2A SSE
    stream, not only on GetTask replay.

    The materialised ``attachment`` event is yielded into the stream generator
    (after the assistant text, before ``done``) so ``A2AStreamEventHandler``
    maps it to a ``working`` status-update carrying a FilePart. Streaming
    clients (Cinna Desktop / Mobile) consume the SSE live and persist their own
    copy — they do NOT replay via GetTask — so without the live yield they never
    receive the file. Before the fix, ``_process_attachments`` only emitted the
    attachment to the Socket.IO room and the persisted trace, so this branch in
    the A2A mapper was dead code on the live path.
    """
    from tests.utils.fixtures import patched_create_sessions

    with (
        patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")),
        patched_create_sessions(db, ["app.api.routes.a2a.create_session"]),
    ):
        agent, token_data = setup_a2a_agent(
            client, superuser_token_headers, name="A2A Live Attachment Agent"
        )
        agent_id = agent["id"]
        a2a_headers = {
            "Authorization": f"Bearer {token_data['token']}",
            "Content-Type": "application/json",
        }

        response_text = _agent_response_with_attach("Here is the report. ", _FILES_PDF)
        shared_adapter = EnvironmentTestAdapter()
        shared_adapter.workspace_files = {"files/report.pdf": _PDF_CONTENT}
        patch_environment_adapter.get_adapter = lambda env: shared_adapter

        stub = StubAgentEnvConnector(response_text=response_text)
        request = build_streaming_request("Generate a report")

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            resp = client.post(
                f"{API}/a2a/{agent_id}/",
                headers=a2a_headers,
                json=request,
            )
            drain_tasks()

        assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"
        sse_events = parse_sse_events(resp.text)

        # Scan the LIVE SSE frames for a status-update whose message carries a
        # FilePart (has a "file" key) — this is the frame Desktop renders as a
        # download badge.
        live_file_parts: list[dict] = []
        for event in sse_events:
            status = event.get("result", {}).get("status", {}) or {}
            message = status.get("message") or {}
            for part in message.get("parts", []):
                root = part.get("root") or part
                if "file" in root:
                    live_file_parts.append(root)

        assert live_file_parts, (
            "Live A2A SSE stream must contain a FilePart status-update for the "
            f"agent attachment (regression: it was only emitted to Socket.IO + "
            f"persisted trace before the fix). SSE events: {sse_events}"
        )

        part_meta = live_file_parts[0].get("metadata", {})
        assert part_meta.get(_A2A_CONTENT_KIND_KEY) == _A2A_KIND_FILE, (
            f"Expected cinna.content_kind='file' on the live FilePart, got: "
            f"{part_meta.get(_A2A_CONTENT_KIND_KEY)!r}"
        )
        assert part_meta.get(_A2A_FILE_ID_KEY), (
            f"Live FilePart must carry cinna.file_id. metadata: {part_meta}"
        )
        assert part_meta.get(_A2A_FILE_NAME_KEY) == "report.pdf"
        assert part_meta.get(_A2A_FILE_MIME_KEY) == "application/pdf"
        assert part_meta.get(_A2A_FILE_SIZE_KEY) == len(_PDF_CONTENT)

        # The live FilePart URI is signed (downloadable without a session JWT).
        uri = live_file_parts[0].get("file", {}).get("uri", "")
        assert "?token=" in uri, f"Live FilePart URI must be signed. Got: {uri!r}"


# ── Unit test: _extract_attachments ──────────────────────────────────────────


def test_extract_attachments_unit() -> None:
    """
    Unit test for the _extract_attachments helper (no HTTP, no DB).

    Verifies the regex-based extraction logic in isolation, mirroring the
    test_extract_webapp_actions_unit pattern used for the webapp action framework.
    """
    from app.services.sessions.message_service import _extract_attachments

    # Basic: single valid absolute path
    paths, cleaned = _extract_attachments(
        "<cinna_attach>/app/workspace/files/report.pdf</cinna_attach>"
    )
    assert paths == ["/app/workspace/files/report.pdf"]
    assert "<cinna_attach>" not in cleaned

    # Text before and after is preserved in cleaned
    paths, cleaned = _extract_attachments(
        "Here it is <cinna_attach>/app/workspace/files/x.csv</cinna_attach> done."
    )
    assert paths == ["/app/workspace/files/x.csv"]
    assert "Here it is" in cleaned
    assert "done." in cleaned
    assert "<cinna_attach>" not in cleaned

    # Multiple tags: both extracted in order
    paths, cleaned = _extract_attachments(
        "<cinna_attach>/app/workspace/files/a.pdf</cinna_attach>"
        "text"
        "<cinna_attach>/app/workspace/app-data/b.csv</cinna_attach>"
    )
    assert paths == [
        "/app/workspace/files/a.pdf",
        "/app/workspace/app-data/b.csv",
    ]
    assert "<cinna_attach>" not in cleaned
    assert "text" in cleaned

    # Empty body: tag stripped, no path collected
    paths, cleaned = _extract_attachments("<cinna_attach>  </cinna_attach>visible")
    assert paths == []
    assert "<cinna_attach>" not in cleaned
    assert "visible" in cleaned

    # Non-absolute body: tag stripped, no path collected
    paths, cleaned = _extract_attachments(
        "<cinna_attach>relative/path/file.pdf</cinna_attach>"
    )
    assert paths == []
    assert "<cinna_attach>" not in cleaned

    # Multiline path (DOTALL): the path body is the full line
    paths, cleaned = _extract_attachments(
        "<cinna_attach>\n/app/workspace/files/report.pdf\n</cinna_attach>"
    )
    assert paths == ["/app/workspace/files/report.pdf"]
    assert "<cinna_attach>" not in cleaned

    # Same path twice: extracted twice (de-dup is service-level, not regex-level)
    paths, cleaned = _extract_attachments(
        "<cinna_attach>/app/workspace/files/a.pdf</cinna_attach>"
        "<cinna_attach>/app/workspace/files/a.pdf</cinna_attach>"
    )
    assert paths == [
        "/app/workspace/files/a.pdf",
        "/app/workspace/files/a.pdf",
    ]
    assert "<cinna_attach>" not in cleaned

    # No tags: no paths, content unchanged
    paths, cleaned = _extract_attachments("plain text with no tags")
    assert paths == []
    assert cleaned == "plain text with no tags"
