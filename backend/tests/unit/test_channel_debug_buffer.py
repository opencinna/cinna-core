"""ChannelDebugBuffer bounds and isolation.

Pure in-memory logic with no I/O, so it is unit-tested rather than driven
through the API (same treatment as ``GoogleChatAdapter._chunk``). The two
properties worth pinning are the ones that keep an admin convenience from
becoming an unbounded, cross-channel leak: the ring buffer discards oldest,
and the text clamp holds — a channel being probed must not be able to grow
this without limit.
"""
import uuid

import pytest

from app.core.config import settings
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_RECEIVED,
    ChannelDebugBuffer,
)


@pytest.fixture(autouse=True)
def _reset_buffer():
    """The buffer is process-global class state — isolate every test."""
    ChannelDebugBuffer.reset()
    yield
    ChannelDebugBuffer.reset()


def _record(channel_id, summary="s", text=None):
    ChannelDebugBuffer.record(
        channel_id=channel_id,
        direction="inbound",
        kind=DEBUG_RECEIVED,
        summary=summary,
        text=text,
    )


def test_ring_buffer_keeps_the_newest_and_drops_the_oldest() -> None:
    channel_id = uuid.uuid4()
    limit = settings.SERVER_CHANNEL_DEBUG_BUFFER_SIZE
    for i in range(limit + 10):
        _record(channel_id, summary=f"event-{i}")

    events = ChannelDebugBuffer.list_events(channel_id)
    assert len(events) == limit
    # Newest first, and the ten oldest are gone rather than the ten newest.
    assert events[0].summary == f"event-{limit + 9}"
    assert events[-1].summary == "event-10"


def test_long_text_is_clamped_and_marked() -> None:
    channel_id = uuid.uuid4()
    limit = settings.SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS
    _record(channel_id, text="x" * (limit + 500))

    stored = ChannelDebugBuffer.list_events(channel_id)[0].text
    assert stored is not None
    # Truncation is visible, not silent — a clipped message that looks whole
    # would send an admin chasing a payload bug that isn't there.
    assert stored.endswith("(truncated)")
    assert stored.startswith("x" * 100)
    assert len(stored) < limit + 500


def test_buffers_are_isolated_per_channel() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    _record(first, summary="first-channel")
    _record(second, summary="second-channel")

    assert [e.summary for e in ChannelDebugBuffer.list_events(first)] == [
        "first-channel"
    ]
    assert [e.summary for e in ChannelDebugBuffer.list_events(second)] == [
        "second-channel"
    ]

    ChannelDebugBuffer.clear(first)
    assert ChannelDebugBuffer.list_events(first) == []
    # Clearing one channel must not clear its neighbours.
    assert len(ChannelDebugBuffer.list_events(second)) == 1


def test_recording_never_raises_on_bad_input() -> None:
    """Every call site is on the request path — a debug aid must not be able
    to fail an inbound webhook or an outbound delivery."""
    # A non-serializable channel id and a non-string text: neither is a legal
    # call, and neither may propagate.
    ChannelDebugBuffer.record(
        channel_id=object(),  # type: ignore[arg-type]
        direction="inbound",
        kind=DEBUG_RECEIVED,
        summary="bad",
        text=object(),  # type: ignore[arg-type]
    )


def test_unknown_channel_reads_empty_rather_than_raising() -> None:
    assert ChannelDebugBuffer.list_events(uuid.uuid4()) == []
    ChannelDebugBuffer.clear(uuid.uuid4())


def test_identical_events_collapse_instead_of_flushing_the_buffer() -> None:
    """A repeated event must not be able to evict the feed an admin is reading.

    The ring holds a bounded number of entries and the webhook is reachable by
    anyone holding the token, so without collapsing, repeating one request
    enough times pushes every real event out. Consecutive identical events fold
    into a single row carrying a count, and ``at`` becomes the latest
    occurrence.
    """
    channel_id = uuid.uuid4()
    limit = settings.SERVER_CHANNEL_DEBUG_BUFFER_SIZE

    _record(channel_id, summary="a real message")
    for _ in range(limit * 3):
        ChannelDebugBuffer.record(
            channel_id=channel_id,
            direction="inbound",
            kind=DEBUG_RECEIVED,
            summary="bad signature",
            detail={"stage": "verify"},
        )

    events = ChannelDebugBuffer.list_events(channel_id)
    assert len(events) == 2
    assert events[0].repeat == limit * 3
    # The point of the whole exercise: the genuine event is still there.
    assert events[1].summary == "a real message"
    assert events[1].repeat == 1


def test_only_consecutive_identical_events_collapse() -> None:
    """Collapsing must not merge across an intervening different event —
    that would reorder history and hide the interleaving."""
    channel_id = uuid.uuid4()
    _record(channel_id, summary="same")
    _record(channel_id, summary="same")
    _record(channel_id, summary="different")
    _record(channel_id, summary="same")

    events = ChannelDebugBuffer.list_events(channel_id)
    assert [(e.summary, e.repeat) for e in events] == [
        ("same", 1),
        ("different", 1),
        ("same", 2),
    ]


def test_events_differing_only_in_detail_do_not_collapse() -> None:
    """`detail` carries the pipeline stage — two rejections at different stages
    are different facts and must stay separate rows."""
    channel_id = uuid.uuid4()
    for stage in ("verify", "whitelist"):
        ChannelDebugBuffer.record(
            channel_id=channel_id,
            direction="inbound",
            kind=DEBUG_RECEIVED,
            summary="rejected",
            detail={"stage": stage},
        )

    events = ChannelDebugBuffer.list_events(channel_id)
    assert len(events) == 2
    assert {e.detail["stage"] for e in events} == {"verify", "whitelist"}
