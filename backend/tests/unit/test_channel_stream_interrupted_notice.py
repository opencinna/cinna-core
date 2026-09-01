"""What an interrupted channel turn puts in the thread's status notice.

``handle_stream_interrupted`` exists for one reason: an interrupted turn is
the one ending that emits no ``STREAM_COMPLETED``, so without it the notice
stays stranded on "💬 Working on your message…" until some later turn patches
it — telling the person we are still busy with something they cancelled. The
two tests here pin the two shapes that decide whether it does its job, and
they are written as **behaviour** pins: they assert on what reaches the
thread and on nothing else, because the seam underneath them has now been
reorganised twice and both reorganisations shipped against a green suite.

Why these two, specifically. Every other test of this feature drives a relay
that produced text, so the *empty-buffer* shape — a turn stopped before the
agent said anything, which is the most likely way to interrupt a turn at all —
had no coverage whatsoever. Two opposite bugs have lived in that gap:

* the notice left stranded on the spinner, because "the relay produced
  nothing" was indistinguishable from "the relay broke" (test one below), and
* a bare "⏹️ Stopped." settled **over** a live draft, replacing an answer the
  reader watched arrive with a two-word tombstone (test two below).

Each test fails on the tree that carries the other bug, which is what makes
the pair survive a refactor of the seam rather than pinning today's branches.

Pure logic with fakes: no DB, no ``TestClient``, no HTTP. ``asyncio.run``
style, per ``tests/unit/README.md``.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

from app.services.server_channels.channel_outbound_service import (
    ChannelOutboundService,
)
from app.services.server_channels.channel_stream_relay import (
    ChannelStreamRegistry,
    ChannelStreamRelay,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeChannel:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.channel_type = "google_chat"
        self.enabled = True


class _FakeBinding:
    """Only what the handler reads: the id of the notice standing in the thread."""

    def __init__(self, status_message_id: str | None = "notice-1") -> None:
        self.id = uuid.uuid4()
        self.status_message_id = status_message_id


class _FakeDB:
    def get(self, model: Any, obj_id: Any) -> Any:  # pragma: no cover - unused
        return None


@contextmanager
def _fake_session():
    yield _FakeDB()


class _Thread:
    """Everything the handler wrote into the thread, however it wrote it.

    Both outbound verbs are captured because the handler picks between them by
    length (``_deliver`` chunks, the notice verb truncates) — which is an
    implementation detail these tests must not depend on.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []

    def install(self, stack) -> None:
        async def _set_binding_status(*, text: str, **_kw: Any) -> bool:
            self.writes.append(text)
            return True

        async def _deliver(*, text: str, **_kw: Any) -> None:
            self.writes.append(text)

        stack.enter_context(
            mock.patch.object(
                ChannelOutboundService,
                "set_binding_status",
                side_effect=_set_binding_status,
            )
        )
        stack.enter_context(
            mock.patch.object(
                ChannelOutboundService, "_deliver", side_effect=_deliver
            )
        )


async def _quiesce(relay: ChannelStreamRelay) -> None:
    """Stop the flusher and wait it out, so no task outlives the test."""
    relay.stop()
    task = relay._task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _relay(session_id: uuid.UUID) -> ChannelStreamRelay:
    return ChannelStreamRelay(
        session_id=session_id,
        binding_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        get_fresh_db_session=_fake_session,
    )


#: The module whose ``relay_failed`` branch Guard 2 pins.
_OUTBOUND_LOGGER = "app.services.server_channels.channel_outbound_service"


