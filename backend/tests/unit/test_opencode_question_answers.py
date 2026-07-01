"""
Unit tests for OpenCodeAdapter._build_question_answers.

Covers the answer-mapping for the interactive `question` tool's `/reply` relay:
the user's next message (or structured selections threaded through
``session_state``) is mapped to opencode's ``{"answers": Answer[]}`` payload,
``Answer = string[]``, one entry per question in order.

End-to-end behavior (question → answer → resumed turn) is covered by the live
env probe documented in docs/agents/agent_environment_core/
opencode_interactive_questions_tech.md.

Run:
    cd backend && python -m pytest tests/unit/test_opencode_question_answers.py -v
"""

# sys.path to app_core_base is set by tests/unit/conftest.py
from core.server.adapters.opencode_sdk_adapter import OpenCodeAdapter

_build = OpenCodeAdapter._build_question_answers


# ===========================================================================
# Free-text fallback (no structured selections)
# ===========================================================================

class TestFreeTextFallback:
    """When no structured answers are supplied, the message text becomes a
    single custom answer in slot 0 and the rest are padded empty."""

    def test_single_question(self):
        assert _build("Red", 1, None) == [["Red"]]

    def test_multi_question_pads_empty_slots(self):
        # text → question 0; remaining questions left unanswered
        assert _build("Red", 3, None) == [["Red"], [], []]

    def test_zero_questions_clamps_to_one(self):
        # n_questions should never produce an empty answer list
        assert _build("Red", 0, None) == [["Red"]]

    def test_session_state_without_answers_key_falls_back(self):
        assert _build("Blue", 2, {"session_context": {"foo": "bar"}}) == [["Blue"], []]

    def test_session_state_with_malformed_answers_falls_back(self):
        # question_answers must be a list-of-lists; a flat list is ignored
        assert _build("Blue", 1, {"question_answers": ["Blue"]}) == [["Blue"]]

    def test_empty_question_answers_falls_back(self):
        assert _build("Blue", 1, {"question_answers": []}) == [["Blue"]]


# ===========================================================================
# Structured selections (threaded via session_state)
# ===========================================================================

class TestStructuredSelections:
    """Structured per-question selections take precedence over the text."""

    def test_structured_used_as_is(self):
        state = {"question_answers": [["Red"], ["Large"]]}
        assert _build("ignored text", 2, state) == [["Red"], ["Large"]]

    def test_structured_multi_select_slot(self):
        state = {"question_answers": [["Red", "Blue"]]}
        assert _build("ignored", 1, state) == [["Red", "Blue"]]

    def test_structured_padded_to_question_count(self):
        # Fewer answers than questions → pad the remaining slots empty
        state = {"question_answers": [["Red"]]}
        assert _build("ignored", 3, state) == [["Red"], [], []]

    def test_structured_truncated_to_question_count(self):
        # More answers than questions → truncate to the question count
        state = {"question_answers": [["Red"], ["Large"], ["Extra"]]}
        assert _build("ignored", 2, state) == [["Red"], ["Large"]]

    def test_structured_coerces_non_string_items(self):
        state = {"question_answers": [[1, 2]]}
        assert _build("ignored", 1, state) == [["1", "2"]]
