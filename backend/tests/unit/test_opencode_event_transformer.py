"""
Unit tests for OpenCodeEventTransformer.

Tests event translation from raw OpenCode SSE events to SDKEvent objects
using real event data captured from opencode serve 1.2.x sessions.

Run: cd backend && python -m pytest tests/unit/test_opencode_event_transformer.py -v
"""

import json
from pathlib import Path

import pytest

# sys.path setup is handled by tests/unit/conftest.py
from core.server.adapters.opencode_event_transformer import OpenCodeEventTransformer
from core.server.sdk_utils import SessionEventLogger
from core.server.adapters.base import SDKEvent, SDKEventType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SESSION_ID = "ses_test_session_001"


@pytest.fixture
def transformer(tmp_path):
    """Create an event transformer with logging disabled."""
    return OpenCodeEventTransformer(str(tmp_path))


@pytest.fixture
def transformer_with_logging(tmp_path):
    """Create an event transformer with logging enabled."""
    import os
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DUMP_LLM_SESSION", "true")
        t = OpenCodeEventTransformer(str(tmp_path))
    return t, tmp_path


# ---------------------------------------------------------------------------
# Raw event fixtures (from real opencode serve 1.2.27 captures)
# ---------------------------------------------------------------------------

def _evt(event_type: str, properties: dict | None = None) -> dict:
    """Helper to build a raw event dict."""
    return {"type": event_type, "properties": properties or {}}


# -- Server lifecycle events ------------------------------------------------

EVT_SERVER_CONNECTED = _evt("server.connected")
EVT_SERVER_HEARTBEAT = _evt("server.heartbeat")

# -- Session events ---------------------------------------------------------

EVT_SESSION_UPDATED = _evt("session.updated", {
    "info": {
        "id": SESSION_ID, "slug": "test-session",
        "title": "Test session", "version": "1.2.27",
        "time": {"created": 1000, "updated": 2000},
    }
})

EVT_SESSION_STATUS_BUSY = _evt("session.status", {
    "sessionID": SESSION_ID, "status": {"type": "busy"}
})

EVT_SESSION_STATUS_IDLE = _evt("session.status", {
    "sessionID": SESSION_ID, "status": {"type": "idle"}
})

EVT_SESSION_IDLE = _evt("session.idle", {"sessionID": SESSION_ID})

EVT_SESSION_DIFF = _evt("session.diff", {
    "sessionID": SESSION_ID, "diff": []
})

# -- Message events ---------------------------------------------------------

EVT_MESSAGE_UPDATED_USER = _evt("message.updated", {
    "info": {
        "id": "msg_user_001", "sessionID": SESSION_ID,
        "role": "user", "time": {"created": 1000},
    }
})

EVT_MESSAGE_UPDATED_ASSISTANT = _evt("message.updated", {
    "info": {
        "id": "msg_asst_001", "sessionID": SESSION_ID,
        "role": "assistant", "time": {"created": 1001},
        "tokens": {"input": 100, "output": 50},
    }
})

# -- Part events: text ------------------------------------------------------

EVT_TEXT_PART_START = _evt("message.part.updated", {
    "part": {
        "id": "prt_text_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "text",
        "text": "", "time": {"start": 1000},
    }
})

def _text_delta(part_id: str, delta: str) -> dict:
    return _evt("message.part.delta", {
        "sessionID": SESSION_ID, "messageID": "msg_asst_001",
        "partID": part_id, "field": "text", "delta": delta,
    })

EVT_TEXT_PART_COMPLETE = _evt("message.part.updated", {
    "part": {
        "id": "prt_text_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "text",
        "text": "Hello, world!\nHow are you?",
        "time": {"start": 1000, "end": 2000},
    }
})

# -- Part events: reasoning -------------------------------------------------

EVT_REASONING_PART_START = _evt("message.part.updated", {
    "part": {
        "id": "prt_reason_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "reasoning",
        "text": "", "time": {"start": 1000},
    }
})

EVT_REASONING_DELTA = _text_delta("prt_reason_001", "thinking about this...")

EVT_REASONING_PART_COMPLETE = _evt("message.part.updated", {
    "part": {
        "id": "prt_reason_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "reasoning",
        "text": "thinking about this...",
        "time": {"start": 1000, "end": 2000},
    }
})

# -- Part events: tool (opencode 1.2.x structure) ---------------------------

EVT_TOOL_PENDING = _evt("message.part.updated", {
    "part": {
        "id": "prt_tool_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "tool",
        "callID": "call_001", "tool": "read",
        "state": {"status": "pending", "input": {}, "raw": ""},
    }
})

EVT_TOOL_RUNNING = _evt("message.part.updated", {
    "part": {
        "id": "prt_tool_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "tool",
        "callID": "call_001", "tool": "read",
        "state": {
            "status": "running",
            "input": {"filePath": "/app/workspace/scripts"},
            "time": {"start": 1000},
        },
    }
})

EVT_TOOL_COMPLETED = _evt("message.part.updated", {
    "part": {
        "id": "prt_tool_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "tool",
        "callID": "call_001", "tool": "read",
        "state": {
            "status": "completed",
            "input": {"filePath": "/app/workspace/scripts"},
            "output": "init_db.py\ntask_manager.py",
            "time": {"start": 1000, "end": 2000},
        },
    }
})

EVT_TOOL_ERROR = _evt("message.part.updated", {
    "part": {
        "id": "prt_tool_002", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "tool",
        "callID": "call_002", "tool": "bash",
        "state": {
            "status": "error",
            "input": {"command": "rm -rf /"},
            "error": "Permission denied",
        },
    }
})

# -- Part events: step lifecycle --------------------------------------------

EVT_STEP_START = _evt("message.part.updated", {
    "part": {
        "id": "prt_step_001", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "step-start",
    }
})

EVT_STEP_FINISH_STOP = _evt("message.part.updated", {
    "part": {
        "id": "prt_step_002", "sessionID": SESSION_ID,
        "messageID": "msg_asst_001", "type": "step-finish",
        "reason": "stop",
        "tokens": {"total": 1000, "input": 500, "output": 100},
    }
})

# -- Permission event -------------------------------------------------------

EVT_PERMISSION_ASKED = _evt("permission.asked", {
    "id": "per_001", "sessionID": SESSION_ID,
    "permission": "external_directory",
    "patterns": ["/app/workspace/scripts/*"],
    "tool": {"messageID": "msg_asst_001", "callID": "call_001"},
})

