"""
Unit tests for A2ARequestHandler._extract_file_ids_from_message.

Covers the private helper that pulls ``cinna_file_ids`` out of an inbound A2A
message's ``metadata`` block and returns a ``list[UUID]`` (or ``None``).

Tests are pure Python — no HTTP, no database, no FastAPI TestClient.
The method is accessed by instantiating the handler with minimal mock
dependencies (agent and environment are never touched by this helper).

Test cases
──────────
  1. Valid list of UUID strings → returns corresponding list[UUID]
  2. Mixed valid + malformed entries → only valid UUIDs returned (malformed
     skipped without raising)
  3. Empty list → returns None
  4. Missing ``metadata`` key → returns None
  5. ``metadata`` present but not a dict (e.g. a string) → returns None
  6. ``metadata`` present but no ``cinna_file_ids`` key → returns None
  7. ``cinna_file_ids`` is not a list (e.g. a string) → returns None
  8. Entries already being UUID objects (not strings) → handled correctly,
     same result as string form
  9. Single valid UUID string in list → returns a single-element list[UUID]
 10. All entries malformed → returns None (list becomes empty → guarded by
     ``or None`` at the end)
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from app.services.a2a.a2a_request_handler import A2ARequestHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler() -> A2ARequestHandler:
    """Construct a minimal A2ARequestHandler sufficient to call the helper.

    Only ``_extract_file_ids_from_message`` is exercised here; it does not
    access ``self.agent``, ``self.environment``, or any DB session.
    """
    agent = MagicMock()
    environment = MagicMock()
    user_id = uuid.uuid4()
    get_db_session = MagicMock()

    return A2ARequestHandler(
        agent=agent,
        environment=environment,
        user_id=user_id,
        get_db_session=get_db_session,
    )


# Stable UUIDs shared across test cases for readability.
_UUID_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_UUID_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_UUID_C = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ---------------------------------------------------------------------------
# 1. Valid list of UUID strings → returns corresponding list[UUID]
# ---------------------------------------------------------------------------


def test_valid_uuid_strings_returned_as_uuid_objects() -> None:
    """A well-formed metadata.cinna_file_ids list converts every entry to UUID."""
    handler = _make_handler()
    message_data = {
        "metadata": {
            "cinna_file_ids": [str(_UUID_A), str(_UUID_B)],
        }
    }
    result = handler._extract_file_ids_from_message(message_data)

    assert result == [_UUID_A, _UUID_B]
    assert all(isinstance(fid, uuid.UUID) for fid in result)


# ---------------------------------------------------------------------------
# 2. Mixed valid + malformed entries → only valid UUIDs returned
# ---------------------------------------------------------------------------


def test_malformed_entries_skipped_valid_ones_returned() -> None:
    """Malformed entries are silently dropped; valid neighbours are kept."""
    handler = _make_handler()
    message_data = {
        "metadata": {
            "cinna_file_ids": [
                str(_UUID_A),
                "not-a-uuid",            # malformed
                str(_UUID_B),
                "12345-bad",             # malformed
                str(_UUID_C),
            ],
        }
    }
    result = handler._extract_file_ids_from_message(message_data)

    assert result == [_UUID_A, _UUID_B, _UUID_C]


# ---------------------------------------------------------------------------
# 3. Empty list → returns None
# ---------------------------------------------------------------------------


def test_empty_list_returns_none() -> None:
    """An empty cinna_file_ids list returns None (nothing to attach)."""
    handler = _make_handler()
    message_data = {"metadata": {"cinna_file_ids": []}}

    assert handler._extract_file_ids_from_message(message_data) is None


# ---------------------------------------------------------------------------
# 4. Missing ``metadata`` key → returns None
# ---------------------------------------------------------------------------


def test_missing_metadata_returns_none() -> None:
    """No metadata key at all → None (graceful absence)."""
    handler = _make_handler()
    message_data = {"parts": [{"text": "Hello"}]}

    assert handler._extract_file_ids_from_message(message_data) is None


# ---------------------------------------------------------------------------
# 5. ``metadata`` present but not a dict → returns None
# ---------------------------------------------------------------------------


def test_metadata_not_a_dict_returns_none() -> None:
    """metadata is present but is a string, not a dict → type guard fires → None."""
    handler = _make_handler()
    for non_dict_value in ("string-value", 42, [str(_UUID_A)], None):
        result = handler._extract_file_ids_from_message(
            {"metadata": non_dict_value}
        )
        assert result is None, (
            f"Expected None for metadata={non_dict_value!r}, got {result!r}"
        )


# ---------------------------------------------------------------------------
# 6. ``metadata`` present but no ``cinna_file_ids`` key → returns None
# ---------------------------------------------------------------------------


def test_metadata_dict_without_cinna_file_ids_returns_none() -> None:
    """metadata is a valid dict but cinna_file_ids is absent → None."""
    handler = _make_handler()
    message_data = {"metadata": {"some_other_key": "value"}}

    assert handler._extract_file_ids_from_message(message_data) is None


# ---------------------------------------------------------------------------
# 7. ``cinna_file_ids`` is not a list → returns None
# ---------------------------------------------------------------------------


def test_cinna_file_ids_not_a_list_returns_none() -> None:
    """cinna_file_ids present but not a list (str, int, dict) → None."""
    handler = _make_handler()
    for bad_value in (str(_UUID_A), 42, {"id": str(_UUID_A)}, True):
        result = handler._extract_file_ids_from_message(
            {"metadata": {"cinna_file_ids": bad_value}}
        )
        assert result is None, (
            f"Expected None for cinna_file_ids={bad_value!r}, got {result!r}"
        )


# ---------------------------------------------------------------------------
# 8. Entries already being UUID objects (not strings) → handled correctly
# ---------------------------------------------------------------------------


def test_uuid_object_entries_handled() -> None:
    """UUID instances (not strings) in the list are accepted unchanged."""
    handler = _make_handler()
    message_data = {
        "metadata": {
            "cinna_file_ids": [_UUID_A, _UUID_B],
        }
    }
    result = handler._extract_file_ids_from_message(message_data)

    assert result == [_UUID_A, _UUID_B]
    assert all(isinstance(fid, uuid.UUID) for fid in result)


# ---------------------------------------------------------------------------
# 9. Single valid UUID string → single-element list
# ---------------------------------------------------------------------------


def test_single_valid_uuid_string_returns_single_element_list() -> None:
    """A list with exactly one valid UUID string returns [UUID]."""
    handler = _make_handler()
    message_data = {"metadata": {"cinna_file_ids": [str(_UUID_A)]}}

    result = handler._extract_file_ids_from_message(message_data)
    assert result == [_UUID_A]
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 10. All entries malformed → returns None (empty list → guarded by ``or None``)
# ---------------------------------------------------------------------------


def test_all_malformed_entries_returns_none() -> None:
    """When every entry is malformed the post-loop ``or None`` guard fires → None."""
    handler = _make_handler()
    message_data = {
        "metadata": {
            "cinna_file_ids": ["not-a-uuid", "also-bad", "123-456"],
        }
    }
    result = handler._extract_file_ids_from_message(message_data)

    assert result is None
