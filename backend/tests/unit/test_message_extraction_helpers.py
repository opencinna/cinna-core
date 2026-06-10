"""
Unit tests for the message-content extraction helpers in
``app.services.sessions.message_service``.

Pure regex/logic — no HTTP, no DB:
  - ``_extract_attachments``  → ``<cinna_attach>…</cinna_attach>`` agent file attaches
  - ``_extract_webapp_actions`` → ``<webapp_action>{…}</webapp_action>`` webapp actions

The API-observable side (attachments materialised on a real message, webapp_action
events emitted on the stream) is covered end-to-end in
``tests/api/agents/agents_message_attachments_test.py`` and
``tests/api/agents/agents_webapp_chat_actions_test.py``.
"""
from app.services.sessions.message_service import (
    _extract_attachments,
    _extract_webapp_actions,
)


# ── _extract_attachments ──────────────────────────────────────────────────────


def test_extract_attachments_single_absolute_path() -> None:
    paths, cleaned = _extract_attachments(
        "<cinna_attach>/app/workspace/files/report.pdf</cinna_attach>"
    )
    assert paths == ["/app/workspace/files/report.pdf"]
    assert "<cinna_attach>" not in cleaned


def test_extract_attachments_preserves_surrounding_text() -> None:
    paths, cleaned = _extract_attachments(
        "Here it is <cinna_attach>/app/workspace/files/x.csv</cinna_attach> done."
    )
    assert paths == ["/app/workspace/files/x.csv"]
    assert "Here it is" in cleaned
    assert "done." in cleaned
    assert "<cinna_attach>" not in cleaned


def test_extract_attachments_multiple_tags_in_order() -> None:
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


def test_extract_attachments_empty_body_stripped() -> None:
    paths, cleaned = _extract_attachments("<cinna_attach>  </cinna_attach>visible")
    assert paths == []
    assert "<cinna_attach>" not in cleaned
    assert "visible" in cleaned


def test_extract_attachments_non_absolute_body_skipped() -> None:
    paths, cleaned = _extract_attachments(
        "<cinna_attach>relative/path/file.pdf</cinna_attach>"
    )
    assert paths == []
    assert "<cinna_attach>" not in cleaned


def test_extract_attachments_multiline_path_dotall() -> None:
    paths, cleaned = _extract_attachments(
        "<cinna_attach>\n/app/workspace/files/report.pdf\n</cinna_attach>"
    )
    assert paths == ["/app/workspace/files/report.pdf"]
    assert "<cinna_attach>" not in cleaned


def test_extract_attachments_same_path_twice_kept() -> None:
    # De-dup is service-level, not regex-level.
    paths, cleaned = _extract_attachments(
        "<cinna_attach>/app/workspace/files/a.pdf</cinna_attach>"
        "<cinna_attach>/app/workspace/files/a.pdf</cinna_attach>"
    )
    assert paths == [
        "/app/workspace/files/a.pdf",
        "/app/workspace/files/a.pdf",
    ]
    assert "<cinna_attach>" not in cleaned


def test_extract_attachments_no_tags_unchanged() -> None:
    paths, cleaned = _extract_attachments("plain text with no tags")
    assert paths == []
    assert cleaned == "plain text with no tags"


# ── _extract_webapp_actions ───────────────────────────────────────────────────


def test_extract_webapp_actions_single_tag() -> None:
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>{"action": "refresh_page"}</webapp_action>'
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "refresh_page"
    assert actions[0]["data"] == {}
    assert "<webapp_action>" not in cleaned


def test_extract_webapp_actions_tag_with_data() -> None:
    actions, cleaned = _extract_webapp_actions(
        'Hello <webapp_action>{"action": "navigate", "data": {"path": "/foo"}}</webapp_action> world'
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "navigate"
    assert actions[0]["data"] == {"path": "/foo"}
    assert "Hello" in cleaned
    assert "world" in cleaned
    assert "<webapp_action>" not in cleaned


def test_extract_webapp_actions_multiple_tags() -> None:
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>{"action": "update_form", "data": {"form_id": "f1"}}</webapp_action>'
        "Some text"
        '<webapp_action>{"action": "show_notification", "data": {"message": "Done", "type": "success"}}</webapp_action>'
    )
    assert len(actions) == 2
    assert actions[0]["action"] == "update_form"
    assert actions[1]["action"] == "show_notification"
    assert "<webapp_action>" not in cleaned
    assert "Some text" in cleaned


def test_extract_webapp_actions_malformed_json_skipped() -> None:
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>not json</webapp_action>'
        "visible text"
    )
    assert len(actions) == 0  # malformed → skipped
    assert "<webapp_action>" not in cleaned
    assert "visible text" in cleaned


def test_extract_webapp_actions_missing_action_field_skipped() -> None:
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>{"foo": "bar"}</webapp_action>'
    )
    assert len(actions) == 0
    assert "<webapp_action>" not in cleaned


def test_extract_webapp_actions_no_tags_unchanged() -> None:
    actions, cleaned = _extract_webapp_actions("plain text without tags")
    assert len(actions) == 0
    assert cleaned == "plain text without tags"


def test_extract_webapp_actions_multiline_content() -> None:
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>\n{"action": "refresh_page"}\n</webapp_action>'
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "refresh_page"