# -- Question asked (LLM called the built-in `question` tool) ---------------

EVT_QUESTION_ASKED = _evt("question.asked", {
    "id": "que_001",
    "sessionID": SESSION_ID,
    "questions": [{
        "question": "Which version do you want?",
        "header": "Table type",
        "options": [
            {"label": "1. Every country",
             "description": "Add capital for all listed countries"},
            {"label": "2. Theme groups",
             "description": "Add capital for grouped themes only"},
        ],
        "multiple": False,
        "custom": True,
    }],
    "tool": {"messageID": "msg_asst_002", "callID": "call_q001"},
})

# The two tool-part events that bracket `question.asked` — both should be
# suppressed in favour of the `question.asked` handler.
EVT_QUESTION_TOOL_PENDING = _evt("message.part.updated", {
    "part": {
        "id": "prt_q1", "sessionID": SESSION_ID,
        "messageID": "msg_asst_002",
        "type": "tool", "tool": "question", "callID": "call_q001",
        "state": {"status": "pending", "input": {}, "raw": ""},
    }
})

EVT_QUESTION_TOOL_RUNNING = _evt("message.part.updated", {
    "part": {
        "id": "prt_q1", "sessionID": SESSION_ID,
        "messageID": "msg_asst_002",
        "type": "tool", "tool": "question", "callID": "call_q001",
        "state": {
            "status": "running",
            "input": {"questions": [{"question": "X?", "header": "X",
                                     "options": [], "multiple": False}]},
            "time": {"start": 1000},
        },
    }
})

# -- Error event ------------------------------------------------------------

EVT_ERROR = _evt("session.error", {
    "error": "Model rate limit exceeded",
    "sessionID": SESSION_ID,
})

# -- Project event (sent on resume instead of server.connected) -------------

EVT_PROJECT_UPDATED = _evt("project.updated", {
    "id": "global", "worktree": "/",
    "time": {"created": 1000, "updated": 2000},
})


# ===========================================================================
# Tests: informational events (should produce no SDKEvents)
# ===========================================================================

class TestInformationalEvents:
    """Events that should be silently skipped."""

    @pytest.mark.parametrize("event", [
        EVT_SERVER_CONNECTED,
        EVT_SERVER_HEARTBEAT,
        EVT_SESSION_UPDATED,
        EVT_SESSION_STATUS_BUSY,
        EVT_SESSION_STATUS_IDLE,
        EVT_SESSION_DIFF,
        EVT_MESSAGE_UPDATED_USER,
        EVT_MESSAGE_UPDATED_ASSISTANT,
    ])
    def test_skipped_events(self, transformer, event):
        result = transformer.translate(event, SESSION_ID)
        assert result == []

    def test_unknown_event_skipped(self, transformer):
        result = transformer.translate(_evt("unknown.type"), SESSION_ID)
        assert result == []

    def test_project_updated_skipped(self, transformer):
        """project.updated is sent on resume — should be silently skipped."""
        result = transformer.translate(EVT_PROJECT_UPDATED, SESSION_ID)
        assert result == []


# ===========================================================================
# Tests: session completion
# ===========================================================================

