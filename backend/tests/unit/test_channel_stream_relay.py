"""Unit tests for ``ChannelStreamRelay`` and its supporting pieces.

Covers the Phase 2 module of ``docs/plans/google_chat_streaming_updates_plan.md``:
``ChannelStreamRelay`` (feed/flush/seal/take_tail), ``ChannelStreamRegistry``,
``ChannelRelayEventHandler`` and ``maybe_attach_channel_relay`` — all in
``app/services/server_channels/channel_stream_relay.py`` — plus
``CompositeStreamEventHandler`` in ``app/services/sessions/stream_event_handlers.py``,
which the relay is teed onto the UI handler through.

Pure logic with fakes: no DB, no ``TestClient``, no real HTTP. The one real
outbound seam, ``ChannelOutboundService.set_binding_status``, is mocked out —
see ``backend/tests/README.md`` ("Unit Tests") for why that is the right line
to mock rather than reaching for a database. ``CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS``
is patched to ``0`` everywhere a flush is expected, per the module's own
documented meaning of that value ("flush immediately on every event") — so
these tests never sleep for a real debounce interval.

Run: cd backend && python -m pytest tests/unit/test_channel_stream_relay.py -v
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import contextmanager
from typing import Any
from unittest import mock

from app.services.server_channels.channel_stream_relay import (
    ChannelRelayEventHandler,
    ChannelStreamRegistry,
    ChannelStreamRelay,
    maybe_attach_channel_relay,
)
from app.services.sessions.stream_event_handlers import CompositeStreamEventHandler

_MODULE = "app.services.server_channels.channel_stream_relay"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeChannel:
    """Stands in for a ``ServerChannel`` row — only what the relay reads."""

    def __init__(self, channel_type: str = "google_chat", enabled: bool = True) -> None:
        self.id = uuid.uuid4()
        self.channel_type = channel_type
        self.enabled = enabled


class _FakeBinding:
    """Stands in for a ``ChannelThreadBinding`` row — an opaque id is enough.

    ``ChannelOutboundService.set_binding_status`` is mocked in every test that
    reaches it, so the relay never actually reads binding attributes.
    """

    def __init__(self) -> None:
        self.id = uuid.uuid4()


class _FakeDB:
    """Minimal stand-in for the DB session the relay re-fetches rows from.

    Keyed by id alone (not id *and* model) since ``binding.id`` and
    ``channel.id`` are independently random UUIDs in every test — good enough
    for ``_resolve``'s two ``db.get(Model, id)`` calls.
    """

    def __init__(self, rows: dict[uuid.UUID, Any]) -> None:
        self._rows = rows

    def get(self, model: Any, obj_id: Any) -> Any:
        return self._rows.get(obj_id)


def _session_factory(db: Any):
    """A ``get_fresh_db_session`` that always hands back the same fake ``db``."""

    @contextmanager
    def _factory():
        yield db

    return _factory


def _make_relay(
    *, channel: _FakeChannel | None = None, binding: _FakeBinding | None = None
) -> ChannelStreamRelay:
    channel = channel or _FakeChannel()
    binding = binding or _FakeBinding()
    db = _FakeDB({binding.id: binding, channel.id: channel})
    return ChannelStreamRelay(
        session_id=uuid.uuid4(),
        binding_id=binding.id,
        channel_id=channel.id,
        get_fresh_db_session=_session_factory(db),
    )


async def _shutdown(relay: ChannelStreamRelay) -> None:
    """Stop the flusher and wait it out, so no task outlives the test."""
    relay.stop()
    task = relay._task
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, sleeping in small real increments.

    Bounded (2s) so a genuine regression fails fast instead of hanging; the
    increments themselves (0.01s) are what let a passing test finish in
    milliseconds, since ``CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS`` is patched
    to 0 in every test that uses this.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


def _zero_interval():
    return mock.patch(f"{_MODULE}.settings.CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS", 0)


def _seal_target(value: int):
    return mock.patch(f"{_MODULE}.settings.CHANNEL_STREAM_SEAL_TARGET_CHARS", value)


def _mock_set_binding_status(*, return_value: bool = True, side_effect=None):
    kwargs: dict[str, Any] = {}
    if side_effect is not None:
        kwargs["side_effect"] = side_effect
    else:
        kwargs["return_value"] = return_value
    return mock.patch(
        f"{_MODULE}.ChannelOutboundService.set_binding_status",
        new_callable=mock.AsyncMock,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# feed() / flush(): small text, single patch, translated
# ---------------------------------------------------------------------------

def test_small_feed_produces_one_patch_with_translated_suffix() -> None:
    """Interval <= 0: a small feed is flushed once, carrying the raw suffix.

    NOTE ("translated" clarified by running, not assumed): ``_flush`` runs
    ``markdown_to_chat`` only to *measure* how long the patch would render
    (seal/clamp decisions) — the text handed to
    ``ChannelOutboundService.set_binding_status`` is the accumulated **raw**
    markdown. The actual Markdown → Chat-markup translation happens one layer
    down, inside the adapter's own ``send_message`` / ``update_message`` /
    ``replace_message`` (each calls ``markdown_to_chat`` right before the wire
    call — see ``google_chat.py``). So this test asserts the patch carries the
    fed markdown unchanged.
    """

    async def run() -> None:
        with _zero_interval(), _mock_set_binding_status(return_value=True) as sbs:
            relay = _make_relay()
            try:
                relay.feed("Hello **world**")
                await _wait_for(lambda: sbs.call_count >= 1)

                # Give the loop one more idle cycle to prove no second patch
                # sneaks in for a single small feed.
                await asyncio.sleep(0.03)
                assert sbs.call_count == 1

                call = sbs.call_args_list[0]
                assert call.kwargs["text"] == "Hello **world**"  # raw, unmutated
                assert call.kwargs.get("settle", False) is False
            finally:
                await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Seal: crossing the target settles the sealed slice, then patches the rest
# ---------------------------------------------------------------------------

def test_feed_past_seal_target_settles_then_patches_remainder() -> None:
    """Crossing the seal target seals the first paragraph and drafts the rest."""

    async def run() -> None:
        head = "A" * 150  # a full paragraph, comfortably above window // 2
        tail = "B" * 150  # what continues in the fresh draft
        text = f"{head}\n\n{tail}"

        with _zero_interval(), _seal_target(200), _mock_set_binding_status(
            return_value=True
        ) as sbs:
            relay = _make_relay()
            try:
                relay.feed(text)
                await _wait_for(lambda: sbs.call_count >= 2)
                await asyncio.sleep(0.03)
                assert sbs.call_count == 2

                seal_call, patch_call = sbs.call_args_list
                assert seal_call.kwargs["text"] == head
                assert seal_call.kwargs["settle"] is True

                assert patch_call.kwargs["text"] == tail
                assert patch_call.kwargs.get("settle", False) is False
            finally:
                await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# take_tail() idempotency
# ---------------------------------------------------------------------------

def test_take_tail_second_call_returns_empty() -> None:
    """A second ``take_tail`` call answers ('', same delivered flag) — no re-send."""

    async def run() -> None:
        relay = _make_relay()
        try:
            relay.feed("hello world")
            tail1, delivered1 = await relay.take_tail()
            assert tail1 == "hello world"

            tail2, delivered2 = await relay.take_tail()
            assert tail2 == ""
            assert delivered2 == delivered1
        finally:
            await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# feed() after stop(): the buffer keeps accepting text (regression sentinel)
# ---------------------------------------------------------------------------

def test_feed_keeps_accepting_text_after_stop() -> None:
    """``stop()`` governs the flusher, not the buffer — later batches must land.

    Guards the bug shape from the plan: ``STREAM_COMPLETED`` fires once per
    LLM batch and the outbound handler stops the relay after taking a batch's
    tail, so a latched buffer would silently drop batch 2+ of a multi-batch
    turn.
    """

    async def run() -> None:
        with _mock_set_binding_status(return_value=True) as sbs:
            relay = _make_relay()
            try:
                relay.feed("first batch. ")
                relay.stop()  # stop right after the first feed, before any await
                tail1, delivered1 = await relay.take_tail()
                assert tail1 == "first batch. "
                # The flusher never got a chance to run (stopped before any
                # checkpoint), so nothing was ever sent.
                assert delivered1 is False
                assert sbs.call_count == 0

                relay.feed("second batch.")  # must still be accepted post-stop
                tail2, delivered2 = await relay.take_tail()
                assert tail2 == "second batch."
            finally:
                await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Forced mid-fence seal: sets, then uses, the fence prefix
# ---------------------------------------------------------------------------

def test_forced_mid_fence_seal_sets_and_uses_fence_prefix() -> None:
    """An oversized, still-open code fence forces a seal that re-opens itself.

    The draft is built as one giant unterminated ``` python ``` block — no
    paragraph or line boundary outside a fence exists, so ``_choose_seal``
    cannot find an ordinary boundary and must fall through to
    :meth:`ChannelStreamRelay._forced_seal`. Only that path sets
    ``_fence_prefix``; this test proves it is also *used* on the very next
    flush (the second patch's text starts with the reopened fence).
    """

    async def run() -> None:
        # Comfortably over the 4096-char hard cap once translated (code fences
        # are passed through unchanged by markdown_to_chat, so raw ~= translated
        # length here).
        lines = "\n".join(f"line_{i} = {i}" for i in range(400))
        draft = f"Here is some code:\n\n```python\n{lines}\n"
        assert len(draft) > 4200  # sanity: comfortably forces the seal

        with _zero_interval(), _mock_set_binding_status(return_value=True) as sbs:
            relay = _make_relay()
            try:
                relay.feed(draft)
                # One flush produces exactly two calls (seal, then the
                # immediate remainder patch) — wait for both before asserting.
                await _wait_for(lambda: sbs.call_count >= 2)
                await asyncio.sleep(0.03)

                calls = sbs.call_args_list
                seal_calls = [c for c in calls if c.kwargs.get("settle")]
                assert len(seal_calls) == 1
                sealed_text = seal_calls[0].kwargs["text"]
                # The forced seal must close the block it interrupted with the
                # SAME marker it opened with — this is what "sets" the prefix.
                assert sealed_text.rstrip().endswith("```")
                assert relay._fence_prefix == "```\n"

                patch_calls = [c for c in calls if not c.kwargs.get("settle")]
                assert patch_calls, "expected an immediate remainder patch after the seal"
                # "...and then uses it": the very next patch re-opens the block.
                assert patch_calls[-1].kwargs["text"].startswith("```\n")

                # Feed more (still not closing the fence) and force another
                # flush — the reopened prefix must still be there.
                before = sbs.call_count
                relay.feed("more_code = 1\n")
                await _wait_for(lambda: sbs.call_count > before)
                await asyncio.sleep(0.03)

                latest = sbs.call_args_list[-1]
                assert not latest.kwargs.get("settle")
                assert latest.kwargs["text"].startswith("```\n")
            finally:
                await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Fence marker identity is preserved on a forced seal (direct, sync)
# ---------------------------------------------------------------------------

def _forced_seal_case(marker: str) -> tuple[str, int, str]:
    """Build a draft opened with ``marker`` and force-seal it via ``_forced_seal``.

    Direct call to the private helper (allowed for unit tests per
    ``tests/README.md``) — no event loop, no mock, no flush pipeline needed to
    pin this one fact: the reopening marker must be the exact run the block
    opened with.
    """
    draft = f"{marker}\nline1\nline2\nline3\nline4"
    relay = _make_relay()
    result = relay._forced_seal(draft, window=len(draft), limit=4096)
    assert result is not None
    return result


def test_forced_seal_reopens_a_tilde_fence_with_tildes_not_backticks() -> None:
    sealed, cut, next_prefix = _forced_seal_case("~~~")
    assert sealed.endswith("\n~~~")
    assert "```" not in sealed  # must not have substituted the wrong marker
    assert next_prefix == "~~~\n"
    # The remainder must still have content to continue with.
    draft = "~~~\nline1\nline2\nline3\nline4"
    assert draft[cut:] == "line4"


def test_forced_seal_reopens_a_four_backtick_fence_with_four_backticks() -> None:
    sealed, cut, next_prefix = _forced_seal_case("````")
    assert sealed.endswith("\n````")
    assert next_prefix == "````\n"
    draft = "````\nline1\nline2\nline3\nline4"
    assert draft[cut:] == "line4"


# ---------------------------------------------------------------------------
# A failed send must not advance the sealed offset
# ---------------------------------------------------------------------------

def test_failed_send_does_not_lose_the_draft() -> None:
    """``set_binding_status`` returning False must not cost the reader the text.

    Regression guard: advancing past a slice on an unconfirmed send would
    leave a silent hole in the middle of the answer — ``take_tail`` must still
    return the fed text byte-for-byte, and report nothing was delivered.
    """

    async def run() -> None:
        with _zero_interval(), _mock_set_binding_status(return_value=False) as sbs:
            relay = _make_relay()
            try:
                relay.feed("important text that must not be lost")
                await _wait_for(lambda: sbs.call_count >= 1)
                await asyncio.sleep(0.03)

                tail, delivered = await relay.take_tail()
                assert tail == "important text that must not be lost"
                assert delivered is False
            finally:
                await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# A raising outbound verb neither propagates nor stops subsequent flushes
# ---------------------------------------------------------------------------

def test_raising_outbound_verb_does_not_propagate_or_stop_the_flusher() -> None:
    async def run() -> None:
        with _zero_interval(), _mock_set_binding_status(
            side_effect=[RuntimeError("boom"), True]
        ) as sbs:
            relay = _make_relay()
            try:
                relay.feed("first ")
                await _wait_for(lambda: sbs.call_count >= 1)
                await asyncio.sleep(0.03)
                # The exception must not have killed the flusher task.
                assert relay._task is not None
                assert not relay._task.done()

                relay.feed("second")
                await _wait_for(lambda: sbs.call_count >= 2)
                await asyncio.sleep(0.03)

                # Nothing was ever confirmed sent, so the whole buffer is still
                # in the tail — the failed first attempt did not drop "first ".
                tail, delivered = await relay.take_tail()
                assert tail == "first second"
            finally:
                await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# ChannelRelayEventHandler: only non-empty assistant text reaches the draft
# ---------------------------------------------------------------------------

def test_relay_event_handler_ignores_thinking_and_empty_assistant_events() -> None:
    async def run() -> None:
        relay = _make_relay()
        handler = ChannelRelayEventHandler(relay)
        try:
            await handler.on_event({"type": "assistant", "content": "Hello "})
            await handler.on_event({"type": "thinking", "content": "secret reasoning"})
            await handler.on_event({"type": "assistant", "content": ""})
            await handler.on_event({"type": "assistant", "content": None})
            await handler.on_event({"type": "assistant", "content": "world"})
            await handler.on_event({"type": "tool", "content": "used a tool"})

            buffered = relay._text()
            assert buffered == "Hello world"
            assert "secret reasoning" not in buffered
            assert "used a tool" not in buffered

            await handler.on_complete("Hello world")
            assert relay.stopped is True
        finally:
            await _shutdown(relay)

    asyncio.run(run())


def test_relay_event_handler_stops_relay_on_error() -> None:
    async def run() -> None:
        relay = _make_relay()
        handler = ChannelRelayEventHandler(relay)
        try:
            assert relay.stopped is False
            await handler.on_error(RuntimeError("stream blew up"))
            assert relay.stopped is True
        finally:
            await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# maybe_attach_channel_relay: decline clears any stale registry entry
# ---------------------------------------------------------------------------

def test_maybe_attach_declines_and_clears_stale_registry_entry() -> None:
    session_id = uuid.uuid4()
    base_handler = object()
    stale_relay = _make_relay()
    ChannelStreamRegistry.put(session_id, stale_relay)

    try:
        with mock.patch(
            f"{_MODULE}.ChannelOutboundService._resolve_channel_session",
            return_value=None,  # binding/channel gone → decline to attach
        ):
            result = maybe_attach_channel_relay(
                session_id=session_id,
                integration_type="channel_google_chat",
                base_handler=base_handler,
                get_fresh_db_session=_session_factory(_FakeDB({})),
            )

        assert result is base_handler
        assert ChannelStreamRegistry.get(session_id) is None
    finally:
        ChannelStreamRegistry.remove(session_id)


def test_maybe_attach_is_total_and_clears_registry_on_internal_exception() -> None:
    """Any internal failure degrades to "no relay", and never raises."""

    session_id = uuid.uuid4()
    base_handler = object()
    stale_relay = _make_relay()
    ChannelStreamRegistry.put(session_id, stale_relay)

    try:
        with mock.patch(
            f"{_MODULE}.ChannelOutboundService._resolve_channel_session",
            side_effect=RuntimeError("db exploded"),
        ):
            result = maybe_attach_channel_relay(
                session_id=session_id,
                integration_type="channel_google_chat",
                base_handler=base_handler,
                get_fresh_db_session=_session_factory(_FakeDB({})),
            )

        assert result is base_handler
        assert ChannelStreamRegistry.get(session_id) is None
    finally:
        ChannelStreamRegistry.remove(session_id)


def test_maybe_attach_returns_base_handler_unchanged_for_non_channel_session() -> None:
    session_id = uuid.uuid4()
    base_handler = object()

    result = maybe_attach_channel_relay(
        session_id=session_id,
        integration_type="ui",
        base_handler=base_handler,
        get_fresh_db_session=_session_factory(_FakeDB({})),
    )

    assert result is base_handler
    assert ChannelStreamRegistry.get(session_id) is None


def test_maybe_attach_builds_a_composite_handler_when_eligible() -> None:
    session_id = uuid.uuid4()
    base_handler = object()
    binding = _FakeBinding()
    channel = _FakeChannel(channel_type="google_chat", enabled=True)

    try:
        with mock.patch(
            f"{_MODULE}.ChannelOutboundService._resolve_channel_session",
            return_value=(binding, channel),
        ):
            result = maybe_attach_channel_relay(
                session_id=session_id,
                integration_type="channel_google_chat",
                base_handler=base_handler,
                get_fresh_db_session=_session_factory(_FakeDB({})),
            )

        assert isinstance(result, CompositeStreamEventHandler)
        assert base_handler in result.handlers
        registered = ChannelStreamRegistry.get(session_id)
        assert registered is not None
        assert registered.binding_id == binding.id
        assert registered.channel_id == channel.id
    finally:
        ChannelStreamRegistry.remove(session_id)


# ---------------------------------------------------------------------------
# CompositeStreamEventHandler: the passenger is isolated, the primary is not
# ---------------------------------------------------------------------------

class _RaisingHandler:
    async def on_event(self, event: dict) -> None:
        raise RuntimeError("boom")


class _RecordingHandler:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def on_event(self, event: dict) -> None:
        self.events.append(event)


def test_composite_handler_isolates_a_failing_passenger() -> None:
    """A relay that cannot reach its channel must not derail the stream."""

    async def run() -> None:
        recorder = _RecordingHandler()
        composite = CompositeStreamEventHandler(recorder, [_RaisingHandler()])

        await composite.on_event({"type": "assistant", "content": "x"})  # must not raise

        assert recorder.events == [{"type": "assistant", "content": "x"}]

    asyncio.run(run())


def test_composite_handler_propagates_a_primary_failure() -> None:
    """The primary's contract with the processor is unchanged by composing.

    ``WebSocketEventHandler.on_complete`` is what writes
    ``pending_messages_count`` / ``interaction_status`` back. Swallowing its
    failure (which the composite used to do, isolating every child alike) left
    a web client watching a channel session stuck on a stale "streaming" state
    until a poll re-derived it — a regression visible only on the composed
    sessions, i.e. exactly the ones this class exists for.
    """

    async def run() -> None:
        passenger = _RecordingHandler()
        composite = CompositeStreamEventHandler(_RaisingHandler(), [passenger])

        try:
            await composite.on_event({"type": "assistant", "content": "y"})
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("the primary's failure must reach the caller")

        # ...and the passenger still got the event. A relay that missed an
        # assistant chunk because somebody else's Socket.IO emit failed would
        # lose text it is the only holder of.
        assert passenger.events == [{"type": "assistant", "content": "y"}]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Agent control tags never reach the thread (F2)
# ---------------------------------------------------------------------------

def test_control_tags_are_stripped_from_both_the_draft_and_the_tail() -> None:
    """``<cinna_attach>`` / ``<webapp_action>`` are stripped at *finalize*.

    The relay reads the stream, which carries assistant content raw — and the
    settled reply is the relay's own accumulated text by decision (plan §1), so
    a tag that survives here reaches the reader's final message too, not just
    the live draft. Channel file attachments are a shipped feature, so this
    input is the ordinary one.
    """

    async def run() -> None:
        text = (
            "Here's the report.\n"
            "<cinna_attach>/app/workspace/report.pdf</cinna_attach>\n"
            'Done.<webapp_action>{"action": "open"}</webapp_action>'
        )
        with _zero_interval(), _mock_set_binding_status(return_value=True) as sbs:
            relay = _make_relay()
            try:
                relay.feed(text)
                await _wait_for(lambda: sbs.call_count >= 1)
                await asyncio.sleep(0.03)

                patched = sbs.call_args_list[-1].kwargs["text"]
                assert "cinna_attach" not in patched
                assert "webapp_action" not in patched
                assert "report.pdf" not in patched
                assert "Here's the report." in patched and "Done." in patched

                tail, _ = await relay.take_tail()
                assert "cinna_attach" not in tail
                assert "webapp_action" not in tail
                assert "Here's the report." in tail and "Done." in tail
            finally:
                await _shutdown(relay)

    asyncio.run(run())


def test_a_half_arrived_tag_does_not_flicker_through_the_draft() -> None:
    """A tag arrives character by character, like everything else.

    Without suppression the reader watches ``<cinna_attach>/app/works…`` type
    itself out over a few flushes and then vanish. The text is not lost — the
    buffer keeps it and the completed tag is stripped whole.
    """

    async def run() -> None:
        with _zero_interval(), _mock_set_binding_status(return_value=True) as sbs:
            relay = _make_relay()
            try:
                relay.feed("Here's the report.\n<cinna_att")
                await _wait_for(lambda: sbs.call_count >= 1)
                await asyncio.sleep(0.03)
                assert sbs.call_args_list[-1].kwargs["text"].strip() == (
                    "Here's the report."
                )

                # The opening has landed but the closing tag has not: still
                # nothing to show for it.
                before = sbs.call_count
                relay.feed("ach>/app/workspace/report.pdf")
                await _wait_for(lambda: sbs.call_count > before)
                await asyncio.sleep(0.03)
                latest = sbs.call_args_list[-1].kwargs["text"]
                assert "report.pdf" not in latest
                assert latest.strip() == "Here's the report."

                # Completed, stripped whole, and the text after it flows on.
                before = sbs.call_count
                relay.feed("</cinna_attach>\nAnything else?")
                await _wait_for(lambda: sbs.call_count > before)
                await asyncio.sleep(0.03)
                latest = sbs.call_args_list[-1].kwargs["text"]
                assert "cinna_att" not in latest
                assert "Anything else?" in latest
            finally:
                await _shutdown(relay)

    asyncio.run(run())


def test_tag_openings_match_the_shared_patterns() -> None:
    """Drift guard: the literals must be the imported patterns' own openings.

    The regexes are imported from ``message_service`` precisely so this module
    owns no second model of them, but a *partial* tag has no regex to match —
    the openings have to be literals. This is what keeps the two honest.
    """
    from app.services.server_channels.channel_stream_relay import (
        _AGENT_TAG_OPENINGS,
        _ATTACH_TAG_RE,
        _WEBAPP_ACTION_TAG_RE,
    )

    patterns = {_ATTACH_TAG_RE.pattern, _WEBAPP_ACTION_TAG_RE.pattern}
    assert len(_AGENT_TAG_OPENINGS) == len(patterns)
    for opening in _AGENT_TAG_OPENINGS:
        assert any(p.startswith(opening) for p in patterns), opening


# ---------------------------------------------------------------------------
# take_tail() reports failure as None, not as "nothing" (F1)
# ---------------------------------------------------------------------------

def test_take_tail_answers_none_when_it_fails_internally() -> None:
    """``None`` is *relay-absent*, and it must not look like an empty stream.

    ``("", False)`` sends the consumer down the "this stream produced nothing"
    branch, which ends in ``clear_binding_status`` — the notice deleted and
    nothing sent, for a reply that is sitting in ``SessionMessage``. The two
    have to be distinguishable, so a failure in here answers ``None``.
    """

    async def run() -> None:
        relay = _make_relay()
        try:
            relay.feed("an answer that exists")
            with mock.patch.object(
                relay, "_draft", side_effect=RuntimeError("boom")
            ):
                assert await relay.take_tail() is None

            # Nothing was consumed by the failed call: the text is still there
            # for whoever asks next.
            tail, _ = await relay.take_tail()
            assert tail == "an answer that exists"
        finally:
            await _shutdown(relay)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Expanding content reaches the boundary path (F3)
# ---------------------------------------------------------------------------

def _wide_table_draft() -> str:
    """A draft whose translation is several times its raw length.

    ``markdown_to_chat`` renders a pipe table as an aligned monospace block,
    padding every cell to the widest one in its column — so one wide row makes
    the whole table expand. Raw ~1.1k, translated ~5.9k: past the seal target
    while still far shorter than a window of ``target`` raw characters.
    """
    wide = (
        "a very long description cell that forces every other row to pad out "
        "to this width, and then some more text to make it wider still"
    )
    rows = "\n".join(
        f"| item-{i:03d} | {wide if i == 0 else 'xxx'} | 12 |" for i in range(40)
    )
    return "Summary:\n\n| name | description | n |\n|---|---|---|\n" + rows + "\n"


def test_expanding_content_seals_at_a_boundary_rather_than_being_forced() -> None:
    """The seal trigger is translated; the window is raw. They must still meet.

    ``find_seal_boundary`` answers ``None`` both for "no boundary here" and for
    "the text is shorter than the window", and a window seeded at the raw seal
    target is longer than any draft that expands under translation. So every
    such draft used to skip the boundary path entirely and fall to
    ``_forced_seal``, which cuts blind — the reader watched a table stop
    mid-row. Seeding from the draft and halving on ``None`` is what puts it
    back on the boundary path.
    """
    from app.services.server_channels.adapters.chat_text_chunking import (
        find_seal_boundary,
    )
    from app.services.server_channels.adapters.google_chat_format import (
        markdown_to_chat,
    )

    draft = _wide_table_draft()
    assert len(markdown_to_chat(draft)) > 3400 > len(draft), "not an expanding draft"

    relay = _make_relay()
    # The trap itself: at a window of ``target`` raw characters there is no
    # boundary to be had, because the draft is shorter than the window.
    assert find_seal_boundary(draft, 3400) is None

    with mock.patch.object(
        relay,
        "_forced_seal",
        side_effect=AssertionError("fell through to the forced cut"),
    ):
        seal = relay._choose_seal(draft, 3400, 4096)

    assert seal is not None, "the boundary path was never reached"
    sealed, cut, next_prefix = seal
    # A real boundary: outside every fence, so no re-opening prefix.
    assert next_prefix == ""
    assert draft[cut:], "the remainder must be non-empty"
    # ...and it fits for real once translated, which is the whole point of
    # measuring the slice rather than the window.
    assert len(markdown_to_chat(sealed)) <= 4096 - 96


def test_plain_prose_still_seals_at_a_paragraph_break() -> None:
    """The happy path is untouched: same window, same cut, no fence prefix."""
    paragraph = (
        "This is an ordinary paragraph of the agent's answer, long enough to "
        "be realistic. " * 5
    ).strip()
    draft = "\n\n".join([paragraph] * 9) + "\n\nand a final sentence."

    relay = _make_relay()
    seal = relay._choose_seal(draft, 3400, 4096)
    assert seal is not None
    sealed, cut, next_prefix = seal
    assert next_prefix == ""
    assert sealed.endswith("realistic.")
    assert 3000 < len(sealed) <= 3400
    assert draft[cut:].startswith("This is an ordinary paragraph")


# ---------------------------------------------------------------------------
# Retirement, spent-ness and eviction (F4 / F9 / F10)
# ---------------------------------------------------------------------------

def test_an_idle_flusher_retires_and_the_next_feed_starts_it_again() -> None:
    """The cancelled turn is the ending that never reaches ``stop()``.

    ``CancelledError`` is a ``BaseException``, so it slips past the stream's
    ``except Exception`` and ``on_complete`` never runs. The flusher retires on
    its idle timeout instead — and must *say* so, or the registry (which used
    to evict only stopped relays) holds the whole buffer for the life of the
    process. ``_retired`` is deliberately not ``_stopped``: the turn may still
    be alive, and a later feed restarts the flusher.
    """

    async def run() -> None:
        with _zero_interval(), mock.patch(
            f"{_MODULE}._IDLE_EXIT_SECONDS", 0.05
        ), _mock_set_binding_status(return_value=True):
            relay = _make_relay()
            try:
                relay.feed("half an answer")
                await _wait_for(lambda: relay.retired)

                assert relay.stopped is False  # nothing ever stopped it
                assert relay.evictable is True  # ...and the registry can reap

                relay.feed("the rest of it")
                await _wait_for(lambda: not relay.retired)
                assert relay._task is not None and not relay._task.done()
            finally:
                await _shutdown(relay)

    asyncio.run(run())


def test_spent_is_only_true_for_a_turn_that_ended_and_was_consumed() -> None:
    """The discriminator a consumer uses to tell "mine" from "last turn's".

    A stopped relay is NOT stale on its own — ``on_complete`` stops the relay
    and the completion handler runs after it, so that is the ordinary shape of
    the happy path. And a stopped, consumed relay that has been fed again is a
    multi-batch turn whose next increment is still owed.
    """

    async def run() -> None:
        relay = _make_relay()
        try:
            relay.feed("batch one")
            assert relay.spent is False  # live

            relay.stop()
            assert relay.spent is False  # stopped, but nobody has taken it

            await relay.take_tail()
            assert relay.spent is True  # over, and handed over

            relay.feed("batch two")
            assert relay.spent is False  # ...until this arrives
        finally:
            await _shutdown(relay)

    asyncio.run(run())


def test_registry_evicts_a_cancelled_turns_relay_but_not_a_pending_one() -> None:
    """Eviction is on ``evictable``, which is what makes the cap a cap.

    Both entries here are stopped-or-retired, and the discrimination is the
    whole point. The **cancelled** turn retired without ever being stopped —
    ``CancelledError`` is a ``BaseException``, so ``on_complete`` never ran and
    no consumer is coming; holding it would leak its whole buffer for the life
    of the process. The **pending** one was stopped normally and its flusher
    has already retired, but its completion handler has not run yet: evicting
    it would send that handler down the full-text path with a partly delivered
    draft already standing, which is the duplicate the relay exists to avoid.
    """

    async def run() -> None:
        saved = dict(ChannelStreamRegistry._relays)
        ChannelStreamRegistry._relays.clear()
        cancelled = _make_relay()
        pending = _make_relay()
        try:
            with _zero_interval(), mock.patch(
                f"{_MODULE}._IDLE_EXIT_SECONDS", 0.05
            ), _mock_set_binding_status(return_value=True):
                # A real cancelled turn: fed, never stopped, flusher timed out.
                cancelled.feed("half an answer")
                await _wait_for(lambda: cancelled.retired)

                # A real finished turn: fed, stopped, flusher exited — and its
                # tail not yet taken.
                pending.feed("the whole answer")
                pending.stop()
                await _wait_for(lambda: pending.retired)

            assert cancelled.evictable is True
            assert pending.evictable is False

            with mock.patch.object(ChannelStreamRegistry, "_MAX_ENTRIES", 2):
                cancelled_id, pending_id, fresh_id = (uuid.uuid4() for _ in range(3))
                ChannelStreamRegistry.put(cancelled_id, cancelled)
                ChannelStreamRegistry.put(pending_id, pending)
                ChannelStreamRegistry.put(fresh_id, _make_relay())

                assert ChannelStreamRegistry.get(cancelled_id) is None
                assert ChannelStreamRegistry.get(pending_id) is pending
                assert ChannelStreamRegistry.get(fresh_id) is not None
        finally:
            await _shutdown(cancelled)
            await _shutdown(pending)
            ChannelStreamRegistry._relays.clear()
            ChannelStreamRegistry._relays.update(saved)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# A seal never cuts a control tag in half
# ---------------------------------------------------------------------------

def test_a_seal_never_cuts_a_multi_line_control_tag_in_half() -> None:
    """Seals cut at newlines; ``<webapp_action>`` bodies are multi-line JSON.

    A cut inside one puts the opening and half the body into a message that is
    **final**, and leaves the closing half at the head of the next draft, where
    nothing can strip it — there is no opening left to pair it with, and it is
    not an opening for the partial-tag suppression to hide either. Both halves
    reach the reader, permanently.

    The shape that reaches it: no paragraph break high enough in the window
    (paragraph breaks are preferred and would land above the tag), so the last
    *line* break wins — and the tag body is full of those. The boundary is
    rejected and the search moves above the tag instead.

    The target below is picked so that the **first** candidate offset lands
    inside the tag body, which is the only way this test exercises the guard
    rather than passing vacuously. It has to be picked relative to the tag's
    position because the seal window is seeded at ``target`` *plus the span the
    tag stripping removes* — a tag costs the search that much raw reach for
    text the reader never sees, and without the compensation a draft like this
    one cannot seal at all. A target that puts the window edge past the closing
    tag would seal cleanly above the remainder and prove nothing here.
    """
    action = (
        "<webapp_action>\n"
        "{\n"
        '  "action": "open_panel",\n'
        '  "target": "report",\n'
        '  "notes": "several lines of JSON, which is why the pattern is DOTALL"\n'
        "}\n"
        "</webapp_action>"
    )
    line = "Some ordinary answer text that the reader is here for, running on. " * 6
    draft = f"{line}\n{line}\n{action}\nand the answer continues after it."

    relay = _make_relay()
    seal = relay._choose_seal(draft, len(line) + 300, 4096)

    assert seal is not None
    sealed, cut, _ = seal
    assert "webapp_action" not in sealed
    assert "open_panel" not in sealed
    # The tag stays whole in the remainder, where ``_visible`` can remove it in
    # one piece once it is delivered.
    remainder = draft[cut:]
    assert remainder.count("<webapp_action>") == 1
    assert remainder.count("</webapp_action>") == 1


# ---------------------------------------------------------------------------
# A seal is whole or deferred — never trimmed to fit
# ---------------------------------------------------------------------------

def test_the_seal_path_never_clamps() -> None:
    """``_clamp_draft`` is safe only on the live draft, and only there.

    A clamped *draft* loses nothing: the next flush rewrites it in full and the
    tail is delivered whole at the end. A clamped *seal* loses that text
    forever — the relay advances past a sealed slice and never offers it again
    — so the seal path's only way to make a slice fit is to reject it and defer
    (``_choose_seal`` → ``None``). This drives a real seal with the clamp
    replaced by a raise, so the separation is executable rather than a comment
    a later refactor can quietly break.
    """

    async def run() -> None:
        head = "A" * 150
        tail = "B" * 150
        with _zero_interval(), _seal_target(200), _mock_set_binding_status(
            return_value=True
        ) as sbs:
            relay = _make_relay()
            boom = mock.patch.object(
                relay,
                "_clamp_draft",
                side_effect=AssertionError("the seal path must not clamp"),
            )
            try:
                # The seal itself runs with the clamp poisoned...
                with boom:
                    async with relay._lock:
                        with relay._open_db() as db:
                            binding, channel = relay._resolve(db)
                            relay.feed(f"{head}\n\n{tail}")
                            left = await relay._seal_down(
                                db, channel, binding, relay._draft(), 4096
                            )

                seal_calls = [c for c in sbs.call_args_list if c.kwargs.get("settle")]
                assert len(seal_calls) == 1
                assert seal_calls[0].kwargs["text"] == head
                assert left == tail
            finally:
                await _shutdown(relay)

    asyncio.run(run())


def test_an_unfittable_slice_is_deferred_rather_than_trimmed() -> None:
    """The other half of the same invariant, at the decision point.

    With a message limit no candidate slice can fit under, ``_choose_seal``
    must answer "not here, not yet" rather than hand back something shortened.
    """
    paragraph = ("A paragraph the reader is owed in full. " * 8).strip()
    draft = "\n\n".join([paragraph] * 6)

    relay = _make_relay()
    # A limit below even the smallest boundary slice: nothing can fit.
    assert relay._choose_seal(draft, 3400, 120) is None


# ---------------------------------------------------------------------------
# A partial tail hides the *trailing* unfinished tag, not an earlier mention
# ---------------------------------------------------------------------------

def test_a_partial_tail_keeps_the_prose_after_a_mentioned_tag() -> None:
    """``take_tail(partial=True)`` settles text; a mention must not delete it.

    ``partial=True`` exists so an interrupted or failed stream cannot settle a
    raw half-typed ``<cinna_attach>/app/wo`` into the thread. It reaches that
    by withholding everything from an unfinished tag opening to the end — which
    is free on a live draft (the next flush shows it again) and **is not free
    here**: this text *is* the settled reply, so a withheld remainder is
    deleted rather than deferred.

    The shape that made it a deletion: these agents document their own
    protocols, so a bare ``<cinna_attach>`` in prose is an ordinary sentence in
    an ordinary answer. Searching the buffer for the *first* opening picked
    that mention and dropped the rest of the reply — while the identical text
    through ``handle_stream_completed`` (``partial=False``) kept all of it. Two
    outcomes for one answer, decided only by how the turn happened to end.

    Both halves are asserted here, because either alone is half a test: the
    prose after the mention survives (the deletion), **and** the genuinely
    truncated tag at the end is still hidden (what the flag is for). A search
    anchored at the wrong end fails the first; deleting the branch outright
    fails the second.
    """

    async def run() -> None:
        relay = _make_relay()
        # Stopped before it is fed, so no flusher task is started and no draft
        # is patched: the buffer still accepts text (the documented contract of
        # ``feed`` after ``stop``) and ``take_tail`` is the only reader.
        relay.stop()
        relay.feed(
            "You can attach files with the <cinna_attach> protocol, "
            "which I will now use.\n"
        )
        relay.feed("<cinna_attach>/app/workspace/rep")

        taken = await relay.take_tail(partial=True)
        # Not ``None``: that is the relay's "I broke" answer, and it would
        # send the caller down the full-text path instead of proving anything
        # about the stripping under test.
        assert taken is not None
        tail, _delivered = taken

        # Exact, trailing newline included: ``take_tail`` withholds from the
        # opening onwards and strips nothing else, so the assertion says what
        # the reader would actually be handed.
        assert tail == (
            "You can attach files with the <cinna_attach> protocol, "
            "which I will now use.\n"
        ), tail
        # The trailing, still-arriving tag is gone: no fragment of the path,
        # and nothing after the opening that begins it.
        assert "/app/workspace/rep" not in tail
        assert tail.count("<cinna_attach>") == 1

    asyncio.run(run())


def test_a_partial_tail_still_hides_a_tag_cut_off_mid_body() -> None:
    """The interrupt case the ``partial`` flag was added for, pinned alone.

    The end-anchored ``"<"`` test in ``_unfinished_tag_start`` cannot see this
    one — the fragment ``"<cinna_attach>/app/wo"`` is *longer* than the opening,
    so no tag literal starts with it — which is why the complete-opening branch
    has to stay. Without it the reader watches "Here is the report:" and gets a
    raw fragment of an internal protocol settled underneath it.
    """

    async def run() -> None:
        relay = _make_relay()
        relay.stop()
        relay.feed("Here is the report.\n<cinna_attach>/app/wo")

        taken = await relay.take_tail(partial=True)
        # Not ``None``: that is the relay's "I broke" answer, and it would
        # send the caller down the full-text path instead of proving anything
        # about the stripping under test.
        assert taken is not None
        tail, _delivered = taken

        assert tail == "Here is the report.\n", tail
        assert "cinna_attach" not in tail

    asyncio.run(run())


def test_a_partial_tail_cuts_at_the_outermost_open_tag_not_the_last_one() -> None:
    """A tag literal inside another tag's body must not become the cut point.

    ``<webapp_action>`` bodies are JSON the agent wrote, and an agent writing
    about attachments puts the literal ``<cinna_attach>`` inside one. The
    search takes the **last** opening *per tag* and then the **earliest** of
    those candidates, so the still-arriving ``<webapp_action>`` wins over the
    ``<cinna_attach>`` it contains. Reversed — "the latest candidate overall",
    which reads like the natural partner to a backwards search — the cut lands
    inside the JSON and ``<webapp_action>{"…`` settles into the thread.
    """

    async def run() -> None:
        relay = _make_relay()
        relay.stop()
        relay.feed(
            "Sure.\n"
            '<webapp_action>\n{"action": "write", '
            '"text": "use <cinna_attach> like so"'
        )

        taken = await relay.take_tail(partial=True)
        # Not ``None``: that is the relay's "I broke" answer, and it would
        # send the caller down the full-text path instead of proving anything
        # about the stripping under test.
        assert taken is not None
        tail, _delivered = taken

        assert tail == "Sure.\n", tail
        assert "webapp_action" not in tail
        assert "cinna_attach" not in tail

    asyncio.run(run())