@contextmanager
def _handler_warnings(
    logger_name: str = _OUTBOUND_LOGGER,
) -> Iterator[list[logging.LogRecord]]:
    """Collect a module's warnings — with a handler, deliberately not ``caplog``.

    ``caplog`` cannot answer this question in this suite, and the way it fails
    is the reason it is not used:

    * it installs its handler on the **root** logger, so it sees only what
      propagates there, and
    * nothing propagates, because the session-scoped autouse ``setup_db``
      fixture runs Alembic and ``alembic.config.Config`` calls
      ``logging.config.fileConfig`` with its default
      ``disable_existing_loggers=True``. Every logger that existed by then —
      this module's included, since collection imports it — comes back
      ``disabled = True`` for the rest of the session.

    So the logger is force-enabled here and a handler is attached straight to
    it. See ``backend/tests/README.md`` ("caplog assertions are vacuous for the
    rest of the session") and ``_swallowed_failures`` in
    ``tests/api/routing/routing_persist_session_ownership_test.py``, the same
    pattern one level down.

    Whole records are kept, not formatted text, so a caller can assert on the
    record's own fields rather than on a rendered string.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger(logger_name)
    handler = _Collector(level=logging.WARNING)
    was_disabled, previous_level = target.disabled, target.level
    target.addHandler(handler)
    target.disabled = False
    target.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.disabled = was_disabled
        target.setLevel(previous_level)


async def _interrupt(session_id: uuid.UUID, binding: _FakeBinding) -> list[str]:
    """Fire ``STREAM_INTERRUPTED`` for ``session_id`` and report what was sent."""
    from contextlib import ExitStack

    thread = _Thread()
    with ExitStack() as stack:
        thread.install(stack)
        stack.enter_context(
            mock.patch("app.core.db.create_session", _fake_session)
        )
        stack.enter_context(
            mock.patch.object(
                ChannelOutboundService,
                "_resolve_channel_session",
                return_value=(binding, _FakeChannel()),
            )
        )
        await ChannelOutboundService.handle_stream_interrupted(
            {"meta": {"session_id": str(session_id)}}
        )
    return thread.writes


# ---------------------------------------------------------------------------
# Guard 1 — a turn stopped before the agent said anything
# ---------------------------------------------------------------------------

def test_interrupting_a_turn_that_said_nothing_settles_the_stopped_marker() -> None:
    """The ``/stop``-before-the-first-token shape, and the notice must not stick.

    A relay is registered at stream start whether or not the agent ever emits
    assistant text, so this turn has a live, non-spent relay holding an empty
    buffer. That is the *normal* result of stopping a turn early — and it is
    the one shape in which the thread had, until this test, no acknowledgement
    of the stop at all: the spinner stayed up and the next turn's notice
    rewrote it minutes later.

    Asserted on what reaches the thread, deliberately, and not on which branch
    produced it: "nothing was streamed" and "the relay could not tell us what
    it streamed" are two different facts about the same empty answer, and the
    handler has been rebuilt around that distinction twice.
    """

    async def run() -> None:
        session_id = uuid.uuid4()
        relay = _relay(session_id)  # attached, fed nothing, never stopped
        ChannelStreamRegistry.put(session_id, relay)
        try:
            writes = await _interrupt(session_id, _FakeBinding())
        finally:
            ChannelStreamRegistry.remove(session_id)

        assert writes, (
            "an interrupted turn with an empty relay wrote nothing to the "
            "thread — the notice is stranded on the spinner"
        )
        assert any("Stopped" in text for text in writes), writes

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Guard 2 — a relay that WAS narrating and cannot hand its tail over
# ---------------------------------------------------------------------------

def test_a_relay_that_fails_its_tail_read_is_met_with_silence() -> None:
    """The mirror case, and the reason the empty case needs its own fact.

    ``take_tail`` answering ``None`` means "something in me broke", not "there
    was nothing" — and a relay that broke may have a confirmed draft standing
    in the notice holding the only copy of a partial answer (the relay's own
    accumulated text is what settles the reply, not the stored message). A
    stopped marker written here replaces that answer with two words,
    permanently, since nothing else delivers it.

    Losing the acknowledgement is the right way round: the thread ends up
    exactly where it would have been before this subscriber existed, and the
    next turn patches the notice. A "simplification" that folds the failure
    flag back into "is there a relay at all" reintroduces the overwrite —
    which is why this assertion is a peer of the one above and not a branch
    detail of it.

    **The silence is pinned by its reason, not only by its shape.**
    ``writes == []`` on its own is satisfied by *any* path that writes nothing,
    the handler blowing up in its outer ``except`` after some future signature
    change included — and that path is a bug wearing this test's passing
    result. So the ``relay_failed`` warning is asserted too: the assertion
    reads "deliberately silent", which is the fact, rather than "wrote
    nothing", which is the symptom. Mutation-proved both ways — the write
    assertion fails when the guard is removed, the log assertion fails when the
    warning is removed or its wording changed.

    The log is read through :func:`_handler_warnings` and **not** through
    ``caplog``, which cannot see it in this suite; that helper says why. The
    negative assertion underneath it is safe for the same reason it is normally
    not: the positive one directly above proves the collector is receiving
    records, so an empty list cannot pass both.
    """

    async def run() -> None:
        session_id = uuid.uuid4()
        relay = _relay(session_id)
        relay.feed("Here is the first half of a real answer.")
        relay._delivered_any = True  # a draft the reader can already see
        # The turn is over — ``on_error``/``on_complete`` stop the relay before
        # the bus handler runs — so the flusher is quiesced here too, and the
        # only thing left to answer the handler is ``take_tail``.
        await _quiesce(relay)
        ChannelStreamRegistry.put(session_id, relay)
        try:
            # ``take_tail``'s own documented failure answer: ``None``.
            with mock.patch.object(
                relay, "_draft", side_effect=RuntimeError("boom")
            ), _handler_warnings() as logged:
                writes = await _interrupt(session_id, _FakeBinding())
        finally:
            ChannelStreamRegistry.remove(session_id)

        assert writes == [], (
            "the stopped marker was written over a standing draft — the "
            "reader's partial answer is gone and nothing else delivers it"
        )

        messages = [record.getMessage() for record in logged]
        assert any(
            "could not hand over a tail" in message
            and str(session_id) in message
            for message in messages
        ), (
            "nothing said the handler stood down on purpose for this session — "
            f"the silence is unexplained and may not be deliberate: {messages}"
        )
        assert not any(
            "handle_stream_interrupted failed" in message for message in messages
        ), (
            "the handler crashed into its outer except; it wrote nothing "
            f"because it broke, not because it decided to: {messages}"
        )

    asyncio.run(run())