class TestSessionCompletion:
    def test_session_idle_emits_done(self, transformer):
        result = transformer.translate(EVT_SESSION_IDLE, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.DONE
        assert result[0].session_id == SESSION_ID

    def test_session_idle_flushes_remaining_text(self, transformer):
        """Any buffered text should be flushed before DONE."""
        # Register a text part and buffer some text
        transformer.translate(EVT_TEXT_PART_START, SESSION_ID)
        transformer.translate(
            _text_delta("prt_text_001", "leftover text"),
            SESSION_ID,
        )
        assert transformer._text_buffers.get("prt_text_001") == "leftover text"

        # session.idle should flush then emit DONE
        result = transformer.translate(EVT_SESSION_IDLE, SESSION_ID)
        assert len(result) == 2
        assert result[0].type == SDKEventType.ASSISTANT
        assert result[0].content == "leftover text"
        assert result[1].type == SDKEventType.DONE


# ===========================================================================
# Tests: error events
# ===========================================================================

class TestErrorEvents:
    def test_error_event(self, transformer):
        result = transformer.translate(EVT_ERROR, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.ERROR
        assert "rate limit" in result[0].content.lower()

    def test_error_in_properties(self, transformer):
        event = _evt("some.event", {"error": "Something broke"})
        result = transformer.translate(event, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.ERROR
        assert result[0].content == "Something broke"


# ===========================================================================
# Tests: text streaming with newline-based flushing
# ===========================================================================

class TestTextStreaming:
    def test_text_delta_buffered_until_newline(self, transformer):
        """Deltas without newlines should be buffered."""
        transformer.translate(EVT_TEXT_PART_START, SESSION_ID)

        result = transformer.translate(
            _text_delta("prt_text_001", "Hello "),
            SESSION_ID,
        )
        assert result == []  # No newline — buffered

        result = transformer.translate(
            _text_delta("prt_text_001", "world"),
            SESSION_ID,
        )
        assert result == []  # Still no newline

    def test_text_delta_flushed_on_newline(self, transformer):
        """When a newline appears, flush everything up to it."""
        transformer.translate(EVT_TEXT_PART_START, SESSION_ID)

        transformer.translate(
            _text_delta("prt_text_001", "Hello "),
            SESSION_ID,
        )
        result = transformer.translate(
            _text_delta("prt_text_001", "world\nNext"),
            SESSION_ID,
        )
        assert len(result) == 1
        assert result[0].type == SDKEventType.ASSISTANT
        assert result[0].content == "Hello world\n"

        # "Next" should remain in buffer
        assert transformer._text_buffers["prt_text_001"] == "Next"

    def test_text_part_complete_flushes_buffer(self, transformer):
        """When text part finishes, emit whatever is left."""
        transformer.translate(EVT_TEXT_PART_START, SESSION_ID)

        transformer.translate(
            _text_delta("prt_text_001", "remaining text"),
            SESSION_ID,
        )

        result = transformer.translate(EVT_TEXT_PART_COMPLETE, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.ASSISTANT
        assert result[0].content == "remaining text"

    def test_text_part_complete_uses_snapshot_if_no_buffer(self, transformer):
        """If buffer is empty, use the text from the part snapshot."""
        transformer.translate(EVT_TEXT_PART_START, SESSION_ID)

        result = transformer.translate(EVT_TEXT_PART_COMPLETE, SESSION_ID)
        assert len(result) == 1
        assert result[0].content == "Hello, world!\nHow are you?"

    def test_reasoning_deltas_buffered(self, transformer):
        """Reasoning deltas should be buffered (no newline = no flush)."""
        transformer.translate(EVT_REASONING_PART_START, SESSION_ID)
        result = transformer.translate(EVT_REASONING_DELTA, SESSION_ID)
        # No newline in delta, so stays buffered
        assert result == []

    def test_reasoning_part_complete_emits_thinking(self, transformer):
        """Reasoning part completion should flush buffer as THINKING event."""
        transformer.translate(EVT_REASONING_PART_START, SESSION_ID)
        transformer.translate(EVT_REASONING_DELTA, SESSION_ID)

        result = transformer.translate(EVT_REASONING_PART_COMPLETE, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.THINKING
        assert result[0].content == "thinking about this..."

    def test_reasoning_delta_with_newline_flushes_as_thinking(self, transformer):
        """Reasoning deltas with newlines should flush as THINKING events."""
        transformer.translate(EVT_REASONING_PART_START, SESSION_ID)
        delta_with_nl = _text_delta("prt_reason_001", "line one\nline two")
        result = transformer.translate(delta_with_nl, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.THINKING
        assert result[0].content == "line one\n"


# ===========================================================================
# Tests: tool events
# ===========================================================================

class TestToolEvents:
    def test_tool_pending_no_input_skipped(self, transformer):
        """Pending with empty input should not emit."""
        result = transformer.translate(EVT_TOOL_PENDING, SESSION_ID)
        assert result == []

    def test_tool_running_emits_tool_use(self, transformer):
        result = transformer.translate(EVT_TOOL_RUNNING, SESSION_ID)
        assert len(result) == 1
        evt = result[0]
        assert evt.type == SDKEventType.TOOL_USE
        assert evt.tool_name == "read"
        assert "read" in evt.content
        assert evt.metadata["tool_call_id"] == "call_001"
        assert evt.metadata["tool_input"]["file_path"] == "/app/workspace/scripts"

    def test_tool_completed_emits_tool_result(self, transformer):
        result = transformer.translate(EVT_TOOL_COMPLETED, SESSION_ID)
        assert len(result) == 1
        evt = result[0]
        assert evt.type == SDKEventType.TOOL_RESULT
        assert "init_db.py" in evt.content
        assert evt.metadata["tool_name"] == "read"

    def test_tool_error_emits_tool_result_with_error(self, transformer):
        result = transformer.translate(EVT_TOOL_ERROR, SESSION_ID)
        assert len(result) == 1
        evt = result[0]
        assert evt.type == SDKEventType.TOOL_RESULT
        assert "Permission denied" in evt.content
        assert evt.metadata["is_error"] is True


# ===========================================================================
# Tests: permission events
# ===========================================================================

class TestPermissionEvents:
    def test_permission_asked_forwarded_as_system_event(self, transformer):
        """Permission requests should be forwarded to the UI for user approval."""
        result = transformer.translate(EVT_PERMISSION_ASKED, SESSION_ID)
        assert len(result) == 1
        evt = result[0]
        assert evt.type == SDKEventType.SYSTEM
        assert evt.subtype == "permission_asked"
        assert evt.metadata["permission"]["permission"] == "external_directory"
        assert evt.metadata["permission"]["patterns"] == ["/app/workspace/scripts/*"]
        # Content must be non-empty so the frontend renderer displays it
        assert evt.content
        assert "external_directory" in evt.content
        assert "/app/workspace/scripts/*" in evt.content

    def test_permission_includes_tool_context(self, transformer):
        """Permission event should include which tool triggered it."""
        result = transformer.translate(EVT_PERMISSION_ASKED, SESSION_ID)
        evt = result[0]
        tool_info = evt.metadata["permission"]["tool"]
        assert tool_info["messageID"] == "msg_asst_001"
        assert tool_info["callID"] == "call_001"

    def test_permission_includes_request_id(self, transformer):
        """Permission event should include the request ID for replying."""
        result = transformer.translate(EVT_PERMISSION_ASKED, SESSION_ID)
        evt = result[0]
        assert evt.metadata["permission"]["id"] == "per_001"


# ===========================================================================
# Tests: `question` tool → unified `askuserquestion` widget
# ===========================================================================

class TestQuestionAsked:
    """OpenCode's `question` tool must be remapped to the Claude Code
    AskUserQuestion widget contract so the existing frontend renders it."""

    def test_question_asked_emits_tool_use_and_done(self, transformer):
        result = transformer.translate(EVT_QUESTION_ASKED, SESSION_ID)
        types = [e.type for e in result]
        assert SDKEventType.TOOL_USE in types
        assert SDKEventType.DONE in types
        # DONE must come last so the outer stream closes cleanly
        assert result[-1].type == SDKEventType.DONE

    def test_question_asked_tool_name_is_askuserquestion(self, transformer):
        result = transformer.translate(EVT_QUESTION_ASKED, SESSION_ID)
        tool_use = next(e for e in result if e.type == SDKEventType.TOOL_USE)
        assert tool_use.tool_name == "askuserquestion"

    def test_question_asked_tool_input_shape(self, transformer):
        """The emitted tool_input must match the Claude Code AskUserQuestion
        schema: {questions: [{question, header, options, multiSelect, ...}]}."""
        result = transformer.translate(EVT_QUESTION_ASKED, SESSION_ID)
        tool_use = next(e for e in result if e.type == SDKEventType.TOOL_USE)
        questions = tool_use.metadata["tool_input"]["questions"]
        assert len(questions) == 1
        q = questions[0]
        # Core fields preserved
        assert q["question"] == "Which version do you want?"
        assert q["header"] == "Table type"
        assert len(q["options"]) == 2
        # Normalized to frontend's preferred key while keeping original
        assert q["multiSelect"] is False
        assert q["multiple"] is False

    def test_question_asked_preserves_multiple_true_as_multiSelect(self, transformer):
        """`multiple: true` on OpenCode must become `multiSelect: true`."""
        evt = _evt("question.asked", {
            "id": "que_002", "sessionID": SESSION_ID,
            "questions": [{
                "question": "Pick any", "header": "Multi",
                "options": [{"label": "A", "description": "a"}],
                "multiple": True,
            }],
            "tool": {"messageID": "msg", "callID": "call_q002"},
        })
        result = transformer.translate(evt, SESSION_ID)
        tool_use = next(e for e in result if e.type == SDKEventType.TOOL_USE)
        assert tool_use.metadata["tool_input"]["questions"][0]["multiSelect"] is True

    def test_question_asked_carries_request_id(self, transformer):
        """The adapter needs `opencode_question_request_id` to call
        `POST /question/{requestID}/reject` and unblock the suspended session."""
        result = transformer.translate(EVT_QUESTION_ASKED, SESSION_ID)
        tool_use = next(e for e in result if e.type == SDKEventType.TOOL_USE)
        assert tool_use.metadata["opencode_question_request_id"] == "que_001"
        # DONE also carries it for defensive logging / debugging
        done = next(e for e in result if e.type == SDKEventType.DONE)
        assert done.metadata.get("opencode_question_request_id") == "que_001"

    def test_question_asked_flushes_text_buffer_before_tool_use(self, transformer):
        """Any buffered preamble text must appear before the tool block."""
        # Buffer a reasoning partial (no newline → stays buffered)
        transformer.translate(
            _evt("message.part.updated", {
                "part": {"id": "prt_r", "sessionID": SESSION_ID,
                         "messageID": "msg", "type": "reasoning",
                         "text": "", "time": {"start": 1000}}
            }),
            SESSION_ID,
        )
        transformer.translate(
            _evt("message.part.delta", {
                "sessionID": SESSION_ID, "messageID": "msg",
                "partID": "prt_r", "field": "text", "delta": "I should ask"
            }),
            SESSION_ID,
        )
        result = transformer.translate(EVT_QUESTION_ASKED, SESSION_ID)
        # Thinking flush comes first, then TOOL_USE, then DONE
        assert [e.type for e in result] == [
            SDKEventType.THINKING,
            SDKEventType.TOOL_USE,
            SDKEventType.DONE,
        ]
        assert result[0].content == "I should ask"

    def test_question_tool_part_pending_suppressed(self, transformer):
        """`message.part.updated` for tool=question (pending) must produce
        nothing — `question.asked` is the canonical source."""
        result = transformer.translate(EVT_QUESTION_TOOL_PENDING, SESSION_ID)
        assert result == []

    def test_question_tool_part_running_suppressed(self, transformer):
        """Likewise, the running state is suppressed to avoid a duplicate
        tool block (the `question.asked` handler emits askuserquestion once)."""
        result = transformer.translate(EVT_QUESTION_TOOL_RUNNING, SESSION_ID)
        assert result == []

    def test_question_tool_part_running_suppressed_after_asked(self, transformer):
        """Sanity: even if question.asked fires first (typical order) and the
        running part arrives later, the running event stays suppressed."""
        transformer.translate(EVT_QUESTION_ASKED, SESSION_ID)
        result = transformer.translate(EVT_QUESTION_TOOL_RUNNING, SESSION_ID)
        assert result == []


# ===========================================================================
# Tests: step lifecycle
# ===========================================================================

class TestStepEvents:
    def test_step_start_skipped(self, transformer):
        result = transformer.translate(EVT_STEP_START, SESSION_ID)
        assert result == []

    def test_step_finish_stop_skipped(self, transformer):
        result = transformer.translate(EVT_STEP_FINISH_STOP, SESSION_ID)
        assert result == []


# ===========================================================================
# Tests: full conversation replay from JSONL
# ===========================================================================

class TestConversationReplay:
    """Replay a full conversation from captured JSONL log and verify output."""

    # Simplified first-message conversation: text-only response
    FIRST_MESSAGE_EVENTS = [
        # User message echo
        _evt("message.updated", {"info": {"id": "msg_u1", "sessionID": SESSION_ID, "role": "user", "time": {"created": 1000}}}),
        _evt("message.part.updated", {"part": {"id": "prt_u1", "sessionID": SESSION_ID, "messageID": "msg_u1", "type": "text", "text": "hello"}}),
        _evt("session.status", {"sessionID": SESSION_ID, "status": {"type": "busy"}}),
        # Assistant reasoning
        _evt("message.part.updated", {"part": {"id": "prt_r1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "step-start"}}),
        _evt("message.part.updated", {"part": {"id": "prt_r2", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "reasoning", "text": "", "time": {"start": 1000}}}),
        _evt("message.part.delta", {"sessionID": SESSION_ID, "messageID": "msg_a1", "partID": "prt_r2", "field": "text", "delta": "simple greeting"}),
        _evt("message.part.updated", {"part": {"id": "prt_r2", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "reasoning", "text": "simple greeting", "time": {"start": 1000, "end": 1100}}}),
        # Assistant text response
        _evt("message.part.updated", {"part": {"id": "prt_t1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "text", "text": "", "time": {"start": 1100}}}),
        _evt("message.part.delta", {"sessionID": SESSION_ID, "messageID": "msg_a1", "partID": "prt_t1", "field": "text", "delta": "Hello! How can I help you today?\n"}),
        _evt("message.part.delta", {"sessionID": SESSION_ID, "messageID": "msg_a1", "partID": "prt_t1", "field": "text", "delta": "I'm ready to assist."}),
        _evt("message.part.updated", {"part": {"id": "prt_t1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "text", "text": "Hello! How can I help you today?\nI'm ready to assist.", "time": {"start": 1100, "end": 1200}}}),
        # Step finish + session idle
        _evt("message.part.updated", {"part": {"id": "prt_sf1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "step-finish", "reason": "stop", "tokens": {"total": 100}}}),
        _evt("session.status", {"sessionID": SESSION_ID, "status": {"type": "idle"}}),
        _evt("session.idle", {"sessionID": SESSION_ID}),
    ]

    def test_first_message_replay(self, transformer):
        """Replay a simple text conversation and verify event stream."""
        all_events: list[SDKEvent] = []
        for raw in self.FIRST_MESSAGE_EVENTS:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        # Filter by type
        assistant_events = [e for e in all_events if e.type == SDKEventType.ASSISTANT]
        done_events = [e for e in all_events if e.type == SDKEventType.DONE]

        # Should have assistant text events
        assert len(assistant_events) >= 1
        full_text = "".join(e.content for e in assistant_events)
        assert "Hello! How can I help you today?" in full_text
        assert "I'm ready to assist." in full_text

        # Should end with DONE
        assert len(done_events) == 1

        # Reasoning should come through as THINKING events
        reasoning_events = [e for e in all_events if e.type == SDKEventType.THINKING]
        assert len(reasoning_events) >= 1
        thinking_text = "".join(e.content for e in reasoning_events)
        assert "simple greeting" in thinking_text

    # Conversation with tool use (no permission needed — pre-approved in config)
    TOOL_USE_EVENTS = [
        # Reasoning
        _evt("message.part.updated", {"part": {"id": "prt_r1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "reasoning", "text": "", "time": {"start": 1000}}}),
        _evt("message.part.updated", {"part": {"id": "prt_r1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "reasoning", "text": "need to read files", "time": {"start": 1000, "end": 1050}}}),
        # Tool pending (no input)
        _evt("message.part.updated", {"part": {"id": "prt_tool1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "tool", "callID": "call_1", "tool": "read", "state": {"status": "pending", "input": {}}}}),
        # Tool running (with input)
        _evt("message.part.updated", {"part": {"id": "prt_tool1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "tool", "callID": "call_1", "tool": "read", "state": {"status": "running", "input": {"filePath": "/app/workspace/scripts"}, "time": {"start": 1100}}}}),
        # Tool completed
        _evt("message.part.updated", {"part": {"id": "prt_tool1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "tool", "callID": "call_1", "tool": "read", "state": {"status": "completed", "input": {"filePath": "/app/workspace/scripts"}, "output": "init_db.py\ntask_manager.py", "time": {"start": 1100, "end": 1200}}}}),
        # Text response after tool
        _evt("message.part.updated", {"part": {"id": "prt_t2", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "text", "text": "", "time": {"start": 1300}}}),
        _evt("message.part.delta", {"sessionID": SESSION_ID, "messageID": "msg_a1", "partID": "prt_t2", "field": "text", "delta": "Found 2 scripts.\n"}),
        _evt("message.part.updated", {"part": {"id": "prt_t2", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "text", "text": "Found 2 scripts.\n", "time": {"start": 1300, "end": 1400}}}),
        # Done
        _evt("message.part.updated", {"part": {"id": "prt_sf", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "step-finish", "reason": "stop"}}),
        _evt("session.idle", {"sessionID": SESSION_ID}),
    ]

    def test_tool_use_replay(self, transformer):
        """Replay a conversation with tool use (pre-approved, no permission prompt)."""
        all_events: list[SDKEvent] = []
        for raw in self.TOOL_USE_EVENTS:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        tool_use = [e for e in all_events if e.type == SDKEventType.TOOL_USE]
        tool_result = [e for e in all_events if e.type == SDKEventType.TOOL_RESULT]
        assistant = [e for e in all_events if e.type == SDKEventType.ASSISTANT]
        done = [e for e in all_events if e.type == SDKEventType.DONE]

        assert len(tool_use) == 1
        assert tool_use[0].tool_name == "read"
        assert tool_use[0].metadata["tool_input"]["file_path"] == "/app/workspace/scripts"

        assert len(tool_result) == 1
        assert "init_db.py" in tool_result[0].content

        assert len(assistant) >= 1
        assert "Found 2 scripts" in "".join(e.content for e in assistant)

        assert len(done) == 1

    # Conversation with tool use that requires permission approval
    # (real flow from opencode serve 1.2.27 — external_directory not in config)
    TOOL_USE_WITH_PERMISSION_EVENTS = [
        # Reasoning
        _evt("message.part.updated", {"part": {"id": "prt_r1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "reasoning", "text": "", "time": {"start": 1000}}}),
        _evt("message.part.updated", {"part": {"id": "prt_r1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "reasoning", "text": "need to read scripts", "time": {"start": 1000, "end": 1050}}}),
        # Tool pending (no input yet)
        _evt("message.part.updated", {"part": {"id": "prt_tool1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "tool", "callID": "call_1", "tool": "read", "state": {"status": "pending", "input": {}, "raw": ""}}}),
        # Permission asked — opencode blocks until user approves
        _evt("permission.asked", {
            "id": "per_abc123", "sessionID": SESSION_ID,
            "permission": "external_directory",
            "patterns": ["/app/workspace/scripts/*"],
            "metadata": {"filepath": "/app/workspace/scripts"},
            "always": ["/app/workspace/scripts/*"],
            "tool": {"messageID": "msg_a1", "callID": "call_1"},
        }),
        # After user approves, tool transitions to running
        _evt("message.part.updated", {"part": {"id": "prt_tool1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "tool", "callID": "call_1", "tool": "read", "state": {"status": "running", "input": {"filePath": "/app/workspace/scripts"}, "time": {"start": 1100}}}}),
        # Tool completed
        _evt("message.part.updated", {"part": {"id": "prt_tool1", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "tool", "callID": "call_1", "tool": "read", "state": {"status": "completed", "input": {"filePath": "/app/workspace/scripts"}, "output": "init_db.py\ntask_manager.py", "time": {"start": 1100, "end": 1200}}}}),
        # Text response
        _evt("message.part.updated", {"part": {"id": "prt_t2", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "text", "text": "", "time": {"start": 1300}}}),
        _evt("message.part.delta", {"sessionID": SESSION_ID, "messageID": "msg_a1", "partID": "prt_t2", "field": "text", "delta": "Here are the scripts.\n"}),
        _evt("message.part.updated", {"part": {"id": "prt_t2", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "text", "text": "Here are the scripts.\n", "time": {"start": 1300, "end": 1400}}}),
        _evt("message.part.updated", {"part": {"id": "prt_sf", "sessionID": SESSION_ID, "messageID": "msg_a1", "type": "step-finish", "reason": "stop"}}),
        _evt("session.idle", {"sessionID": SESSION_ID}),
    ]

    def test_tool_use_with_permission_approval(self, transformer):
        """
        Replay a conversation where a tool requires permission approval.

        The permission.asked event should be forwarded to the UI as a SYSTEM
        event so the user can approve it. After approval, the tool completes
        normally.
        """
        all_events: list[SDKEvent] = []
        for raw in self.TOOL_USE_WITH_PERMISSION_EVENTS:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        # Permission request should be forwarded as SYSTEM event
        permission_events = [
            e for e in all_events
            if e.type == SDKEventType.SYSTEM and e.subtype == "permission_asked"
        ]
        assert len(permission_events) == 1
        perm = permission_events[0]
        assert perm.metadata["permission"]["id"] == "per_abc123"
        assert perm.metadata["permission"]["permission"] == "external_directory"
        assert perm.metadata["permission"]["patterns"] == ["/app/workspace/scripts/*"]
        # Should include tool context for the UI
        assert perm.metadata["permission"]["tool"]["callID"] == "call_1"

        # Tool use and result should still work after permission
        tool_use = [e for e in all_events if e.type == SDKEventType.TOOL_USE]
        tool_result = [e for e in all_events if e.type == SDKEventType.TOOL_RESULT]
        assert len(tool_use) == 1
        assert tool_use[0].tool_name == "read"
        assert len(tool_result) == 1
        assert "init_db.py" in tool_result[0].content

        # Text and done should follow
        assistant = [e for e in all_events if e.type == SDKEventType.ASSISTANT]
        done = [e for e in all_events if e.type == SDKEventType.DONE]
        assert len(assistant) >= 1
        assert "Here are the scripts" in "".join(e.content for e in assistant)
        assert len(done) == 1

    def test_permission_event_order_in_stream(self, transformer):
        """
        Verify that in the full event stream, the permission event appears
        before the tool use event — matching the real opencode behavior.
        """
        all_events: list[SDKEvent] = []
        for raw in self.TOOL_USE_WITH_PERMISSION_EVENTS:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        # Find indices
        perm_idx = next(
            i for i, e in enumerate(all_events)
            if e.type == SDKEventType.SYSTEM and e.subtype == "permission_asked"
        )
        tool_idx = next(
            i for i, e in enumerate(all_events)
            if e.type == SDKEventType.TOOL_USE
        )
        done_idx = next(
            i for i, e in enumerate(all_events)
            if e.type == SDKEventType.DONE
        )

        # Permission asked → tool use → done
        assert perm_idx < tool_idx < done_idx

    # Second message on resumed session (no server.connected,
    # starts with project.updated)
    SECOND_MESSAGE_EVENTS = [
        _evt("project.updated", {"id": "global", "worktree": "/"}),
        _evt("message.updated", {"info": {"id": "msg_u2", "sessionID": SESSION_ID, "role": "user", "time": {"created": 2000}}}),
        _evt("message.part.updated", {"part": {"id": "prt_u2", "sessionID": SESSION_ID, "messageID": "msg_u2", "type": "text", "text": "what next?"}}),
        _evt("session.status", {"sessionID": SESSION_ID, "status": {"type": "busy"}}),
        _evt("message.part.updated", {"part": {"id": "prt_t3", "sessionID": SESSION_ID, "messageID": "msg_a2", "type": "text", "text": "", "time": {"start": 2100}}}),
        _evt("message.part.delta", {"sessionID": SESSION_ID, "messageID": "msg_a2", "partID": "prt_t3", "field": "text", "delta": "Let me check.\n"}),
        _evt("message.part.updated", {"part": {"id": "prt_t3", "sessionID": SESSION_ID, "messageID": "msg_a2", "type": "text", "text": "Let me check.\n", "time": {"start": 2100, "end": 2200}}}),
        _evt("message.part.updated", {"part": {"id": "prt_sf2", "sessionID": SESSION_ID, "messageID": "msg_a2", "type": "step-finish", "reason": "stop"}}),
        _evt("session.idle", {"sessionID": SESSION_ID}),
    ]

    def test_second_message_no_server_connected(self, transformer):
        """
        On session resume, opencode sends project.updated instead of
        server.connected. The adapter should still work — project.updated
        is just silently skipped, and the conversation completes normally.
        """
        all_events: list[SDKEvent] = []
        for raw in self.SECOND_MESSAGE_EVENTS:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        assistant = [e for e in all_events if e.type == SDKEventType.ASSISTANT]
        done = [e for e in all_events if e.type == SDKEventType.DONE]

        assert len(assistant) >= 1
        assert "Let me check" in "".join(e.content for e in assistant)
        assert len(done) == 1

    def test_multi_turn_conversation(self, transformer):
        """Two consecutive messages through the same adapter instance."""
        # First message
        events_1: list[SDKEvent] = []
        for raw in self.FIRST_MESSAGE_EVENTS:
            events_1.extend(transformer.translate(raw, SESSION_ID))
        assert any(e.type == SDKEventType.DONE for e in events_1)

        # Reset between messages (as the real adapter does)
        transformer.reset()

        # Second message
        events_2: list[SDKEvent] = []
        for raw in self.SECOND_MESSAGE_EVENTS:
            events_2.extend(transformer.translate(raw, SESSION_ID))
        assert any(e.type == SDKEventType.DONE for e in events_2)

        assistant_2 = [e for e in events_2 if e.type == SDKEventType.ASSISTANT]
        assert "Let me check" in "".join(e.content for e in assistant_2)


# ===========================================================================
# Tests: real session replay (from opencode_session_20260322 capture)
# ===========================================================================

class TestRealSessionReplay:
    """
    Tests derived from a real successful OpenCode session log featuring:
    reasoning with OpenAI encrypted metadata, parallel tool calls,
    step-finish with reason "tool-calls", and multi-step conversation.
    """

    MSG_ID = "msg_asst_real"

    def test_reasoning_with_openai_metadata(self, transformer):
        """Reasoning parts with OpenAI encrypted content metadata should
        still emit THINKING events. The metadata should not break parsing."""
        # Reasoning part start with OpenAI encrypted content metadata
        start = _evt("message.part.updated", {"part": {
            "id": "prt_reason_meta", "sessionID": SESSION_ID,
            "messageID": self.MSG_ID, "type": "reasoning",
            "text": "", "metadata": {
                "openai": {
                    "itemId": "rs_abc123",
                    "reasoningEncryptedContent": "gAAAAA_encrypted_blob_here",
                },
            },
            "time": {"start": 1774176304824},
        }})
        result = transformer.translate(start, SESSION_ID)
        assert result == []  # Start with empty text — nothing to emit

        # Delta with reasoning text
        delta = _text_delta("prt_reason_meta", "Inspecting files\n")
        result = transformer.translate(delta, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.THINKING
        assert result[0].content == "Inspecting files\n"

        # Another delta that stays buffered (no trailing newline)
        delta2 = _text_delta("prt_reason_meta", "in scripts folder")
        result = transformer.translate(delta2, SESSION_ID)
        assert result == []  # No newline — buffered

        # Reasoning part complete — still has metadata; flushes remaining buffer
        complete = _evt("message.part.updated", {"part": {
            "id": "prt_reason_meta", "sessionID": SESSION_ID,
            "messageID": self.MSG_ID, "type": "reasoning",
            "text": "Inspecting files\nin scripts folder",
            "metadata": {
                "openai": {
                    "itemId": "rs_abc123",
                    "reasoningEncryptedContent": "gAAAAA_encrypted_blob_here",
                },
            },
            "time": {"start": 1774176304824, "end": 1774176307000},
        }})
        result = transformer.translate(complete, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.THINKING
        assert result[0].content == "in scripts folder"

    def test_reasoning_delta_with_newline_produces_thinking(self, transformer):
        """A reasoning delta containing a newline should flush the line(s)
        before the newline as a THINKING event."""
        # Register reasoning part
        transformer.translate(_evt("message.part.updated", {"part": {
            "id": "prt_reason_nl", "sessionID": SESSION_ID,
            "messageID": self.MSG_ID, "type": "reasoning",
            "text": "", "time": {"start": 1000},
        }}), SESSION_ID)

        # Delta with embedded newline (as seen in real session)
        delta = _text_delta(
            "prt_reason_nl",
            "**Inspecting files in scripts folder**\n\nI",
        )
        result = transformer.translate(delta, SESSION_ID)
        assert len(result) == 1
        assert result[0].type == SDKEventType.THINKING
        # Flush up to last newline
        assert result[0].content == "**Inspecting files in scripts folder**\n\n"

        # "I" should remain buffered
        assert transformer._text_buffers["prt_reason_nl"] == "I"

    def test_parallel_tool_calls(self, transformer):
        """Two tool calls (glob + read) happening in parallel — pending,
        running, completed. Should produce 2 TOOL_USE and 2 TOOL_RESULT
        events in order."""
        events = [
            # glob: pending (no input — skipped)
            _evt("message.part.updated", {"part": {
                "id": "prt_glob", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_glob", "tool": "glob",
                "state": {"status": "pending", "input": {}, "raw": ""},
            }}),
            # read: pending (no input — skipped)
            _evt("message.part.updated", {"part": {
                "id": "prt_read", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_read", "tool": "read",
                "state": {"status": "pending", "input": {}, "raw": ""},
            }}),
            # glob: running (with input)
            _evt("message.part.updated", {"part": {
                "id": "prt_glob", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_glob", "tool": "glob",
                "state": {
                    "status": "running",
                    "input": {"pattern": "*", "path": "/app/workspace/scripts"},
                    "time": {"start": 1774176307937},
                },
                "metadata": {"openai": {"itemId": "fc_glob123"}},
            }}),
            # read: running (with input)
            _evt("message.part.updated", {"part": {
                "id": "prt_read", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_read", "tool": "read",
                "state": {
                    "status": "running",
                    "input": {"filePath": "/app/workspace/scripts/README.md"},
                    "time": {"start": 1774176307940},
                },
                "metadata": {"openai": {"itemId": "fc_read456"}},
            }}),
            # glob: completed
            _evt("message.part.updated", {"part": {
                "id": "prt_glob", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_glob", "tool": "glob",
                "state": {
                    "status": "completed",
                    "input": {"pattern": "*", "path": "/app/workspace/scripts"},
                    "output": "README.md\ninit_db.py\ntask_manager.py\nutils.py\nconfig.py\nsetup.sh",
                    "title": "app/workspace/scripts",
                    "metadata": {"count": 6, "truncated": False},
                    "time": {"start": 1774176307937, "end": 1774176308026},
                },
                "metadata": {"openai": {"itemId": "fc_glob123"}},
            }}),
            # read: completed
            _evt("message.part.updated", {"part": {
                "id": "prt_read", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_read", "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "/app/workspace/scripts/README.md"},
                    "output": "# Scripts\n\nThis directory contains utility scripts.",
                    "time": {"start": 1774176307940, "end": 1774176308100},
                },
                "metadata": {"openai": {"itemId": "fc_read456"}},
            }}),
        ]

        all_events: list[SDKEvent] = []
        for raw in events:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        tool_use = [e for e in all_events if e.type == SDKEventType.TOOL_USE]
        tool_result = [e for e in all_events if e.type == SDKEventType.TOOL_RESULT]

        assert len(tool_use) == 2
        assert tool_use[0].tool_name == "glob"
        assert tool_use[0].metadata["tool_input"]["pattern"] == "*"
        assert tool_use[1].tool_name == "read"
        assert tool_use[1].metadata["tool_input"]["file_path"] == "/app/workspace/scripts/README.md"

        assert len(tool_result) == 2
        assert "README.md" in tool_result[0].content
        assert "utility scripts" in tool_result[1].content

        # Verify ordering: TOOL_USE events come before their TOOL_RESULT
        use_indices = [i for i, e in enumerate(all_events) if e.type == SDKEventType.TOOL_USE]
        result_indices = [i for i, e in enumerate(all_events) if e.type == SDKEventType.TOOL_RESULT]
        assert use_indices[0] < result_indices[0]
        assert use_indices[1] < result_indices[1]

    def test_step_finish_tool_calls_reason_skipped(self, transformer):
        """Step-finish with reason 'tool-calls' should be silently skipped
        (same as 'stop')."""
        event = _evt("message.part.updated", {"part": {
            "id": "prt_sf_tc", "sessionID": SESSION_ID,
            "messageID": self.MSG_ID, "type": "step-finish",
            "reason": "tool-calls",
            "cost": 0.015333,
            "tokens": {
                "total": 18969, "input": 18800, "output": 169,
                "reasoning": 105,
                "cache": {"read": 0, "write": 0},
            },
        }})
        result = transformer.translate(event, SESSION_ID)
        assert result == []

    def test_full_session_with_reasoning_tools_and_text(self, transformer):
        """Full replay: reasoning -> tool calls -> step-finish(tool-calls)
        -> second reasoning -> text response -> step-finish(stop) -> idle.

        Verifies correct event types and ordering."""
        events = [
            # --- Step 1: reasoning ---
            _evt("message.part.updated", {"part": {
                "id": "prt_ss1", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "step-start",
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_r1", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "reasoning",
                "text": "", "metadata": {"openai": {"itemId": "rs_001", "reasoningEncryptedContent": "gAAAAA..."}},
                "time": {"start": 1000},
            }}),
            _text_delta("prt_r1", "Need to inspect scripts\n"),
            _evt("message.part.updated", {"part": {
                "id": "prt_r1", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "reasoning",
                "text": "Need to inspect scripts\nand read README",
                "time": {"start": 1000, "end": 1050},
            }}),

            # --- Step 1: parallel tool calls ---
            _evt("message.part.updated", {"part": {
                "id": "prt_tg", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_g", "tool": "glob",
                "state": {"status": "pending", "input": {}, "raw": ""},
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_tr", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_r", "tool": "read",
                "state": {"status": "pending", "input": {}, "raw": ""},
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_tg", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_g", "tool": "glob",
                "state": {"status": "running", "input": {"pattern": "*", "path": "/app/workspace/scripts"}, "time": {"start": 1100}},
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_tr", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_r", "tool": "read",
                "state": {"status": "running", "input": {"filePath": "/app/workspace/scripts/README.md"}, "time": {"start": 1100}},
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_tg", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_g", "tool": "glob",
                "state": {"status": "completed", "input": {"pattern": "*"}, "output": "README.md\ninit_db.py", "time": {"start": 1100, "end": 1200}},
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_tr", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "tool",
                "callID": "call_r", "tool": "read",
                "state": {"status": "completed", "input": {"filePath": "/app/workspace/scripts/README.md"}, "output": "# Scripts README", "time": {"start": 1100, "end": 1200}},
            }}),

            # --- Step 1: step-finish with reason "tool-calls" ---
            _evt("message.part.updated", {"part": {
                "id": "prt_sf1", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "step-finish",
                "reason": "tool-calls",
                "cost": 0.015,
                "tokens": {"total": 18969, "input": 18800, "output": 169, "reasoning": 105},
            }}),

            # --- Step 2: second reasoning ---
            _evt("message.part.updated", {"part": {
                "id": "prt_ss2", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "step-start",
            }}),
            _evt("message.part.updated", {"part": {
                "id": "prt_r2", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "reasoning",
                "text": "", "time": {"start": 1300},
            }}),
            _text_delta("prt_r2", "Now I can summarize\n"),
            _evt("message.part.updated", {"part": {
                "id": "prt_r2", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "reasoning",
                "text": "Now I can summarize\nthe results",
                "time": {"start": 1300, "end": 1350},
            }}),

            # --- Step 2: text response ---
            _evt("message.part.updated", {"part": {
                "id": "prt_txt", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "text",
                "text": "", "time": {"start": 1400},
            }}),
            _text_delta("prt_txt", "I found 2 scripts in the directory:\n"),
            _text_delta("prt_txt", "- init_db.py\n- README.md"),
            _evt("message.part.updated", {"part": {
                "id": "prt_txt", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "text",
                "text": "I found 2 scripts in the directory:\n- init_db.py\n- README.md",
                "time": {"start": 1400, "end": 1500},
            }}),

            # --- Step 2: step-finish with reason "stop" ---
            _evt("message.part.updated", {"part": {
                "id": "prt_sf2", "sessionID": SESSION_ID,
                "messageID": self.MSG_ID, "type": "step-finish",
                "reason": "stop",
                "tokens": {"total": 25000},
            }}),

            # --- Session idle ---
            _evt("session.idle", {"sessionID": SESSION_ID}),
        ]

        all_events: list[SDKEvent] = []
        for raw in events:
            all_events.extend(transformer.translate(raw, SESSION_ID))

        # Collect by type
        thinking = [e for e in all_events if e.type == SDKEventType.THINKING]
        tool_use = [e for e in all_events if e.type == SDKEventType.TOOL_USE]
        tool_result = [e for e in all_events if e.type == SDKEventType.TOOL_RESULT]
        assistant = [e for e in all_events if e.type == SDKEventType.ASSISTANT]
        done = [e for e in all_events if e.type == SDKEventType.DONE]

        # THINKING from both reasoning steps
        assert len(thinking) >= 2
        thinking_text = "".join(e.content for e in thinking)
        assert "inspect scripts" in thinking_text
        assert "summarize" in thinking_text

        # Tool events from parallel calls
        assert len(tool_use) == 2
        assert {e.tool_name for e in tool_use} == {"glob", "read"}
        assert len(tool_result) == 2

        # Assistant text
        assert len(assistant) >= 1
        full_text = "".join(e.content for e in assistant)
        assert "I found 2 scripts" in full_text

        # DONE at the end
        assert len(done) == 1

        # Verify ordering: THINKING -> TOOL_USE -> TOOL_RESULT -> THINKING -> ASSISTANT -> DONE
        type_sequence = [e.type for e in all_events]
        first_thinking = type_sequence.index(SDKEventType.THINKING)
        first_tool_use = type_sequence.index(SDKEventType.TOOL_USE)
        first_tool_result = type_sequence.index(SDKEventType.TOOL_RESULT)
        last_done = len(type_sequence) - 1 - type_sequence[::-1].index(SDKEventType.DONE)

        assert first_thinking < first_tool_use
        assert first_tool_use < first_tool_result
        assert first_tool_result < last_done


# ===========================================================================
# Tests: event logger
# ===========================================================================

class TestEventLogger:
    def test_logging_disabled(self, tmp_path):
        logger = SessionEventLogger(tmp_path / "logs", prefix="opencode_session", enabled=False)
        logger.log_recv({"type": "test"})
        logger.log_send("test", {"data": 1})
        # No files should be created
        log_dir = tmp_path / "logs"
        if log_dir.exists():
            assert list(log_dir.iterdir()) == []

    def test_logging_enabled_bidirectional(self, tmp_path):
        log_dir = tmp_path / "logs"
        logger = SessionEventLogger(log_dir, prefix="opencode_session", enabled=True)

        logger.log_send("create_session", {"session_id": "ses_001"})
        logger.log_recv({"type": "server.connected", "properties": {}})
        logger.log_send("post_message", {"message": "hello"})
        logger.log_recv({"type": "session.idle", "properties": {}})

        log_files = list(log_dir.glob("opencode_session_*.jsonl"))
        assert len(log_files) == 1

        lines = log_files[0].read_text().strip().split("\n")
        assert len(lines) == 4

        records = [json.loads(line) for line in lines]
        assert records[0]["dir"] == "send"
        assert records[0]["event"]["action"] == "create_session"
        assert records[1]["dir"] == "recv"
        assert records[1]["event"]["type"] == "server.connected"
        assert records[2]["dir"] == "send"
        assert records[2]["event"]["action"] == "post_message"
        assert records[3]["dir"] == "recv"

        # All records should have timestamps
        for r in records:
            assert "ts" in r
