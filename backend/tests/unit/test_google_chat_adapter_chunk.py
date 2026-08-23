"""Unit tests for GoogleChatAdapter._chunk — pure text-splitting logic.

No I/O, no DB, no HTTP: a textbook tests/unit/ candidate per
backend/tests/README.md ("private `_helper` functions ... goes here").

The API-observable side of outbound delivery (STREAM_COMPLETED gating,
binding lookup, and that the adapter is actually invoked with the final
assistant text) is covered in
tests/api/server_channels/server_channels_pending_outbound_test.py — see the
module docstring there for the cross-reference back to this file.
"""
from app.services.server_channels.adapters.google_chat import GoogleChatAdapter

_LIMIT = 4096  # GoogleChatAdapter's max_message_chars


def _adapter() -> GoogleChatAdapter:
    return GoogleChatAdapter()


def test_chunk_short_text_is_a_single_chunk() -> None:
    text = "Hello, this fits in one message."
    chunks = _adapter()._chunk(text)
    assert chunks == [text]


def test_chunk_exactly_at_limit_is_a_single_chunk() -> None:
    text = "a" * _LIMIT
    chunks = _adapter()._chunk(text)
    assert chunks == [text]


def test_chunk_splits_oversized_text_with_no_newline_at_the_hard_limit() -> None:
    text = "a" * (_LIMIT + 500)
    chunks = _adapter()._chunk(text)
    assert len(chunks) == 2
    assert chunks[0] == "a" * _LIMIT
    assert chunks[1] == "a" * 500
    assert all(len(c) <= _LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_chunk_prefers_a_late_newline_boundary_over_a_hard_cut() -> None:
    # Newline sits at index LIMIT - 10 — comfortably past limit // 2 — so the
    # split must land there instead of at the hard limit, and the newline
    # itself must be consumed (not duplicated into either chunk).
    head = "a" * (_LIMIT - 10)
    tail = "b" * 300
    text = f"{head}\n{tail}"

    chunks = _adapter()._chunk(text)

    assert chunks == [head, tail]
    assert all(len(c) <= _LIMIT for c in chunks)
    # Rejoining with the single newline that was split on reconstructs the
    # original message exactly.
    assert "\n".join(chunks) == text


def test_chunk_ignores_a_pathologically_early_newline() -> None:
    # A newline at index 1 is well before limit // 2 and must NOT be used as
    # the split point — using it would produce a 1-character first chunk.
    text = "x\n" + ("y" * (_LIMIT + 100))

    chunks = _adapter()._chunk(text)

    assert len(chunks) == 2
    assert chunks[0] == text[:_LIMIT]
    assert chunks[1] == text[_LIMIT:]
    assert "".join(chunks) == text


def test_chunk_splits_multiple_times_for_a_large_multiple_of_the_limit() -> None:
    text = "z" * (_LIMIT * 3 + 10)

    chunks = _adapter()._chunk(text)

    assert len(chunks) == 4
    assert all(len(c) <= _LIMIT for c in chunks)
    assert sum(len(c) for c in chunks) == len(text)
    assert "".join(chunks) == text
