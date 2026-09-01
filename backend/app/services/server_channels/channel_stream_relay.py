"""Live streaming of an agent's answer into a channel thread's status notice.

Without this module a Google Chat turn is silent while the agent works: the
status notice says "💬 Working on your message…" until the stream completes and
then the whole reply lands at once. With it, the notice becomes a **rolling
draft** — the same message, rewritten in place every few seconds with the
assistant text accumulated so far — and the final delivery patches only the
remaining tail into it. The reader watches one message grow, the way the web
client does, instead of watching a spinner.

Three pieces:

* :class:`ChannelStreamRelay` — one per streaming turn. Accumulates raw
  markdown fed to it by the stream, debounces, rewrites the draft, and
  **seals** it (leaves it standing and opens a fresh one below) when it grows
  past what one Chat message can hold.
* :class:`ChannelStreamRegistry` — where the outbound event handlers find the
  relay for a session once the stream is over, so the final delivery can send
  the *tail* rather than the whole answer again.
* :func:`maybe_attach_channel_relay` — the one-call seam the UI streaming path
  uses. It decides whether this session gets a relay at all and returns either
  the caller's own handler untouched or a composite that feeds both.

**Everything here is best-effort and nothing here may raise into the stream.**
The relay is a passenger on somebody else's turn: the stream, the Socket.IO
emission, and the event bus all keep working exactly as they did if every
single thing in this module fails. That is why the flusher runs as its own
task, why each method is wrapped, and why :func:`maybe_attach_channel_relay`
answers "no relay" for every failure it can possibly have.

**No ORM instance is held across a flush.** The relay keeps plain ids
(``session_id``, ``binding_id``, ``channel_id``) and re-fetches the rows in a
fresh session on every flush. This is not caution for its own sake:
``ChannelOutboundService`` commits inside ``set_binding_status``, which expires
every instance in the session, so a held row's next attribute read is a lazy
reload that can raise ``ObjectDeletedError`` on a concurrently deleted binding.
That module documents the hazard at length (§11a); this one stays out of its
way by never holding anything to begin with.

**Relay state is in-memory, and legitimately so.** The relay, the stream it
tees off, and the bus handlers that consume it all live in one process and one
task lifetime. If the process dies the stream dies with it, so there is no
later completion that could arrive needing state that no longer exists — the
state's lifetime is designed to equal the lifetime of the thing it describes.
A draft stranded by a crash is healed by the next turn's "working on your
message…" patch of the same ``status_message_id``.

The consumer contract
---------------------

Three rules bind the outbound handlers that consume a relay. They are not
style; each one is the difference between "the answer arrives" and "the answer
is silently lost", and each has a failure mode that no test of the consumer
alone would catch.

1. **``take_tail`` answers ``None`` for "ask me nothing, I failed".** ``None``
   means *relay-absent*: take the full-text path — this turn's stored
   ``SessionMessage``, resolved by the id the stream event carries — the same
   as if there were no relay at all. It must never be routed to
   ``clear_binding_status`` — the agent's reply exists in ``SessionMessage``
   and deleting the notice would throw it away. ``("", False)`` — the genuine
   "this stream produced nothing" — also belongs on the full-text path, which
   degrades to ``clear_binding_status`` by itself when there really is no
   text. Only ``("", True)`` ("everything is already on screen") is a no-op.
2. **Only ``on_complete``/``on_error`` call :meth:`ChannelStreamRelay.stop`.**
   They fire exactly once per turn. ``STREAM_COMPLETED`` fires once per *LLM
   batch*, so a completion handler that stopped the relay would stream batch 1
   live and leave batches 2+ silent. See :meth:`ChannelStreamRelay.stop`.
3. **A :attr:`~ChannelStreamRelay.spent` relay is treated as absent.** The
   registry is keyed by session and a session outlives a turn, so the entry a
   consumer finds is not necessarily its own turn's. ``spent`` is the
   discriminator; :class:`ChannelStreamRegistry` documents what it rules out
   and why nothing weaker (a timestamp, ``stopped`` on its own) can.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

from sqlmodel import Session as DBSession

from app.core.config import settings
from app.models import ChannelThreadBinding, ServerChannel
from app.services.server_channels.adapters.chat_text_chunking import (
    find_seal_boundary,
)
from app.services.server_channels.adapters.google_chat_format import (
    FENCE_RE,
    markdown_to_chat,
)
from app.services.server_channels.adapters.registry import get_adapter
from app.services.server_channels.channel_outbound_service import (
    CHANNEL_INTEGRATION_PREFIX,
    ChannelOutboundService,
)
from app.services.server_channels.channel_turn_delivery_service import (
    ChannelTurnDeliveryLedger,
    delivered_prefix_key,
)
# The finalize path's own tag patterns, imported rather than re-declared. The
# relay sends assistant text *before* finalize strips these, so without them
# the reader would watch ``<cinna_attach>/app/workspace/report.pdf</...>``
# scroll past — and, since the settled reply is the relay's own accumulated
# text (plan §1), keep it. Two consumers of one model, like the fence and
# table patterns this module already shares with ``chat_text_chunking``; a
# third copy is how they drift. A rename on the other side breaks the import
# loudly, which is the point.
from app.services.sessions.message_service import (
    _ATTACH_TAG_RE,
    _WEBAPP_ACTION_TAG_RE,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelStreamRelay]"

#: Fallback for a transport that declares no per-message cap. Google Chat's is
#: 4096 (``GoogleChatAdapter._MAX_MESSAGE_CHARS``) and is read from the
#: adapter's declared ``capabilities.max_message_chars`` rather than copied —
#: this is only what an adapter that says nothing gets.
_DEFAULT_MESSAGE_LIMIT = 4096

#: Headroom kept between a sealed slice and the transport's hard cap. A sealed
#: message is *final* — the relay advances past it and never sends that text
#: again — so it must fit for real, not nearly. ``update_message`` truncates
#: rather than chunks, and a truncated seal is text the reader never gets.
_SEAL_MARGIN_CHARS = 96

#: How many times a seal window may be halved looking for a boundary whose
#: translated slice fits. Bounded because each attempt translates the slice.
_MAX_WINDOW_ATTEMPTS = 6

#: How many newlines a *forced* cut may walk back past looking for a slice that
#: fits. Deliberately larger than :data:`_MAX_WINDOW_ATTEMPTS`, which it used to
#: share: halving covers the whole window in six steps, but this walk moves one
#: line at a time, and on the table- or list-heavy text that produces a forced
#: cut in the first place six lines is barely any distance at all.
_MAX_FORCED_WALKBACK = 40

#: How many times :meth:`ChannelStreamRelay._clamp_draft` may shrink an over-long
#: draft. Each step keeps three quarters, so the cap is reached long after any
#: realistic expansion.
_MAX_CLAMP_ATTEMPTS = 12

#: How many translated lengths one flush may memoise. The cache is keyed by
#: the slice itself, so each entry is a copy of part of the draft; the cap is
#: comfortably above what a flush asks for (a seal search plus a walkback) and
#: far below what would make it a memory story.
_MAX_RENDER_CACHE_ENTRIES = 64

#: How long the flusher will sit idle before retiring. Not a timeout on the
#: turn — it is the backstop for a stream that was **cancelled**, which is the
#: one ending that never reaches ``stop()``. See :meth:`ChannelStreamRelay._run`.
_IDLE_EXIT_SECONDS = 300.0

#: How many messages one flush may seal off. A single flush normally seals
#: zero or one; the cap stops a pathological buffer from posting a burst of
#: messages inside one iteration (and spending a whole space's write quota).
_MAX_SEALS_PER_FLUSH = 3


class ChannelStreamRelay:
    """Turns a stream of assistant text into a live-updating channel message.

    One instance per streaming turn, created by
    :func:`maybe_attach_channel_relay` and found again by the outbound event
    handlers through :class:`ChannelStreamRegistry`.

    The buffer is **raw markdown**, exactly as the agent produced it, and every
    decision about where it may be cut is taken in that space
    (:func:`find_seal_boundary`). Translation to Chat markup happens only to
    *measure* — how long would this be once the reader's client has it — and at
    delivery, inside the adapter. Sealing translated text instead would cut in
    a space the next message's remainder does not live in.
    """

    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        binding_id: uuid.UUID,
        channel_id: uuid.UUID,
        get_fresh_db_session: Callable[[], Any],
    ) -> None:
        self.session_id = session_id
        self.binding_id = binding_id
        self.channel_id = channel_id
        self.get_fresh_db_session = get_fresh_db_session

        # Accumulated raw markdown, kept as parts and joined lazily: a turn can
        # feed thousands of small assistant events (OpenCode flushes per
        # newline) and repeated ``self._raw += text`` on a growing string is
        # quadratic. ``_text()`` collapses the parts in place, so the join cost
        # is amortised across reads rather than paid per append.
        self._parts: list[str] = []
        # How much of the buffer has already been sealed into finished
        # messages. Everything from here on is "the current draft".
        self._sealed_offset = 0
        # Non-empty only after a *forced* mid-fence seal: the marker run that
        # re-opens the code block the seal had to close, e.g. ``"```\n"``. It
        # is prepended to the draft (and to the tail) so the remainder's own
        # closing fence still closes something.
        self._fence_prefix = ""
        # Whether the relay has put any of this turn's text on screen. The
        # outbound handler needs it to tell "everything is already delivered"
        # apart from "the stream produced nothing".
        self._delivered_any = False

        # Serialises the flusher against ``take_tail`` — the two both read and
        # advance ``_sealed_offset``, and a tail taken mid-flush would either
        # duplicate or drop a slice.
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._last_flush = 0.0
        # Whether a consumer has taken the tail at least once. With
        # ``_stopped`` and the draft state it makes up :attr:`spent`, the
        # "this relay belongs to a turn that is over and settled" test the
        # outbound handlers discriminate on.
        self._tail_taken = False
        # Whether the flusher task has exited. Deliberately NOT ``_stopped``:
        # the flusher also retires on the idle timeout, on a turn that is very
        # much still alive, and :meth:`feed` must be able to start it again.
        # The registry reads it to evict the relay of a *cancelled* turn, which
        # is the one ending where nothing ever calls :meth:`stop`.
        self._retired = False
        # The turn-delivery ledger row for the draft currently standing, and
        # the part index the next boundary write will take. **Plain values, and
        # that is the same rule the ids above follow**: no ORM instance and no
        # session is ever held here (see the module docstring), so the row is
        # remembered by id and re-fetched by the ledger inside the flush's own
        # session. ``None`` means "no draft row" — the ordinary state until the
        # first seal, since draft rows are only written *after* one — and the
        # next seal inserts instead of updating. Both reset per turn because
        # the relay is per turn.
        self._ledger_row_id: uuid.UUID | None = None
        self._ledger_part_index = 0
        # Where **this batch's** text starts in the buffer. ``_sealed_offset``
        # is a whole-turn offset and the buffer accumulates across batches, but
        # the answer a sealed prefix is checked against at completion is the
        # batch's own ``SessionMessage`` — so a prefix measured from 0 would
        # count the previous batch's text and make the divergence check fire on
        # every multi-batch turn. Advanced at each hand-over, in
        # :meth:`take_tail`, beside the other per-batch resets.
        self._ledger_batch_base = 0
        # One flush's worth of ``markdown_to_chat`` lengths. A seal search
        # measures the same draft several times over (the loop condition, the
        # defer test, the clamp) and translation is a synchronous parse of the
        # whole buffer, run on the event loop under the lock. Cleared at the
        # top of every flush, so it never holds a stale — or a growing —
        # answer.
        self._rendered: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Producer side (called from the stream)
    # ------------------------------------------------------------------

    def feed(self, text: str) -> None:
        """Append assistant markdown to the draft and wake the flusher.

        Synchronous and cheap on purpose: this runs inside the stream's own
        event loop iteration, between two chunks of the agent's answer, and
        must not make it wait on anything. All the slow work — translation,
        seal decisions, HTTP — happens on the flusher task.

        **Text is buffered even after :meth:`stop`, and that is not an
        oversight.** ``_stopped`` governs the *flusher*, never the buffer.
        Under the ownership rule in :meth:`stop` a feed after a stop should
        not happen at all — but if it does, the belief that the turn was over
        was wrong, and dropping the text would lose it invisibly:
        :meth:`take_tail` would answer "nothing new, and I already delivered
        some", which reads to the caller as "it is all on screen". Appending is
        free.

        **The live draft, though, stays stopped.** The flusher is *not*
        restarted after a stop, because by then the completion handler may
        already have taken the tail and delivered it into the notice, releasing
        the id — a flush racing behind that would find no id, post the whole
        draft as a fresh message and duplicate the answer. The one restart that
        does happen is the other one: a flusher that retired on the idle
        timeout (``_retired`` without ``_stopped``) is started again by
        :meth:`_ensure_task` below, because that turn never ended.

        Never raises; the caller is the stream.
        """
        try:
            if not text:
                return
            # Unconditional: the buffer is what ``take_tail`` reads, and it is
            # the only copy of this turn's text the relay has.
            self._parts.append(text)
            if self._stopped:
                return
            self._ensure_task()
            self._wake.set()
        except Exception:  # noqa: BLE001 — a passenger may not derail the trip
            logger.warning("%s Could not accept streamed text", _LOG_PREFIX, exc_info=True)

    def stop(self) -> None:
        """Ask the flusher to finish. Idempotent, never raises.

        **``ChannelRelayEventHandler.on_complete`` / ``on_error`` are the sole
        owners of this call.** They fire exactly once, at the end of the turn,
        on the handler the stream processor already talks to. Nothing else
        stops a relay — in particular the ``STREAM_COMPLETED`` bus handler must
        not, however natural it reads there: that event fires once per *LLM
        batch*, so batch 1 would stream live and every batch after it would go
        silent and land as a lump at the end. (The plan's Phase-3 text says the
        completion handler stops the relay; that instruction is superseded by
        this contract.)

        **Stops the rolling draft, not the relay.** The buffer stays readable
        and :meth:`feed` keeps filling it, so a stop that turns out to be
        premature costs the live narration and nothing else — the text still
        comes out of :meth:`take_tail`.

        Deliberately a *graceful* stop rather than ``task.cancel()``. A cancel
        landing between the adapter call that sealed a slice and the bookkeeping
        that advances past it would re-deliver that slice in the tail, so the
        turn would end with a paragraph shown twice. The loop instead notices
        the flag at its next check and returns; a flush already in flight
        completes, harmlessly — it holds the same lock ``take_tail`` does, so
        the two cannot interleave, and a flush that runs after the tail was
        taken finds an empty draft and does nothing.
        """
        try:
            self._stopped = True
            self._wake.set()
        except Exception:  # noqa: BLE001
            logger.warning("%s Could not stop the flusher", _LOG_PREFIX, exc_info=True)

    @property
    def stopped(self) -> bool:
        """Whether the turn's stream has ended (see :meth:`stop`)."""
        return self._stopped

    @property
    def spent(self) -> bool:
        """Whether this relay is finished business: **treat it as absent.**

        True when the turn is over (:meth:`stop` has run), a consumer has
        already taken a tail, and nothing new has arrived since. It is the
        discriminator :class:`ChannelStreamRegistry` documents — the answer to
        "is the entry I found under this session id mine, or last turn's".

        Each of the three terms carries its own case:

        * ``_stopped`` — a live relay is somebody's turn in progress, and a
          consumer that finds one either belongs to it or is early.
        * ``_tail_taken`` — a stopped relay nobody has consumed is the normal
          shape of the happy path: ``on_complete`` stops the relay, and the
          completion handler runs after it. Without this term the ordinary
          single-batch turn would look stale to its own handler and re-deliver
          the whole answer through the full-text path.
        * no draft content — a multi-batch turn's second batch arrives *after*
          batch 1's tail was taken, and on a long turn after the stop as well.
          Its increment must still be deliverable.
        """
        return self._stopped and self._tail_taken and not self._has_draft_content()

    @property
    def evictable(self) -> bool:
        """Whether the registry may drop this relay under pressure.

        :attr:`spent` is one half — a turn that ended and handed everything
        over. The other is a flusher that :attr:`retired` **without ever being
        stopped**: the cancelled turn (client disconnect, ``/stop``, an
        environment restart mid-stream), whose ``on_complete`` never runs, so
        no consumer is coming for it either.

        ``retired`` on its own would be wrong, and the difference is not
        theoretical: on the happy path ``on_complete`` stops the relay and the
        flusher retires *before* the completion handler has taken its tail, so
        an ordinary finished turn would spend a few moments evictable. Taking
        it there sends that handler down the full-text path with a partly
        delivered draft — and a sealed message or two — already standing, which
        is the duplicate this class exists to avoid.

        What that leaves un-evictable is the stopped turn whose consumer never
        arrives at all. It is bounded rather than unbounded: the entry is keyed
        by session and the next turn on that session replaces it.
        """
        return self.spent or (self._retired and not self._stopped)

    @property
    def retired(self) -> bool:
        """Whether the flusher task has exited (idle timeout, stop, or crash)."""
        return self._retired

    # ------------------------------------------------------------------
    # Consumer side (called from the outbound event handlers)
    # ------------------------------------------------------------------

    async def take_tail(self, *, partial: bool = False) -> tuple[str, bool] | None:
        """Take everything not yet sealed, and advance past it.

        Returns ``(tail_markdown, delivered_anything)``. ``delivered_anything``
        reports whether the *relay* put any text on screen during this turn —
        the caller needs it to tell an already-delivered answer apart from a
        stream that produced nothing at all, which end very differently.

        **``None`` means the relay failed, not that it had nothing.** The three
        answers are three different instructions, and conflating the first two
        loses an answer that exists:

        * ``None`` — something in here broke. Treat the relay as **absent**:
          take the full-text path (this turn's stored ``SessionMessage``,
          resolved by the id the stream event carries). Never
          ``clear_binding_status`` — the agent's reply is in ``SessionMessage``
          whatever happened here, and deleting the notice would be the relay
          throwing away text it merely failed to read.
        * ``("", False)`` — the stream genuinely produced nothing. The
          full-text path is right here too and degrades to
          ``clear_binding_status`` on its own when there is no message text;
          routing it straight to the delete would also delete the answer of a
          turn whose relay this consumer does not actually own (see
          :class:`ChannelStreamRegistry`).
        * ``("", True)`` — everything is already on screen. Nothing to do.

        **It also ends the batch for the turn-delivery ledger**, releasing the
        draft row this relay was holding — see the comment at that line, which
        names the corruption holding it across a batch boundary would cause.

        **Idempotent**: a second call returns ``("", …)``. That is what makes a
        multi-batch turn correct — ``STREAM_COMPLETED`` fires once per LLM
        batch, so each call delivers exactly that batch's increment and a
        duplicate event delivers nothing.

        The tail is stripped of the agent's control tags (:func:`_visible`),
        because the settled reply is this text and not the stored message's.
        A tail with no non-whitespace content left comes back as ``""``: there
        is nothing to show, and the caller's "did the stream produce anything"
        branch should not be tripped by a stray newline.

        **``partial=True`` for a tail taken off a stream that ended
        mid-token.** On a completed batch the buffer ends where the agent
        stopped writing, so a tag in it is whole and :func:`_visible` removes
        it. An **interrupted or failed** stream has no such guarantee: it can
        end in ``"<cinna_attach>/app/workspace/repo"``, which :func:`_visible`
        leaves alone (no closing half to pair) and which then settles into the
        thread as a raw fragment of an internal protocol — the reader watched
        "Here is the file:" and gets that under it. The flag switches to
        :func:`_visible_draft`, which drops from the unfinished opening on.
        It is an **opt-in and not the default** because that function also cuts
        on a bare trailing ``"<"``: an ordinary reply that genuinely ends in
        ``"<c"`` would silently lose those characters, which is the right price
        for a live draft that redraws two seconds later and the wrong one for a
        final answer that does not.
        """
        try:
            async with self._lock:
                strip = _visible_draft if partial else _visible
                tail = strip(self._draft()) if self._has_draft_content() else ""
                if not tail.strip():
                    tail = ""
                self._sealed_offset = len(self._text())
                self._fence_prefix = ""
                self._tail_taken = True
                # **Let go of the ledger's draft row too.** Handing the tail
                # over ends this *batch*, and the completion handler that just
                # asked for it is about to settle that row as the batch's
                # ``final``. A multi-batch turn keeps feeding this same relay
                # afterwards (bus handlers never stop a relay — see
                # :meth:`stop`), so holding the id would make the next batch's
                # seal reach back and rewrite a finished row from ``final``
                # to ``sealed``, corrupting the record of a message that had
                # already been settled. Releasing it here costs nothing: the
                # completion's delivery releases the notice id as well, so the
                # next batch opens a genuinely new message and gets a genuinely
                # new row. The part index moves on with it, so the turn's parts
                # stay in order.
                if self._ledger_row_id is not None:
                    self._ledger_row_id = None
                    self._ledger_part_index += 1
                # The next batch's text starts here. See the field's comment:
                # without this the next batch's sealed prefix would be measured
                # from the start of the *turn* and compared against that
                # batch's own answer.
                self._ledger_batch_base = self._sealed_offset
                return tail, self._delivered_any
        except Exception:  # noqa: BLE001 — the module's claim, made literal
            logger.warning(
                "%s Could not take the tail for session %s; the caller falls "
                "back to delivering the whole answer",
                _LOG_PREFIX,
                self.session_id,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Flusher
    # ------------------------------------------------------------------

    def _ensure_task(self) -> None:
        """Start the single flusher task on first use.

        Started lazily from :meth:`feed` rather than in ``__init__`` so a turn
        that never produces assistant text costs no task at all, and so the
        task is created on the loop that is actually running the stream.
        """
        # ``_retired`` is checked as well as ``done()``: ``_run``'s ``finally``
        # sets it *before* the coroutine returns, so a feed landing in that gap
        # would see a task that is not done yet, decline to start one, and
        # leave the draft frozen for the rest of the turn.
        if self._task is not None and not self._task.done() and not self._retired:
            return
        # Started again means not retired again: the flag says "no flusher is
        # coming back", and the registry evicts on it.
        self._retired = False
        # A bare ``create_task`` rather than the project's
        # ``create_task_with_error_logging``: ``_run`` catches and logs
        # everything itself (it has to — it must survive a failed flush and
        # keep going), so the wrapper would have nothing left to report.
        self._task = asyncio.create_task(
            self._run(), name=f"channel-stream-relay-{self.session_id}"
        )

    async def _run(self) -> None:
        """Debounce loop: wake, wait out the remainder of the interval, flush.

        The first flush is immediate (``_last_flush`` starts at 0), so the
        reader sees the answer begin as soon as the agent does; every later one
        waits only the part of ``CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS`` that
        has not already elapsed. An interval <= 0 disables the wait entirely —
        see the setting's comment.

        The loop's own failure modes are contained here: a flush that raises is
        logged and the loop continues, because the *next* flush usually
        succeeds and giving up would silently freeze the draft for the rest of
        the turn.

        **The idle wait is bounded, and that is what stops a leak.** This task
        is a *sibling* of the streaming task, not a child, so it is not
        cancelled with it — and cancellation is precisely the path on which
        :meth:`stop` is never called: ``CancelledError`` is a
        ``BaseException``, so it slips past the ``except Exception`` in both
        ``SessionStreamProcessor`` and ``process_pending_messages``, and the
        handler's ``on_complete``/``on_error`` never run. An unbounded
        ``wait()`` would park this task forever holding the relay, its whole
        buffer and the session factory — one per interrupted channel turn.
        Retiring on the timeout costs nothing: :meth:`_ensure_task` starts a
        fresh task if text ever arrives again.

        **Every exit sets ``_retired``, and that is what lets the registry
        reap.** A cancelled turn's relay is never stopped, so a registry that
        evicted only *stopped* relays would hold it — and its whole buffer and
        the session factory — for the life of the process, one per cancelled
        channel turn, until the bounded dict was bounded in name only.
        ``_retired`` is deliberately not ``_stopped``: the turn may still be
        alive and :meth:`feed` must be able to start the flusher again.
        """
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=_IDLE_EXIT_SECONDS
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    # Nothing fed for minutes: whatever happened to the stream,
                    # this turn is not still narrating. See the docstring.
                    return
                self._wake.clear()
                if self._stopped:
                    return
                interval = self._interval()
                if interval > 0:
                    remaining = interval - (time.monotonic() - self._last_flush)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    if self._stopped:
                        return
                self._last_flush = time.monotonic()
                try:
                    await self._flush()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — see the docstring
                    logger.warning(
                        "%s Draft update failed for session %s; continuing",
                        _LOG_PREFIX,
                        self.session_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a detached task must not shout
            logger.warning(
                "%s Flusher task for session %s ended unexpectedly",
                _LOG_PREFIX,
                self.session_id,
                exc_info=True,
            )
        finally:
            # Reached by every exit, cancellation included — see the docstring.
            self._retired = True

    @staticmethod
    def _interval() -> float:
        try:
            return float(settings.CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS)
        except Exception:  # noqa: BLE001 — a bad setting is not a lost turn
            return 3.0

    async def _flush(self) -> None:
        """Seal what has to be sealed, then rewrite the draft. Under the lock.

        The session is opened **only when there is something to send** and is
        closed again the moment the flush ends, so a pooled connection is held
        across the adapter's HTTP call but never between flushes. That is the
        same bargain ``handle_stream_completed`` already strikes, and it is
        forced by the same thing: ``set_binding_status`` is one verb that both
        posts and persists the notice id, and splitting it here to shorten the
        hold would mean a second copy of the id bookkeeping — exactly the
        drift this relay exists to avoid. What the relay adds is *frequency*,
        which is what ``CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS`` bounds.

        **What that costs, stated plainly:** one pooled connection is held
        across each HTTP round trip in here — up to
        ``_MAX_SEALS_PER_FLUSH + 1`` of them — once per interval per
        *concurrent streaming channel turn*. The adapter's 429 backoff can
        stretch a round trip well past its usual latency, and Google Chat's
        write quota is per **space**, so several busy threads in one space back
        each other off together. Sizing follows from that: concurrent channel
        turns must stay comfortably under the pool, and lowering the interval
        multiplies the frequency, not the hold.

        The relay's lock is held across those calls too, so ``take_tail`` can
        wait out one flush's worth of adapter retries (429 backoff included)
        before it answers. Accepted rather than fixed: dropping the lock around
        the awaits is what would let a tail be taken between a seal's send and
        the offset advance that follows it, which is the interleaving the lock
        exists to prevent — and the caller waiting is a completion handler that
        has nothing else to do.
        """
        async with self._lock:
            self._rendered.clear()
            if not self._has_draft_content():
                return
            with self._open_db() as db:
                resolved = self._resolve(db)
                if resolved is None:
                    return
                binding, channel = resolved
                limit = _message_limit(channel)
                draft = await self._seal_down(
                    db, channel, binding, self._draft(), limit
                )
                if not self._has_draft_content():
                    return
                # Clamping happens HERE, at the draft patch, and nowhere else:
                # see :meth:`_clamp_draft` on why a sealed slice may never be
                # trimmed to fit.
                visible = self._clamp_draft(_visible_draft(draft), limit)
                if not visible.strip():
                    return
                (
                    patched,
                    draft_message_id,
                ) = await ChannelOutboundService.set_binding_status_ex(
                    db=db, channel=channel, binding=binding, text=visible
                )
                if patched:
                    # Only on a confirmed send. ``_delivered_any`` is the
                    # outbound handler's evidence that the answer is already on
                    # screen; claiming it after a failed patch makes that
                    # handler skip its own delivery and the reader gets nothing.
                    self._delivered_any = True
                    if self._ledger_row_id is None:
                        # A **fresh** draft: either the turn's first, or the
                        # one that opened after a seal let the notice id go.
                        # Both created a new external message, and the ledger's
                        # grain is one row per external message — so both get a
                        # row, and the rolling patches that follow get none.
                        # That is the boundary-only rule, not an exception to
                        # it: what it forbids is per-*flush* persistence, and
                        # this writes once per message however many times that
                        # message is later rewritten. Recording the turn's
                        # first draft as well is what lets a process that dies
                        # mid-turn leave behind a record of the message left
                        # standing in the thread, which is the crash knowledge
                        # this table exists for.
                        #
                        # The row's offsets stay NULL until its content stops
                        # moving — at the seal that settles it, or the
                        # completion that finalizes it.
                        self._ledger_row_id = ChannelTurnDeliveryLedger.record_draft(
                            db,
                            binding_id=self.binding_id,
                            part_index=self._ledger_part_index,
                            external_message_id=draft_message_id,
                        )

    async def _seal_down(
        self,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        draft: str,
        limit: int,
    ) -> str:
        """Seal finished messages off the front of ``draft`` until it fits.

        Returns what is left to be the live draft. Each seal is a
        ``set_binding_status(..., settle=True)`` — the existing verb for "write
        this one last time and let go of the id" — so the sealed message stays
        standing and the *next* patch, having no id to patch, posts a fresh
        draft below it. The relay adds only the decision of where to cut; the
        posting, the id bookkeeping and the failure degradation are all the
        outbound service's, unchanged.

        ``set_binding_status_ex`` rather than ``set_binding_status`` only to
        learn *which* message the seal landed in, for the turn-delivery ledger.
        The first value it returns is the same ``bool`` this loop has always
        gated its advance on, with the same meaning.

        A seal is one of the ledger's three boundaries. The write happens after
        the advance and never before it, is total, and cannot fail the turn:
        losing it costs a row, never a message.
        """
        target = _seal_target()
        for _ in range(_MAX_SEALS_PER_FLUSH):
            # Measured on what would actually be sent, tags removed: the
            # decision to seal has to be taken on the text the reader gets.
            if self._rendered_len(_visible(draft)) <= target:
                return draft
            seal = self._choose_seal(draft, target, limit)
            if seal is None:
                return draft
            sealed_text, cut, next_prefix = seal
            (
                delivered,
                sealed_message_id,
            ) = await ChannelOutboundService.set_binding_status_ex(
                db=db,
                channel=channel,
                binding=binding,
                text=sealed_text,
                settle=True,
            )
            if not delivered:
                # A boundary delivery that did not land. Recorded on the row
                # the seal was attempting, purely so the failure is visible;
                # the row keeps its ``draft`` role because the message it names
                # is still standing and still being rewritten, and the retry
                # below flips it to ``sealed``. Total, like every ledger call.
                ChannelTurnDeliveryLedger.mark_failed(db, self._ledger_row_id)
                # **The advance is gated on a confirmed send, and it has to be.**
                # A seal is the one irreversible thing this relay does: it
                # moves past a slice and never offers it again. Advancing on a
                # send that failed (auth expired, 5xx past the adapter's
                # retries, the space's write quota spent) would leave a
                # 3000-character hole in the middle of the answer, with the
                # tail arriving as if nothing were missing — a silent partial
                # reply, which is strictly worse than the loud total failure
                # the non-streaming path produces. Leaving the offset alone
                # costs nothing: the next flush retries the same seal, and if
                # the turn ends first the slice is still in the tail.
                return draft
            self._delivered_any = True
            # ``cut`` is an offset into the draft, whose first
            # ``len(self._fence_prefix)`` characters are the re-opening fence
            # this relay synthesised — not buffer content — so only the rest of
            # it advances the buffer.
            #
            # **These two lines are ordered, not adjacent.** The advance reads
            # the fence prefix the draft was built WITH; the line under it
            # replaces that prefix with the one the *next* draft opens with.
            # Swapped (or merged into a tidier-looking pair), the advance would
            # subtract the wrong prefix length and the buffer would skip or
            # repeat exactly that many characters of the answer.
            self._sealed_offset += cut - len(self._fence_prefix)
            self._fence_prefix = next_prefix
            # **After the advance, and only on a confirmed send.** The ledger
            # records what is standing in the thread, so it may only be written
            # once the two facts it describes are true: the message went out,
            # and the relay has moved past that text for good. The prefix is
            # measured on the *buffer*, not on what was posted — the fence runs
            # a forced mid-fence seal synthesises are this relay's own, not the
            # agent's, and the completion compares this digest against the
            # finalized canonical answer, which does not contain them. It is
            # measured from this **batch's** base, not the turn's, because that
            # canonical answer is the batch's own ``SessionMessage``.
            visible_char_end, content_sha256 = delivered_prefix_key(
                _visible(self._text()[self._ledger_batch_base : self._sealed_offset])
            )
            ChannelTurnDeliveryLedger.record_seal(
                db,
                binding_id=self.binding_id,
                row_id=self._ledger_row_id,
                part_index=self._ledger_part_index,
                external_message_id=sealed_message_id,
                visible_char_end=visible_char_end,
                content_sha256=content_sha256,
            )
            self._ledger_row_id = None
            self._ledger_part_index += 1
            draft = self._draft()
        return draft

    def _choose_seal(
        self, draft: str, target: int, limit: int
    ) -> tuple[str, int, str] | None:
        """Where to cut ``draft``, or ``None`` for "not here, not yet".

        Returns ``(sealed_message, cut_offset, next_fence_prefix)`` where
        ``draft[cut_offset:]`` is what continues in the fresh draft.

        Three outcomes, in the order they are tried:

        1. **Seal at a good boundary.** :func:`find_seal_boundary` is asked for
           the last paragraph break (else line break) outside a code fence
           inside a window of at most ``target`` raw characters. Raw characters
           are not translated characters, so the slice it proposes is measured
           for real; if it comes out over the transport's cap — **or if there
           is no boundary in that window at all** — the window is **halved**
           and the question asked again, which walks the cut earlier into text
           that has already been measured too long. The halving terminates on
           its own — ``window // 2`` reaches 0 and the loop breaks — and is
           capped besides.
        2. **Defer.** No boundary clears the bar, and the draft is still small
           enough to keep growing. Answer ``None``: a boundary usually arrives
           with the next few sentences. Deferring is always safe — nothing is
           lost, the draft is simply longer, and the transport truncates an
           over-long *draft* cosmetically (the tail is delivered in full at the
           end regardless).
        3. **Force.** No boundary, and the draft has grown to where the reader
           is about to start losing the end of it to truncation. Cut at the
           last newline in the window even if that lands inside a code fence,
           closing the block on the way out and re-opening it in the next
           message (see :meth:`_forced_seal`).

        A seal whose *translated* slice does not fit is never taken, in any of
        the three. A sealed message is final — the buffer advances past it — so
        an over-long one is text the reader never receives, whereas a deferred
        seal costs nothing but a longer draft.

        **Known degradation, deliberately not fixed.** An *unclosed* tag
        opening stranded near the head of the draft — an agent writing about
        the ``<webapp_action>`` protocol in prose rather than emitting one —
        sits in every slice this method can propose, so :func:`_has_open_tag`
        rejects them all and :meth:`_forced_seal` walks back into the same
        opening and answers ``None``. The turn then never seals again: the
        draft grows, :meth:`_clamp_draft` freezes what the notice shows near
        the cap, and everything arrives at the end through ``_deliver``, which
        chunks. Live narration stalls; no text is lost. Fixing it means
        deciding a tag is not a tag after all, which is a worse trade than a
        rare stalled draft on a turn that still answers in full.
        """
        # **Seeded from the draft, not from the target.** The seal *trigger* is
        # measured on translated text while the window is in raw characters,
        # and translation expands: a wide pipe table goes from 1862 raw
        # characters to 8445 translated ones. Such a draft is far past the
        # target and still shorter than a window of ``target`` raw characters —
        # and ``find_seal_boundary`` answers ``None`` for "the text still fits
        # in the window", the same ``None`` it uses for "no boundary here". A
        # window seeded at the target therefore never saw the boundaries of any
        # content that expands, and every such draft fell through to
        # ``_forced_seal``, which cuts blind and at best keeps the first 40
        # lines: the reader watched a table stop mid-row at two fifths of what
        # the agent wrote.
        # **Plus what stripping will remove.** The window is a budget in *raw*
        # characters and the trigger is measured on *visible* ones, so a
        # 1900-character ``<webapp_action>`` body sitting in the draft costs
        # the search 1900 characters of reach for text the reader never sees:
        # every boundary in the answer's actual prose falls outside a window of
        # ``target``, and the search either seals a scrap of the text above the
        # tag or (with the floor below in place) never seals at all for the
        # rest of the turn. Widening by exactly the stripped span puts the
        # visible budget back at ``target`` where the trigger measures it.
        # ``len(draft) - 1``, not ``len(draft)``: ``find_seal_boundary`` answers
        # ``None`` whenever the text *fits* in the window, so a window of
        # exactly the draft's length is a guaranteed miss that costs one of the
        # six attempts — and, since the search then really starts at half the
        # draft, one halving of cut quality too.
        stripped = len(draft) - len(_visible(draft))
        window = min(len(draft) - 1, target + stripped)
        for _ in range(_MAX_WINDOW_ATTEMPTS):
            if window <= 0:
                break
            offset = find_seal_boundary(draft, window)
            if offset is None:
                # Both meanings of ``None`` are answered the same way: halve
                # and ask again. "Text shorter than the window" becomes a real
                # search on the next turn of the loop, and "no boundary in this
                # window" walks the cut earlier, which is where the boundaries
                # that fit live anyway.
                window //= 2
                continue
            if offset <= len(self._fence_prefix):
                # The whole slice would be the fence prefix this relay
                # synthesised: there is no content to seal, and a smaller
                # window can only propose less.
                break
            sealed = _visible(draft[:offset]).rstrip("\n")
            if not sealed.strip():
                # Everything up to the boundary was a control tag: there is no
                # message in it, and a smaller window can only propose less.
                break
            if _has_open_tag(sealed):
                # The cut landed inside a tag body (``<webapp_action>`` JSON is
                # multi-line). Sealing is final, so this is the one place that
                # would strand both halves — see :func:`_has_open_tag`. Halve
                # and look again above the tag.
                window //= 2
                continue
            if self._rendered_len(sealed) < target // 2:
                # **A floor on what the reader actually gets.**
                # ``find_seal_boundary`` applies one too, but in *raw*
                # characters — which was a faithful proxy for the visible slice
                # right up until tags started being stripped out of it. It is
                # not one now: a draft of ``"Sure.\n\n"`` followed by a
                # 1900-character ``<webapp_action>`` body has a perfectly good
                # paragraph boundary below the tag that clears the raw floor by
                # a mile and leaves **five** visible characters to seal — a
                # five-character message, posted final and left standing, while
                # the buffer advances 1948 characters past it. That is the
                # plan's "never 1 sentence = 1 message" contract, broken by the
                # one path that cannot be taken back.
                #
                # Measured against ``target`` and never against ``window``: a
                # window-derived floor shrinks with every halving, so the
                # search would lower its own quality bar until it accepted the
                # scrap it had just rejected. ``target`` is also the quantity
                # the seal *trigger* is measured in
                # (``_rendered_len(_visible(draft)) > target``), which makes
                # "big enough to cut" and "big enough to stand alone" one
                # question asked twice rather than two that can disagree.
                window //= 2
                continue
            if self._rendered_len(sealed) <= limit - _SEAL_MARGIN_CHARS:
                # The boundary is outside every fence *as far as this scan can
                # tell* — an assumption now, not a construction, and named as
                # one because the last reader took it for a proof.
                # ``find_seal_boundary`` tracks fences in raw space while what
                # is sent is ``_visible(...)``, so a stripped tag body holding
                # an odd number of bare fence-marker lines would leave the two
                # disagreeing and this draft opening unprefixed inside a block.
                # Unreachable without a malformed tag body — valid
                # ``<webapp_action>`` JSON and a one-line ``<cinna_attach>``
                # path cannot contain a bare fence line — so it is documented
                # rather than guarded.
                return sealed, offset, ""
            window //= 2

        if self._rendered_len(_visible(draft)) < limit - _SEAL_MARGIN_CHARS:
            return None  # defer — see outcome 2
        return self._forced_seal(draft, target, limit)

    def _forced_seal(
        self, draft: str, window: int, limit: int
    ) -> tuple[str, int, str] | None:
        """Cut at the last usable newline, even inside a fence.

        Reached only when the draft has no acceptable boundary *and* has grown
        to the point where continuing to defer starts costing the reader the
        end of the message. The cut walks backwards newline by newline until
        the translated slice fits, and still answers ``None`` rather than
        sealing something oversized — or, since the walkback only shrinks the
        slice, anything below the same visible floor :meth:`_choose_seal`
        applies. A forced cut is still a *final* message, so "too short to be
        worth a message of its own" disqualifies it here exactly as it does
        there; the difference is only that here the answer is to give up rather
        than to look again.

        **The re-opened fence must repeat the marker the block opened with.**
        ``markdown_to_chat`` closes a fenced block only on a fence whose first
        marker character matches the opening one (``_take_fenced_block``), so a
        ``~~~`` block cut in half and re-opened with ```` ``` ```` would leave
        the remainder's own ``~~~`` unable to close it — the rest of the answer
        would render as one runaway code block. So the open marker run is
        recovered from the sealed slice and used for both the closing fence and
        the next draft's prefix.
        """
        stop = min(len(draft), window)
        for _ in range(_MAX_FORCED_WALKBACK):
            cut = draft.rfind("\n", 0, stop)
            if cut <= len(self._fence_prefix):
                return None
            head = _visible(draft[:cut])
            if not head.strip():
                return None
            if _has_open_tag(head):
                # Inside a tag body: walk back past it rather than seal both
                # halves into two messages nothing can strip (see
                # :func:`_has_open_tag`).
                stop = cut
                continue
            if self._rendered_len(head) < window // 2:
                # The same visible floor :meth:`_choose_seal` applies, and here
                # it is also the end of the search: the walkback only ever
                # moves the cut *earlier*, so every candidate after this one is
                # shorter still. Without it a draft that opens with a couple of
                # words and then a long tag body — ``"Sure.\n"`` plus a
                # ``<webapp_action>``, which reaches this method on the variant
                # with no paragraph break in it — seals those two words as a
                # final, standing message and advances the buffer past two
                # thousand characters. Deferring instead costs nothing: the
                # draft keeps growing and the tail delivers all of it.
                return None
            # Skip the blank line that separated the halves, and make sure
            # something is actually left to continue with.
            end = cut
            while end < len(draft) and draft[end] == "\n":
                end += 1
            if end >= len(draft):
                stop = cut
                continue
            marker = _open_fence_marker(head)
            sealed = head.rstrip("\n")
            next_prefix = ""
            if marker is not None:
                sealed = f"{sealed}\n{marker}"
                next_prefix = f"{marker}\n"
            if self._rendered_len(sealed) <= limit - _SEAL_MARGIN_CHARS:
                return sealed, end, next_prefix
            stop = cut
        return None

    # ------------------------------------------------------------------
    # Buffer + row access
    # ------------------------------------------------------------------

    def _rendered_len(self, text: str) -> int:
        """How long ``text`` renders in the reader's client, memoised per flush.

        ``markdown_to_chat`` parses the whole string, and one flush asks about
        the same draft several times over — the seal loop's trigger, the defer
        test, the clamp — on top of one translation per window attempt. All of
        it runs synchronously on the event loop, holding the relay's lock, on a
        buffer that grows all turn. The cache is cleared at the top of every
        flush, so it can neither answer for a stale draft nor outlive one.
        """
        cached = self._rendered.get(text)
        if cached is None:
            cached = len(markdown_to_chat(text))
            if len(self._rendered) < _MAX_RENDER_CACHE_ENTRIES:
                # Keys are the slices themselves, so an uncapped cache holds a
                # copy of the draft per candidate — a forced walkback alone
                # offers forty. Past the cap the memo simply stops helping,
                # which is where it started.
                self._rendered[text] = cached
        return cached

    def _clamp_draft(self, draft: str, limit: int) -> str:
        """Keep a *draft* to a single message. Cosmetic by construction.

        **Never call this on a sealed slice, and the name says so.** What makes
        it cosmetic is entirely that its input is the live draft: whatever it
        drops is rewritten in full on the next flush, and the turn's tail is
        delivered through ``_deliver``, which chunks properly. A sealed slice
        has neither of those safety nets — the relay advances past it and never
        offers that text again — so trimming one to fit would delete part of
        the answer permanently and silently, which is precisely the class of
        loss the rest of this module is built to prevent. **A seal is whole or
        deferred**: :meth:`_choose_seal` answers ``None`` when no cut fits, and
        the draft simply keeps growing until one does. The two paths are kept
        apart by construction — nothing under :meth:`_seal_down` calls this,
        and a test drives a seal with this method replaced by a raise to keep
        it that way.

        It exists because ``set_binding_status`` with no live notice id — the
        state the relay is in immediately after every seal — falls through to
        ``send_message``, which **chunks** an over-long body and hands back the
        id of the *last* chunk only. The next flush would then patch that last
        chunk with the whole draft and leave the earlier chunk's text standing
        above it, duplicated. One message per draft makes the id mean what the
        relay assumes it means. Reachable only when the draft outgrew the cap
        without ever offering a seal boundary — no newlines at all, or a
        translation that expanded past the raw window — but that is exactly the
        shape (a long unbroken table, a wall of prose) that a reader would
        notice.

        Shrinks in the *untranslated* space, measuring in the translated one,
        since only the latter is what the cap applies to. Untranslated is no
        longer the same thing as raw: the draft handed in here has been through
        :func:`_visible_draft`, so ``cut`` indexes that stripped string and is
        emphatically **not** an offset into the buffer. Nothing may carry it
        back to ``_sealed_offset``.
        """
        if self._rendered_len(draft) <= limit:
            return draft
        cut = limit
        for _ in range(_MAX_CLAMP_ATTEMPTS):
            if cut <= 0:
                break
            candidate = draft[:cut]
            if self._rendered_len(candidate) <= limit:
                return candidate
            cut = cut * 3 // 4
        # Nothing shrank far enough to fit, which needs a translation that
        # expands by more than 10x. Skipping this patch (the caller drops an
        # empty draft) beats posting something that chunks.
        return ""

    def _text(self) -> str:
        """The whole accumulated buffer, collapsing the parts in place."""
        if len(self._parts) > 1:
            self._parts = ["".join(self._parts)]
        return self._parts[0] if self._parts else ""

    def _draft(self) -> str:
        """The live draft: everything unsealed, with any re-opening fence."""
        return self._fence_prefix + self._text()[self._sealed_offset :]

    def _has_draft_content(self) -> bool:
        """Whether the draft holds anything a reader would see.

        Measured on the **buffer**, deliberately, not on :meth:`_draft`. After
        a forced mid-fence seal the draft opens with the fence run this relay
        synthesised, and ``"```\n".strip()`` is perfectly truthy — so the
        obvious check would let a draft that is *nothing but the re-opening
        fence* through and patch the notice with a bare, empty code block.
        """
        return bool(self._text()[self._sealed_offset :].strip())

    def _open_db(self) -> Any:
        return self.get_fresh_db_session()

    def _resolve(
        self, db: DBSession
    ) -> tuple[ChannelThreadBinding, ServerChannel] | None:
        """Re-fetch the binding and channel for this flush. Never raises.

        Fresh rows every time, by design — see the module docstring. ``None``
        means "nothing to narrate to": the binding or channel is gone, or the
        channel was disabled mid-turn, and the relay simply goes quiet. The
        answer itself is unaffected; the outbound handlers make the same checks
        again when the turn ends.
        """
        try:
            binding = db.get(ChannelThreadBinding, self.binding_id)
            if binding is None:
                return None
            channel = db.get(ServerChannel, self.channel_id)
            if channel is None or not channel.enabled:
                return None
            return binding, channel
        except Exception:  # noqa: BLE001 — a draft is never worth a failure
            logger.warning(
                "%s Could not resolve the binding for session %s",
                _LOG_PREFIX,
                self.session_id,
                exc_info=True,
            )
            return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ChannelStreamRegistry:
    """Where the outbound event handlers find the relay for a session.

    A module-level dict keyed by session id, in the same shape (and with the
    same bounded eviction) as ``stream_processor._session_locks``. Process
    memory is the right home: see the module docstring on why relay state has
    exactly the lifetime of the stream it describes.

    **Entries are put and removed at the start of a turn, by
    :func:`maybe_attach_channel_relay`, and never popped by a consumer.**
    ``STREAM_COMPLETED`` fires once per LLM batch, so a consumer that popped
    would leave the second batch of a multi-batch turn unable to find the relay
    and re-delivering the whole answer instead of its increment.

    **What a consumer may NOT assume: that the entry it finds is its own
    turn's.** The registry is keyed by session, and a session outlives a turn.
    Two ways the key can be answered by the wrong relay:

    * ``SessionStreamProcessor`` is also constructed by the App MCP
      (``app/mcp/message_streaming.py``) and A2A (``a2a_request_handler.py``)
      paths, neither of which calls :func:`maybe_attach_channel_relay`. A
      channel-bound session streamed through one of those leaves the previous
      turn's relay standing, so the completion handler finds a relay for a turn
      that ended long ago, takes its empty tail with ``delivered_any=True``,
      concludes "it is all on screen" and delivers **nothing**.
    * Bus handlers are fire-and-forget tasks, so turn N's ``STREAM_COMPLETED``
      handler and turn N+1's ``maybe_attach`` are unordered. A handler that
      arrives that late reads the *fresh* relay and is told "nothing new,
      nothing delivered". Unlikely — the handler has many scheduling
      opportunities while the session lock is still held.

    **The discriminator is :attr:`ChannelStreamRelay.spent`, and a spent relay
    is treated as absent** — the consumer takes the full-text path, which is
    always safe (a duplicated reply at worst, never a lost one). Spent is
    "stopped, already consumed once, and nothing new since": exactly the state
    a finished turn's relay is left in, and a state a live turn's relay cannot
    be in. It closes the first hazard outright. It does not close the second,
    which is why ``("", False)`` must also route to the full-text path rather
    than to ``clear_binding_status`` (see :meth:`ChannelStreamRelay.take_tail`)
    — with that, a late handler reading the next turn's relay delivers its own
    turn's stored answer instead of deleting the notice.

    **What was tried and does not work.** A creation timestamp reads like a
    guard and is not one: no ``STREAM_COMPLETED`` payload carries a comparable
    monotonic mark, so there is nothing to compare it against. "The relay is
    not stopped" is not one either, however well it matches the emission order
    (``STREAM_COMPLETED`` is emitted inside ``stream_message_with_events``,
    strictly before the processor's ``on_complete`` — which is what stops the
    relay): backend event handlers are *tasks*, dispatched with
    ``create_task``, so the handler routinely runs after the ``on_complete``
    that its own event preceded. A stopped relay is the ordinary shape of the
    happy path, not evidence of staleness.
    """

    _relays: dict[str, ChannelStreamRelay] = {}
    _MAX_ENTRIES = 500

    @classmethod
    def put(cls, session_id: uuid.UUID | str, relay: ChannelStreamRelay) -> None:
        key = str(session_id)
        if key not in cls._relays and len(cls._relays) >= cls._MAX_ENTRIES:
            # ``evictable``, not ``stopped``: a cancelled turn's relay is never
            # stopped (nothing calls ``stop`` on that path) and would otherwise
            # sit here with its whole buffer for the life of the process, one
            # per cancelled channel turn, leaving the cap bounding nothing. And
            # not ``retired`` either — a relay whose consumer is still to come
            # stays, however finished its flusher is. See :attr:`evictable`.
            for stale in [k for k, r in cls._relays.items() if r.evictable]:
                del cls._relays[stale]
        cls._relays[key] = relay

    @classmethod
    def get(cls, session_id: uuid.UUID | str) -> ChannelStreamRelay | None:
        return cls._relays.get(str(session_id))

    @classmethod
    def remove(cls, session_id: uuid.UUID | str) -> None:
        cls._relays.pop(str(session_id), None)


# ---------------------------------------------------------------------------
# Relay-side stream handler
# ---------------------------------------------------------------------------

class ChannelRelayEventHandler:
    """``StreamEventHandler`` that feeds the draft. Never raises.

    Only ``assistant`` events with text reach the buffer. ``thinking`` is a
    **distinct event type**, not a flavour of assistant output, and it carries
    the model's reasoning — it must never reach a channel message. Everything
    else (``tool``, ``attachment``, ``error``, ``done``, …) is narration this
    version deliberately does not send.

    **This handler is the sole owner of :meth:`ChannelStreamRelay.stop`.**
    ``on_complete`` and ``on_error`` are called by ``SessionStreamProcessor``
    once, at the end of the turn, after every batch — which is the only place
    that knows a turn is over. The bus handlers must not stop a relay; see
    :meth:`ChannelStreamRelay.stop`.
    """

    def __init__(self, relay: ChannelStreamRelay) -> None:
        self.relay = relay

    async def on_stream_starting(self, pending_count: int) -> None:
        # The pipeline's own "working on your message…" notice already covers
        # the moment before the first token.
        return None

    async def on_event(self, event: dict[str, Any]) -> None:
        try:
            if event.get("type") != "assistant":
                return
            content = event.get("content")
            if not isinstance(content, str) or not content:
                return
            self.relay.feed(content)
        except Exception:  # noqa: BLE001 — never into the stream
            logger.warning("%s Could not read a stream event", _LOG_PREFIX, exc_info=True)

    async def on_error(self, error: Exception) -> None:
        self.relay.stop()

    async def on_complete(self, response_text: str) -> None:
        self.relay.stop()


# ---------------------------------------------------------------------------
# Attach seam
# ---------------------------------------------------------------------------

def maybe_attach_channel_relay(
    *,
    session_id: uuid.UUID,
    integration_type: str | None,
    base_handler: Any,
    get_fresh_db_session: Callable[[], Any],
) -> Any:
    """Give a channel session a streaming relay, or hand back ``base_handler``.

    The single seam the streaming path calls; everything channel-aware lives
    behind it, so the caller's change is one wrap and no branches.

    A relay is attached only when **all** of these hold: the feature is
    enabled, the session is a channel session, it has a binding, the channel
    resolves and is enabled, and the transport declares
    ``supports_status_notice``. That last gate is what keeps email and App MCP
    byte-for-byte unchanged — neither has a message it can rewrite, so neither
    has a draft to roll.

    **Declining for a channel session removes any registry entry.** A leftover
    relay from an earlier turn would be found by the completion handler, asked
    for its (empty) tail, and the full-text fallback would be skipped — the
    reader would get nothing at all. Removing on decline is not tidiness; it is
    what makes the fallback reachable.

    **Total.** Any failure at all — a bad setting, a dead pool, an adapter that
    will not resolve — degrades to "no streaming updates this turn", which is
    exactly today's behaviour, and never to a stream that will not start.
    """
    try:
        if not (integration_type or "").startswith(CHANNEL_INTEGRATION_PREFIX):
            # Not ours. Deliberately does NOT touch the registry: a non-channel
            # session never had an entry, and the session id space is shared.
            return base_handler

        relay = _build_relay(session_id, get_fresh_db_session)
        if relay is None:
            ChannelStreamRegistry.remove(session_id)
            return base_handler

        from app.services.sessions.stream_event_handlers import (
            CompositeStreamEventHandler,
        )

        ChannelStreamRegistry.put(session_id, relay)
        # The caller's handler stays the **primary**: its contract with the
        # stream processor, exceptions included, is not something a passenger
        # gets to change. The relay is the passenger — isolated, never
        # propagating.
        return CompositeStreamEventHandler(
            base_handler, [ChannelRelayEventHandler(relay)]
        )
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not attach a streaming relay to session %s; the turn will "
            "deliver its reply in one piece",
            _LOG_PREFIX,
            session_id,
            exc_info=True,
        )
        # The failure may have landed after the entry went in, and a registered
        # relay nothing feeds is exactly the stale entry the docstring warns
        # about. Removing an entry that was never added is a no-op.
        try:
            ChannelStreamRegistry.remove(session_id)
        except Exception:  # noqa: BLE001
            logger.warning("%s Could not clear the registry entry", _LOG_PREFIX)
        return base_handler


def _build_relay(
    session_id: uuid.UUID, get_fresh_db_session: Callable[[], Any]
) -> ChannelStreamRelay | None:
    """The gate conditions, in one place. Returns ``None`` for "not eligible".

    Every id the relay keeps is read here, while the instances are live and
    the session is open — the relay itself holds no rows (module docstring).

    **The binding is resolved through ``_resolve_channel_session``, not by a
    second query of its own.** That is the same helper the completion handler
    will use to find the thread this turn answers into, and
    ``ChannelThreadBinding.session_id`` is indexed but *not* unique — so two
    ``.first()`` calls over the same session have no guaranteed agreement.
    Sharing the one lookup is what makes "the draft and the final tail land in
    the same thread" a fact rather than a coincidence. It also applies the
    ``integration_type`` gate, leaving the prefix check in
    :func:`maybe_attach_channel_relay` as what it is: the *registry's* scope
    rule, not a second opinion about what a channel session is.
    """
    if not settings.CHANNEL_STREAM_UPDATES_ENABLED:
        return None

    with get_fresh_db_session() as db:
        resolved = ChannelOutboundService._resolve_channel_session(db, session_id)
        if resolved is None:
            return None
        binding, channel = resolved
        if not get_adapter(channel.channel_type).capabilities.supports_status_notice:
            return None
        return ChannelStreamRelay(
            session_id=session_id,
            binding_id=binding.id,
            channel_id=channel.id,
            get_fresh_db_session=get_fresh_db_session,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The opening halves of the tags :func:`_visible` removes, as literals. A tag
#: that has not finished arriving has no regex to match — that is exactly what
#: makes it partial — so the live draft needs the openings themselves. Pinned
#: against the shared patterns by a unit test rather than by a comment, since
#: the whole point of importing those patterns is that this file owns no second
#: model of them.
_AGENT_TAG_OPENINGS = ("<webapp_action>", "<cinna_attach>")


def _visible(text: str) -> str:
    """``text`` with the agent's control tags removed. Total.

    The stream carries assistant content **raw**: ``<webapp_action>`` and
    ``<cinna_attach>`` are stripped at *finalize*, long after the relay has
    seen them (``message_service`` ~line 2800). The channel path used to read
    the stored, already-stripped content, so sending them would be a
    regression — and not only in the live draft: the settled reply is the
    relay's own accumulated text by decision (plan §1), so the tags would
    reach the reader's final message too.

    Only **complete** tags are removed, matching finalize exactly. A slice that
    cuts a tag in half is not stripped and not truncated — it is not *sealed*:
    :func:`_has_open_tag` is what keeps a cut out of a tag body, so both halves
    stay in the buffer until the whole tag can be removed at once. The live
    draft goes one step further and hides a tag that is still arriving; see
    :func:`_visible_draft`.
    """
    try:
        if "<" not in text:
            return text
        return _ATTACH_TAG_RE.sub("", _WEBAPP_ACTION_TAG_RE.sub("", text))
    except Exception:  # noqa: BLE001 — a visible tag beats a lost draft
        logger.warning("%s Could not strip agent tags", _LOG_PREFIX, exc_info=True)
        return text


def _visible_draft(text: str) -> str:
    """:func:`_visible`, and the unfinished tag at the end of the draft too.

    A tag reaches the buffer character by character like everything else, so
    between two flushes the draft can end in ``"<cinna_a"``, then
    ``"<cinna_attach>/app/works"``, then nothing at all once the closing tag
    lands and :func:`_visible` can see the whole thing. Left alone the reader
    watches a path type itself out and vanish. Everything from the unfinished
    tag on is dropped instead — it is at most a few seconds of the draft's
    tail, and the text after it (there is none yet) is not lost: the buffer
    keeps it and the next flush shows whatever the tag turned out to be.

    **Not draft-only, despite the name.** The completed tail is
    :func:`_visible` — fully stripped, nothing withheld — but the tail of an
    *interrupted or failed* stream comes through here too
    (``take_tail(partial=True)``), and that text is **settled** into the thread
    rather than redrawn. Whatever this withholds there is gone, not deferred,
    which is why :func:`_unfinished_tag_start` looks for the **last** opening
    and not the first; see it.
    """
    visible = _visible(text)
    try:
        cut = _unfinished_tag_start(visible)
        return visible if cut is None else visible[:cut]
    except Exception:  # noqa: BLE001 — cosmetic, never worth a failed patch
        return visible


def _has_open_tag(text: str) -> bool:
    """Whether ``text`` (already :func:`_visible`) opens a tag it never closes.

    Complete pairs are gone by the time this runs, so any opening still in the
    text is one whose closing half has not arrived — or, for a *slice*, one the
    slice cut in half.

    **This is what keeps a seal out of a tag body.** Seals cut at newlines and
    ``<webapp_action>`` bodies are multi-line JSON (its pattern is ``DOTALL``
    for exactly that reason), so without this check a seal could put
    ``<webapp_action>{"action": …`` into a message that is *final* and leave
    ``…}</webapp_action>`` at the head of the next draft, where nothing can
    strip it — no opening for :func:`_visible` to pair, and not an opening for
    :func:`_unfinished_tag_start` to hide. Rejecting the cut costs one earlier
    seal; taking it costs the reader both halves of a tag, permanently.
    """
    return any(tag in text for tag in _AGENT_TAG_OPENINGS)


def _unfinished_tag_start(text: str) -> int | None:
    """Where a tag that has not finished arriving begins, if one has.

    Two shapes, in the order they occur as text streams in: an opening whose
    closing tag has not arrived (:func:`_has_open_tag`), and — before even that
    much has landed — a ``"<"`` run at the very end that could still become one
    of the openings. Either way the answer is a cut point, and everything from
    it to the end of the text is withheld.

    **The opening is looked for with ``rfind``, and that is load-bearing.** A
    tag that is *still arriving* is necessarily the **last** opening of its own
    kind in the buffer — there is nothing after it but the body that has not
    finished. ``find`` answers with the **first** one instead, and the two
    differ exactly when the agent wrote a bare opening earlier in prose. That
    is not a hypothetical for this product, whose own agents document these
    protocols in the answers they write: "emit a ``<cinna_attach>`` tag with
    the path in it" is an ordinary sentence here, and under ``find`` everything
    from the word ``<cinna_attach>`` onwards was withheld.

    On a live draft that costs nothing — :func:`_visible_draft` says so, and it
    is right: the buffer keeps the text and the next flush shows it. **On the
    final tail there is no next flush.** ``take_tail(partial=True)`` settles
    *this* text into the thread as the turn's answer, so a withheld remainder
    is not deferred, it is deleted — and deleted only on the interrupt/error
    endings, while the identical reply through ``handle_stream_completed``
    keeps it. ``rfind`` is what narrows that divergence.

    **Nothing that used to be hidden becomes visible.** Per tag ``rfind`` is at
    or after ``find``, and the aggregation below is monotone in that, so the
    cut only ever moves *later*. It can never move past a genuinely trailing
    unfinished opening, because that opening **is** its own tag's last
    occurrence: its ``rfind`` is its own index, and the minimum taken below is
    at or before it. The mid-token interrupt this branch exists for —
    ``"Here is the report.\n<cinna_attach>/app/wo"``, which the end-anchored
    ``"<"`` test underneath cannot see (the fragment is longer than the
    opening, so no tag *starts with* it) — is still cut at the opening.

    **The aggregation stays the earliest of the candidates, deliberately.**
    Taking the latest index across the two tags would read like the natural
    partner to ``rfind`` and is wrong: a ``<cinna_attach>`` literal inside a
    still-arriving ``<webapp_action>`` JSON body would then win over the
    ``<webapp_action>`` that contains it, and the cut would land inside the
    body, leaving ``<webapp_action>{"…`` standing in the settled reply. The
    outermost still-open opening is the one to cut at.

    **Residual, and named rather than fixed.** A prose mention that occurs
    *once* is also its own last occurrence, so a partial tail still drops
    everything after it. Telling that apart from a real emission means deciding
    a tag is not a tag after all — the same trade :meth:`ChannelStreamRelay.
    _choose_seal` documents refusing, for the same reason.
    """
    earliest: int | None = None
    for tag in _AGENT_TAG_OPENINGS:
        # ``rfind``: the last opening of this tag, not the first. See above.
        index = text.rfind(tag)
        # ...and the *earliest* of those per-tag candidates: the outermost
        # still-open tag, not the innermost. See above.
        if index != -1 and (earliest is None or index < earliest):
            earliest = index
    if earliest is not None:
        return earliest
    start = text.rfind("<")
    if start == -1:
        return None
    fragment = text[start:]
    if any(tag.startswith(fragment) for tag in _AGENT_TAG_OPENINGS):
        return start
    return None


def _seal_target() -> int:
    """``CHANNEL_STREAM_SEAL_TARGET_CHARS``, defended against a bad value."""
    try:
        target = int(settings.CHANNEL_STREAM_SEAL_TARGET_CHARS)
    except Exception:  # noqa: BLE001
        return 3400
    return target if target > 0 else 3400


def _message_limit(channel: ServerChannel) -> int:
    """The transport's per-message cap, read from its declared capabilities.

    Never raises: ``channel.channel_type`` is a lazy reload on an expired
    instance here just as it is everywhere else on this path, and an unknown
    transport falls back to the conservative default rather than failing a
    draft update.
    """
    try:
        limit = get_adapter(channel.channel_type).capabilities.max_message_chars
    except Exception:  # noqa: BLE001
        return _DEFAULT_MESSAGE_LIMIT
    if not isinstance(limit, int) or limit <= 0:
        return _DEFAULT_MESSAGE_LIMIT
    return limit


def _open_fence_marker(text: str) -> str | None:
    """The marker run of the fenced block still open at the end of ``text``.

    Raw-markdown model, mirroring ``markdown_to_chat._take_fenced_block``: a
    block is closed only by a fence repeating the **same marker character** it
    opened with, so this tracks the opening run rather than toggling a bool.
    The run itself is returned, not just its character, so the fence the relay
    writes is the one the block opened with — a four-backtick block re-opens
    with four. **This is a deliberate deviation from the plan's Phase-2 text**,
    which hardcodes a ```` ``` ```` for the re-open; that would leave a ``~~~``
    or four-backtick block unclosable by the remainder's own fence, so the rest
    of the answer would render as one runaway code block. Do not "correct" it
    back toward the plan.

    Deliberately not ``chat_text_chunking.fence_open_after``, whose
    backtick-only toggle is the correct model for the *translated* text that
    function is given and the wrong one here; that module's docstring explains
    why the two must not be unified.
    """
    open_marker: str | None = None
    for line in text.split("\n"):
        match = FENCE_RE.match(line)
        if match is None:
            continue
        run = match.group(2)
        if open_marker is None:
            open_marker = run
        elif run[0] == open_marker[0]:
            open_marker = None
    return open_marker


__all__ = [
    "ChannelStreamRelay",
    "ChannelStreamRegistry",
    "ChannelRelayEventHandler",
    "maybe_attach_channel_relay",
]
