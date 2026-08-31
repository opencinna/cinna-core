"""
Unit tests for OpenCodeAdapter._event_session_id.

Tests the static helper that extracts the opencode session ID from a raw SSE
event dict so the SSE loop can demultiplex the serve-wide ``GET /global/event``
stream and drop events that belong to a different (e.g. orphaned) session.

End-to-end / API-observable behavior for this fix is covered in
``tests/api/agents/sessions/agents_session_delete_interrupt_test.py``.

Run:
    cd backend && python -m pytest tests/unit/test_opencode_session_filter.py -v
"""

import pytest

# sys.path to app_core_base is set by tests/unit/conftest.py
from core.server.adapters.opencode_sdk_adapter import OpenCodeAdapter

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

SESSION_A = "ses_aaaaaaaa_1111"
SESSION_B = "ses_bbbbbbbb_2222"


def _evt(event_type: str, properties: dict | None = None) -> dict:
    """Build a minimal raw event dict."""
    return {"type": event_type, "properties": properties or {}}


# ===========================================================================
# Extraction from properties.sessionID (primary path)
# ===========================================================================

class TestExtractionFromTopLevelSessionID:
    """Events where the session ID lives directly in properties.sessionID.

    Covers: session.idle, permission.asked, message.part.delta, and other
    shapes that embed sessionID at the top level of properties.
    """

    def test_session_idle_shape(self):
        """session.idle carries sessionID directly in properties."""
        event = _evt("session.idle", {"sessionID": SESSION_A})
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_session_status_busy(self):
        """session.status carries sessionID directly in properties."""
        event = _evt("session.status", {"sessionID": SESSION_A, "status": {"type": "busy"}})
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_message_part_delta_shape(self):
        """message.part.delta carries sessionID directly in properties."""
        event = _evt("message.part.delta", {
            "sessionID": SESSION_A,
            "messageID": "msg_001",
            "partID": "prt_001",
            "field": "text",
            "delta": "hello",
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_permission_asked_shape(self):
        """permission.asked carries sessionID directly in properties."""
        event = _evt("permission.asked", {
            "id": "per_001",
            "sessionID": SESSION_A,
            "permission": "external_directory",
            "patterns": ["/app/workspace/*"],
            "tool": {"messageID": "msg_001", "callID": "call_001"},
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_session_error_shape(self):
        """session.error carries sessionID directly in properties."""
        event = _evt("session.error", {
            "sessionID": SESSION_A,
            "error": "Model rate limit exceeded",
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_session_diff_shape(self):
        """session.diff carries sessionID directly in properties."""
        event = _evt("session.diff", {
            "sessionID": SESSION_A,
            "diff": [],
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_returns_session_b_when_present(self):
        """Returns whatever session ID is present, not just SESSION_A."""
        event = _evt("session.idle", {"sessionID": SESSION_B})
        assert OpenCodeAdapter._event_session_id(event) == SESSION_B


# ===========================================================================
# Fallback to properties.part.sessionID
# ===========================================================================

class TestFallbackToPartSessionID:
    """Events where the session ID is nested inside properties.part.sessionID.

    Covers: message.part.updated (running, completed, step-start/finish, etc.)
    """

    def test_message_part_updated_text_part(self):
        """message.part.updated (text part) nests sessionID inside part."""
        event = _evt("message.part.updated", {
            "part": {
                "id": "prt_text_001",
                "sessionID": SESSION_A,
                "messageID": "msg_001",
                "type": "text",
                "text": "Hello",
                "time": {"start": 1000},
            }
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_message_part_updated_tool_running(self):
        """message.part.updated (tool running) nests sessionID inside part."""
        event = _evt("message.part.updated", {
            "part": {
                "id": "prt_tool_001",
                "sessionID": SESSION_A,
                "messageID": "msg_001",
                "type": "tool",
                "callID": "call_001",
                "tool": "read",
                "state": {"status": "running", "input": {"filePath": "/app"}},
            }
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_message_part_updated_step_finish(self):
        """message.part.updated (step-finish) also nests sessionID inside part."""
        event = _evt("message.part.updated", {
            "part": {
                "id": "prt_sf_001",
                "sessionID": SESSION_A,
                "messageID": "msg_001",
                "type": "step-finish",
                "reason": "stop",
            }
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_part_session_id_takes_precedence_over_absent_top_level(self):
        """When properties.sessionID is absent, fallback to properties.part.sessionID."""
        # Explicitly omit sessionID at the top level
        event = {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "prt_001",
                    "sessionID": SESSION_B,
                    "messageID": "msg_001",
                    "type": "text",
                    "text": "",
                }
            },
        }
        assert OpenCodeAdapter._event_session_id(event) == SESSION_B

    def test_part_not_a_dict_does_not_crash(self):
        """If properties.part is not a dict (malformed), skip it gracefully."""
        event = _evt("message.part.updated", {
            "part": "not-a-dict",
        })
        # Should return None (no session id found) without raising
        result = OpenCodeAdapter._event_session_id(event)
        assert result is None

    def test_part_missing_session_id_falls_through_to_info(self):
        """If properties.part exists but has no sessionID, fall through to info."""
        event = _evt("message.part.updated", {
            "part": {"id": "prt_001", "type": "text"},
            "info": {"sessionID": SESSION_A},
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A


# ===========================================================================
# Fallback to properties.info.sessionID
# ===========================================================================

class TestFallbackToInfoSessionID:
    """Events where the session ID is nested inside properties.info.sessionID.

    Covers: message.updated snapshots (user/assistant message finalization).
    """

    def test_message_updated_assistant(self):
        """message.updated for an assistant message nests sessionID inside info."""
        event = _evt("message.updated", {
            "info": {
                "id": "msg_asst_001",
                "sessionID": SESSION_A,
                "role": "assistant",
                "time": {"created": 1000},
                "tokens": {"input": 100, "output": 50},
            }
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_message_updated_user(self):
        """message.updated for a user message also uses info.sessionID."""
        event = _evt("message.updated", {
            "info": {
                "id": "msg_user_001",
                "sessionID": SESSION_B,
                "role": "user",
                "time": {"created": 900},
            }
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_B

    def test_info_not_a_dict_does_not_crash(self):
        """If properties.info is not a dict (malformed), skip it gracefully."""
        event = _evt("message.updated", {"info": 42})
        result = OpenCodeAdapter._event_session_id(event)
        assert result is None

    def test_info_missing_session_id_returns_none(self):
        """If properties.info exists but has no sessionID, returns None."""
        event = _evt("message.updated", {
            "info": {"id": "msg_001", "role": "user"}
        })
        result = OpenCodeAdapter._event_session_id(event)
        assert result is None


# ===========================================================================
# Sessionless events — must return None
# ===========================================================================

class TestSessionlessEvents:
    """Server lifecycle and project events carry no session ID at all.

    These events must return None so the SSE loop lets them through unchanged
    — they are needed for the "first event triggers the POST" handshake.
    """

    def test_server_connected_returns_none(self):
        """server.connected has no session ID."""
        event = _evt("server.connected")
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_server_heartbeat_returns_none(self):
        """server.heartbeat has no session ID."""
        event = _evt("server.heartbeat")
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_project_updated_global_returns_none(self):
        """project.updated with properties.id='global' has no sessionID."""
        event = _evt("project.updated", {
            "id": "global",
            "worktree": "/",
            "time": {"created": 1000, "updated": 2000},
        })
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_project_updated_empty_properties_returns_none(self):
        """project.updated with empty properties dict has no sessionID."""
        event = _evt("project.updated", {})
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_empty_properties_returns_none(self):
        """An event with an empty properties dict has no session ID."""
        event = _evt("some.event", {})
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_missing_properties_key_returns_none(self):
        """An event with no 'properties' key at all returns None."""
        event = {"type": "some.event"}
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_none_properties_returns_none(self):
        """An event with properties=None returns None."""
        event = {"type": "some.event", "properties": None}
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_non_dict_properties_returns_none(self):
        """An event where 'properties' is a string (malformed) returns None."""
        event = {"type": "some.event", "properties": "malformed"}
        assert OpenCodeAdapter._event_session_id(event) is None

    def test_empty_session_id_string_returns_none(self):
        """An event where properties.sessionID is an empty string is falsy → None."""
        event = _evt("session.idle", {"sessionID": ""})
        # Empty string is falsy — treated as absent, so should return None
        assert OpenCodeAdapter._event_session_id(event) is None


# ===========================================================================
# Priority: properties.sessionID wins over nested paths
# ===========================================================================

class TestExtractionPriority:
    """properties.sessionID always takes priority over the part/info fallbacks."""

    def test_top_level_wins_over_part(self):
        """When both properties.sessionID and properties.part.sessionID are set,
        the top-level wins (first check in the method)."""
        event = _evt("message.part.updated", {
            "sessionID": SESSION_A,
            "part": {"sessionID": SESSION_B, "id": "prt_001"},
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_top_level_wins_over_info(self):
        """When both properties.sessionID and properties.info.sessionID are set,
        the top-level wins."""
        event = _evt("message.updated", {
            "sessionID": SESSION_A,
            "info": {"sessionID": SESSION_B},
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A

    def test_part_wins_over_info(self):
        """When only properties.part.sessionID and properties.info.sessionID are
        set (no top-level), part wins (checked before info)."""
        event = _evt("some.event", {
            "part": {"sessionID": SESSION_A},
            "info": {"sessionID": SESSION_B},
        })
        assert OpenCodeAdapter._event_session_id(event) == SESSION_A
