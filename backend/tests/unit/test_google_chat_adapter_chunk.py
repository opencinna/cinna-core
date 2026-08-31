"""Unit tests for GoogleChatAdapter._chunk — pure text-splitting logic.

No I/O, no DB, no HTTP: a textbook tests/unit/ candidate per
backend/tests/README.md ("private `_helper` functions ... goes here").

The API-observable side of outbound delivery (STREAM_COMPLETED gating,
binding lookup, and that the adapter is actually invoked with the final
assistant text) is covered in
tests/api/server_channels/server_channels_pending_outbound_test.py — see the
module docstring there for the cross-reference back to this file.
"""
import pytest

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


# ---------------------------------------------------------------------------
# Code fences across a split
# ---------------------------------------------------------------------------
#
# A fenced block cut in half is not a cosmetic problem. The first chunk ends
# with an unterminated fence, so Chat renders the rest of that message as
# prose; the second chunk then OPENS with the block's closing fence, which
# swallows everything after it. Both halves are wrong, and the reader sees a
# code block where there is none.


def test_chunk_closes_and_reopens_a_fence_it_splits() -> None:
    body = "\n".join("line %04d" % i for i in range(600))
    text = f"before\n```\n{body}\n```\nafter"
    assert len(text) > _LIMIT

    chunks = _adapter()._chunk(text)

    assert len(chunks) > 1
    assert all(len(c) <= _LIMIT for c in chunks)
    # Every chunk is balanced on its own: an even number of fence lines means
    # nothing is left hanging open at a message boundary.
    for chunk in chunks:
        fences = [ln for ln in chunk.split("\n") if ln.lstrip().startswith("```")]
        assert len(fences) % 2 == 0, chunk[:200]
    # The body survives the round trip, fences aside.
    rejoined = "\n".join(
        ln for c in chunks for ln in c.split("\n") if not ln.lstrip().startswith("```")
    )
    assert "line 0000" in rejoined and "line 0599" in rejoined


def test_chunk_leaves_fence_free_text_splitting_exactly_where_it_did() -> None:
    # The fence reserve must not cost ordinary prose eight characters a chunk:
    # it is claimed only when the text actually contains a fence.
    text = "a" * (_LIMIT + 500)
    assert _adapter()._chunk(text)[0] == "a" * _LIMIT


@pytest.mark.parametrize("tail_len", list(range(_LIMIT - 16, _LIMIT + 1)))
def test_chunk_never_exceeds_the_limit_when_the_tail_reopens_a_fence(tail_len: int) -> None:
    """Regression guard for a confirmed bug in the reserve loop condition.

    The loop used to exit at ``len(remaining) > limit`` rather than
    ``limit - reserve``, so re-appending the closing ``\\n```` onto a chunk
    that was cut at exactly the hard limit pushed it FOUR characters past
    ``max_message_chars`` — a message Chat's API answers with a 400, after
    earlier chunks of the same reply have already been posted.

    Swept across the boundary the bug actually lived on (a tail from 16 short
    of the limit up to the limit itself) rather than pinned at one length, and
    the exact documented repro — ``tail_len == 4093`` — is pinned exactly:
    pre-fix this produced ``[4084, 4097]`` (the second chunk OVER the limit);
    it must now produce ``[4084, 4096, 9]``.
    """
    adapter = _adapter()
    text = "```\n" + "a" * 4076 + "\n" + "b" * tail_len

    chunks = adapter._chunk(text)

    assert all(len(c) <= _LIMIT for c in chunks), [len(c) for c in chunks]
    # No content lost to the fence bookkeeping: every "a" and "b" survives.
    joined = "".join(chunks)
    assert joined.count("a") == 4076
    assert joined.count("b") == tail_len

    if tail_len == 4093:
        assert [len(c) for c in chunks] == [4084, 4096, 9], [len(c) for c in chunks]


def test_chunk_reserve_applies_when_the_fence_opens_in_a_later_chunk() -> None:
    """The reserve is computed once for the WHOLE text, not per chunk.

    ``reserve = 8 if "```" in text else 0`` looks at the fence's presence
    anywhere in the message, so a chunk that — taken on its own — has no fence
    in it yet must still respect the reserve: a later chunk's re-opened fence
    is what could otherwise push an EARLIER, fence-free chunk's neighbour past
    the limit once accounted for. This fixture opens its fence only in the
    second cut, not the first, so the reserve is proven live before there is
    any fence text to see.
    """
    adapter = _adapter()
    prose = "x" * 5000  # no fence marker anywhere in it, and over one chunk alone
    body = "\n".join("line %04d" % i for i in range(400))
    text = prose + "\n```\n" + body + "\n```\nafter"
    assert "```" not in text[: _LIMIT - 8], "fixture must not open a fence in chunk 1"

    chunks = adapter._chunk(text)

    assert all(len(c) <= _LIMIT for c in chunks), [len(c) for c in chunks]
    assert "```" not in chunks[0], "the first chunk must still be plain prose"
    assert "```" in chunks[1], "the fence must open in the second chunk"
    # Every individual chunk is internally balanced: nothing is left open
    # across a message boundary.
    for chunk in chunks:
        fences = [ln for ln in chunk.split("\n") if ln.lstrip().startswith("```")]
        assert len(fences) % 2 == 0, chunk[:80]
    assert chunks[-1].endswith("after")
    rejoined = "".join(chunks)
    assert "line 0000" in rejoined and "line 0399" in rejoined
