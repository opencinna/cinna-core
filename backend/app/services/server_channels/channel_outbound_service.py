"""Outbound delivery: agent replies and progress notices back to the channel.

Two entry points:

* Event subscribers (``handle_stream_completed`` / ``handle_stream_error`` /
  ``handle_stream_interrupted``) registered in ``app/main.py`` next to the
  email integration's. They fire for every stream on the instance, so the
  first thing each does is a cheap gate — a dict lookup in the streaming
  relay's registry, then ``integration_type.startswith("channel_")`` —
  and everything else is only reached for sessions this feature owns.

  All three are **relay-aware**: when ``channel_stream_relay`` narrated this
  turn into the status notice, they deliver only the *tail* the reader has
  not seen yet instead of the whole answer again. When there is no relay
  (the feature is off, the transport has no notice, the stream came in
  through App MCP or A2A) every one of them behaves exactly as it did
  before the relay existed — see :meth:`ChannelOutboundService._take_stream_tail`,
  which folds every "pretend this feature does not exist" case into one
  ``None``.
* The **status notice** helpers (``set_status`` / ``set_binding_status`` /
  ``clear_binding_status``), called by the inbound pipeline to narrate the slow
  parts — routing, installing, ready, failed. On a transport that can edit its
  own posts that narration is ONE message, rewritten in place, and the last
  rewrite is the agent's own reply (``_deliver(into_status_notice=True)``) — so
  the notice does not disappear, it *becomes* the answer. A transport that can
  post but not edit gets each state as its own message instead (``set_status``
  falls through to ``_send_notice`` and keeps no id), and one that has no
  progress surface at all gets silence — both handled inside the helpers, so
  callers never branch on it.

Delivery is best-effort: three attempts inside the adapter, then the failure
is recorded on the binding and logged. A persistent outbound queue (the email
integration's ``OutgoingEmailQueue`` pattern) is a listed future enhancement —
until then a user whose reply was lost can simply ask again.

Everything here runs as an asyncio task on the main event loop. HTTP is async
httpx; the DB work mirrors what every other event handler on that loop does.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session as DBSession, select

from app.models import (
    ChannelThreadBinding,
    ServerChannel,
    Session as ChatSession,
    SessionMessage,
)
from app.models.events.event import (
    AGENT_MESSAGE_ID_META_KEY as _AGENT_MESSAGE_ID_META_KEY,
)
from app.services.routing import routing_trace
from app.services.server_channels.adapters.email import build_reply_thread_key
from app.services.server_channels.adapters.registry import (
    get_adapter,
    get_transport,
)
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_REPLIED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)
# Module scope, unlike the relay import below: the ledger service imports
# nothing from this module (it reaches the relay's ``_visible`` lazily), so
# there is no cycle to dodge here.
from app.services.server_channels.channel_tool_summary import (
    tool_only_summary_for_message,
)
from app.services.server_channels.channel_turn_delivery_service import (
    ChannelTurnDeliveryLedger,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelOutbound]"


def _binding_thread_key(
    binding: ChannelThreadBinding, channel: ServerChannel | None = None
) -> str | None:
    """The transport-facing thread key for ``binding``, or ``None``.

    **Total by construction**, and the single place a transport-facing thread
    key is derived from a binding.

    The binding-shaped sibling of
    ``channel_inbound_service._debug_channel_key``, and it exists rather than
    reusing it for one reason: that helper reads ``channel.id``, and there is
    no total reader for a *binding* attribute to reuse. The hazard is
    identical — ``binding.thread_key`` looks like a field read and is not.
    Every path into ``_deliver`` arrives after a ``db.commit()`` (the inbound
    pipeline commits between every progress notice; the event handlers commit
    while resolving the session), which expires the instance, so the read is a
    lazy reload and reloading a concurrently deleted binding raises
    ``ObjectDeletedError``.

    ``None`` means "this message cannot be addressed", and the caller declines
    to send rather than posting to a null thread — the same bargain
    ``_debug_channel_key`` strikes, for the same reason: a delivery aimed at
    nothing is worse than an honest, logged non-delivery.

    **``channel`` and the reply context (settled decision §2.7).** A polled
    transport's reply needs more than a thread id: an email answer carries
    ``In-Reply-To`` and ``References``, which name the *last* inbound message,
    not the thread root. ``send_message(channel, thread_key, text)`` has no
    room for them, so the polled key is a composite —
    ``"<root-message-id>|<last-message-id>"`` — built here and parsed by the
    transport. The **stored** ``binding.thread_key`` is untouched: it stays the
    bare root and remains the unique key everything binds by.

    ``binding.last_external_message_id`` is read **inside the same ``try``**,
    and that placement is the whole point rather than tidiness. It is the same
    expired-instance lazy reload ``thread_key`` is, so a read outside the guard
    would let a concurrently deleted binding raise out of a helper whose
    callers rely on it never raising — turning an honest declined delivery into
    a crash on the delivery path.

    ``channel`` is optional and defaults to "no reply context", which is
    exactly right for every webhook transport (Google Chat's key is already
    complete) and is what a caller that has no channel to hand gets. Only the
    transport shape decides: ``inbound_mode == "polled"``. The composite's
    format belongs to the polled transport that reads it back —
    ``adapters.email`` defines the separator, the builder and the parser in one
    place — so a second polled transport with a different reply shape needs
    its own branch here, not a different spelling of this one.
    """
    try:
        thread_key = str(binding.thread_key)
        # Same reload, same guard. See the docstring.
        last_external_message_id = binding.last_external_message_id
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read a thread key from the binding (instance expired "
            "and its row is gone?)",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None

    if channel is None:
        return thread_key

    try:
        # ``channel.channel_type`` is a lazy reload too, and this helper may
        # not raise. A channel we cannot classify degrades to the bare thread
        # key rather than to ``None``: the key is still the right address, and
        # only the threading headers are lost. (``_deliver``'s own
        # ``get_adapter`` call raises on the same row a moment later and is
        # handled there — this is not the place to answer for it.)
        transport = get_transport(channel.channel_type)
    except Exception:  # noqa: BLE001 — degrade, never raise
        logger.warning(
            "%s Could not resolve the transport for a delivery; sending with "
            "the bare thread key",
            _LOG_PREFIX,
            exc_info=True,
        )
        return thread_key

    if transport.inbound_mode != "polled":
        return thread_key
    return build_reply_thread_key(thread_key, last_external_message_id)


def _binding_status_message_id(binding: ChannelThreadBinding) -> str | None:
    """The binding's live status-notice id, or ``None``. Never raises.

    The same expired-instance lazy reload :func:`_binding_thread_key` exists
    for, and read through the same kind of guard for the same reason: every
    caller arrives after a ``db.commit()``, so this is a reload, and a
    concurrently deleted binding would raise ``ObjectDeletedError`` out of a
    helper whose callers treat it as a field read.

    ``None`` on failure is the right degradation and not merely the safe one:
    it means "no notice to patch or delete", which sends the caller down the
    post-a-fresh-one path — a visible extra message at worst, never a lost one.
    """
    try:
        return binding.status_message_id
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read the status notice id from the binding",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None


def _binding_row_id(binding: ChannelThreadBinding) -> uuid.UUID | None:
    """The binding's own primary key, or ``None``. Never raises.

    A third instance of the hazard :func:`_binding_thread_key` and
    :func:`_binding_status_message_id` exist for, and it looks even more like a
    plain field read than they do — which is exactly why it needs a helper of
    its own. Every caller here arrives after a ``db.commit()``, so ``binding.id``
    is a lazy reload and a concurrently deleted row raises
    ``ObjectDeletedError`` from it.

    Its one consumer is the turn-delivery ledger, whose whole contract is that
    a failed write costs observability and nothing else — so ``None`` means
    "no ledger for this turn", which the ledger's own entry points already
    accept.
    """
    try:
        return binding.id
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read the binding's id for the delivery ledger",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None


# Prefix used for the integration_type stamped on channel sessions.
CHANNEL_INTEGRATION_PREFIX = "channel_"

#: Appended to a partial answer when the sender stopped the turn. Markdown,
#: like every other text handed to a transport — the adapter translates it.
STOPPED_SUFFIX = "⏹️ _Stopped._"

#: The whole message when a stopped turn has no partial answer to append to:
#: either nothing had been streamed yet, or everything already went out as a
#: sealed message and this is the acknowledgement below it.
STOPPED_NOTICE = "⏹️ Stopped."

#: What the thread is told when a turn fails. Named rather than inlined
#: because the failure handler now builds two messages out of it — the bare
#: text, and the same text under whatever the relay had already streamed —
#: and a second literal is how the two would drift apart.
TURN_FAILED_TEXT = (
    "Something went wrong while I was working on that. Please try again."
)


#: Meta key on the terminal stream events (``STREAM_COMPLETED`` /
#: ``STREAM_ERROR`` / ``STREAM_INTERRUPTED``) carrying **turn identity**: the
#: id of the agent ``SessionMessage`` that batch wrote, stringified, or an
#: explicit ``None`` for a batch that wrote none. Genuinely **shared** with
#: the emitters: this is a re-export of
#: ``app.models.events.event.AGENT_MESSAGE_ID_META_KEY``, and
#: ``sessions/message_service.py`` passes the key through the same symbol
#: (``**{AGENT_MESSAGE_ID_META_KEY: ...}``) at every emission site — so a
#: rename is one edit, not two edits joined by a string that fails silently.
#:
#: **The id names a *finalized* row only on ``STREAM_COMPLETED``.** The other
#: two events are emitted from inside the streaming loop, before
#: ``_finalize_agent_message`` has run, so what they name is a row **mid-write**
#: and never a settled turn result:
#:
#: * ``STREAM_INTERRUPTED`` names the row created on the first assistant event,
#:   whose content is whatever ``_flush_streaming_to_db`` last checkpointed —
#:   a partial, roughly two seconds stale. And its ``None`` is not durable
#:   either: the finalize that runs after the loop breaks can retroactively
#:   create a row, so "this turn wrote nothing" may be false by the time
#:   anything reads it.
#: * the mid-stream ``STREAM_ERROR`` returns before finalize entirely, so the
#:   row it names is never finalized at all.
#:
#: Only ``handle_stream_completed`` therefore loads the row. A reader that
#: wants "the settled text of this turn" must take it from the completion
#: event, not from whichever terminal event arrived.
AGENT_MESSAGE_ID_META_KEY = _AGENT_MESSAGE_ID_META_KEY

#: Distinguishes "the event carries no ``agent_message_id`` key at all" from
#: "it carries the key, set to ``None``". ``meta.get(key)`` cannot tell those
#: apart and they are opposite instructions: the first is an event from code
#: predating the key (rolling deploy, stale fixture) and keeps the legacy
#: newest-row behaviour; the second is an emitter stating on the record that
#: this turn produced no agent message, where falling back to the newest row
#: is precisely the bug.
_MISSING = object()


class _Unreadable:
    """Type of :data:`_UNREADABLE`. A class so the sentinel is *typed*.

    ``object()`` would work at runtime, but it makes the helper's return type
    collapse to ``object`` and costs the caller its narrowing — and the whole
    point of this sentinel is that the caller must not be able to confuse it
    with the ``None`` beside it.
    """

    __slots__ = ()


#: "The read failed, so we do not know what this turn said" — as opposed to
#: ``None``, "we know, and it said nothing".
#:
#: The distinction is load-bearing and not defensive tidiness. ``None`` sends
#: :meth:`ChannelOutboundService.handle_stream_completed` to
#: ``clear_binding_status``, which **deletes the status notice**. That is right
#: when the turn genuinely produced no text, and catastrophic when the read
#: merely failed: a relay that narrated a partial answer into that notice and
#: then broke leaves the reader's only copy of that text standing there, and a
#: transient ``OperationalError`` on the row lookup would delete it — for a
#: reply that exists in ``SessionMessage`` and would never be sent again. It is
#: the same direction the tail contract forbids for ``take_tail`` returning
#: ``None`` (see :meth:`ChannelOutboundService._take_stream_tail`), for the
#: same reason, and folding it into ``None`` was a regression against the
#: pre-turn-identity code, where such a raise propagated to the handler's outer
#: ``except`` and left the notice alone.
#:
#: The caller's only correct response is to do nothing at all: leave the thread
#: exactly as the relay left it.
_UNREADABLE = _Unreadable()


def _agent_message_uuid(raw_message_id: Any) -> uuid.UUID | None:
    """The uuid a stream event named, or ``None``. Never raises.

    The turn-delivery ledger needs the id itself, not the text
    :func:`_agent_message_text` resolves from it — to gate on an already
    settled turn, and to attribute the turn's rows.

    **Silent by design**, unlike its sibling: the two are called on the same
    meta value in the same handler, and an unusable id is already reported at
    WARNING there. Logging it twice would read like two different problems.
    ``None`` covers ``_MISSING``, an explicit ``None`` and an unparseable
    value alike — all three mean "this event does not name a row the ledger
    can attribute to", which is one instruction, not three.
    """
    if raw_message_id is None or raw_message_id is _MISSING:
        return None
    try:
        return uuid.UUID(str(raw_message_id))
    except (TypeError, ValueError, AttributeError):
        return None


def _agent_message_text(
    db: DBSession, raw_message_id: Any
) -> str | _Unreadable | None:
    """The text of the agent message a stream event named.

    **Total** — it never raises, because its caller's contract is to be total.
    Three answers, and the third exists because it is not the second:

    * a ``str`` — the text to deliver.
    * ``None`` — **there is nothing to deliver for this turn.** A malformed or
      unparseable id in the meta, a row that has since been deleted, a row
      whose content is empty or whitespace. Folded together on purpose: each
      means the same thing to the caller, and each is a fact we actually
      established.
    * :data:`_UNREADABLE` — **the read itself failed**, so nothing was
      established. Never merged into ``None``: see that sentinel's own note.
      The caller must leave the thread untouched rather than act on a fact it
      does not have.

    The meta value crosses a process boundary as JSON, so "it is a uuid string"
    is an expectation and not a guarantee — hence the parse guard, which is a
    genuine ``None`` (we read the event fine; what it named cannot exist).

    Deliberately **not** ``_last_agent_message``: this loads exactly the row
    the completing batch wrote. That is the whole fix — see
    :attr:`AGENT_MESSAGE_ID_META_KEY`.
    """
    try:
        message_uuid = uuid.UUID(str(raw_message_id))
    except (TypeError, ValueError, AttributeError):
        logger.warning(
            "%s Stream event carried an unusable %s (%r) — treating the turn "
            "as having produced no message",
            _LOG_PREFIX,
            AGENT_MESSAGE_ID_META_KEY,
            raw_message_id,
        )
        return None

    try:
        row = db.get(SessionMessage, message_uuid)
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read agent message %s named by the stream event — "
            "leaving the thread untouched rather than assuming the turn said "
            "nothing",
            _LOG_PREFIX,
            message_uuid,
            exc_info=True,
        )
        return _UNREADABLE

    try:
        if row is None:
            return None
        return (row.content or "").strip() or None
    except Exception:  # noqa: BLE001 — ``row.content`` is a lazy reload on an
        # instance an earlier commit expired, exactly like the binding reads
        # above; a concurrently deleted row raises here rather than at ``get``.
        logger.warning(
            "%s Could not read the content of agent message %s — leaving the "
            "thread untouched",
            _LOG_PREFIX,
            message_uuid,
            exc_info=True,
        )
        return _UNREADABLE


class ChannelOutboundService:
    """Sends agent output back out through the originating channel."""

    # ------------------------------------------------------------------
    # Event subscribers
    # ------------------------------------------------------------------

    @staticmethod
    async def _take_stream_tail_ex(
        session_id: Any, *, partial: bool = False
    ) -> tuple[str | None, bool]:
        """:meth:`_take_stream_tail`, plus the one fact it has to drop.

        Answers ``(tail, relay_failed)`` where ``tail`` is exactly what
        :meth:`_take_stream_tail` returns and ``relay_failed`` says whether the
        ``None`` came from a relay that **broke** rather than from a session
        that has no relay at all.

        That second fact is the whole reason this form exists, and its absence
        has now produced the same defect twice in opposite directions.
        ``take_tail`` answering ``None`` (the relay broke, and may be holding
        the only copy of a partial answer in a standing draft) and
        ``("", False)`` (the relay is fine and the stream simply produced
        nothing, which is what stopping a turn before the agent speaks looks
        like) collapse into one ``None`` here — and
        :meth:`handle_stream_interrupted` needs them apart, because one must be
        met with silence and the other with the stopped marker. Asking a
        separate "is there a live relay" question instead cannot tell them
        apart either: both shapes have one.

        The two siblings do not need it — both recover through a full-text path
        that overwrites the notice either way — so :meth:`_take_stream_tail`
        keeps the two-value contract and delegates here.

        **The exception default is split, deliberately.** A failure *before* a
        relay is in hand (the import — the module is in ``sys.modules`` if a
        relay was ever attached, so an import failure means there was none —
        or ``str(session_id)``) is honestly "no relay", and the caller is right
        to write the notice. A failure *after* one is in hand — the ``spent``
        read, ``take_tail`` itself — is a relay that demonstrably exists and
        whose draft may be standing in the notice; reported as "no relay" it
        would settle a tombstone over an answer. Same reason the ``None`` from
        ``take_tail`` is flagged rather than flattened.
        """
        relay: Any = None
        try:
            # Function-level: ``channel_stream_relay`` imports this module at
            # import time, so the reverse edge would be circular. Same dodge
            # as ``_deliver``'s.
            from app.services.server_channels.channel_stream_relay import (
                ChannelStreamRegistry,
            )

            relay = ChannelStreamRegistry.get(str(session_id))
            if relay is None or relay.spent:
                return None, False
            taken = await relay.take_tail(partial=partial)
            if taken is None:
                # The relay failed to answer. Full-text path for the two
                # siblings; silence for the interrupt handler, which is what
                # the flag buys.
                return None, True
            tail, delivered_any = taken
            if tail:
                return tail, False
            # Empty tail: a no-op only if the relay put something on screen.
            return ("" if delivered_any else None), False
        except Exception:  # noqa: BLE001 — a relay may never cost an answer
            logger.warning(
                "%s Could not read the streaming relay for session %s — "
                "delivering the whole answer instead",
                _LOG_PREFIX,
                session_id,
                exc_info=True,
            )
            return None, relay is not None

    @staticmethod
    async def _take_stream_tail(
        session_id: Any, *, partial: bool = False
    ) -> str | None:
        """What the live relay has not put on screen yet, or ``None``.

        The one place the three event subscribers ask "did
        ``channel_stream_relay`` narrate this turn, and if so what is left to
        send?". It answers with two values and no third state to get wrong:

        * ``None`` — **behave exactly as if this feature did not exist.** Take
          the pre-relay path: this turn's stored ``SessionMessage`` (resolved
          by the id the event carries, never by recency), the bare failure
          text, the notice-only stop acknowledgement.
        * a ``str`` — the relay owns this turn. Deliver this text (``""`` means
          it already delivered everything itself).

        Collapsing to that pair is the whole point, because the relay's
        ``take_tail`` has three answers and the two that look alike are not
        (its docstring, and the module docstring's "consumer contract", state
        this normatively — read them before changing anything here):

        * ``None`` from ``take_tail`` is "something in me broke", **not**
          "there was nothing". The agent's reply is in ``SessionMessage``
          whatever happened in the relay, so the honest recovery is the
          full-text path. Routing it to ``clear_binding_status`` instead would
          delete the notice and send nothing — losing an answer that exists.
        * ``("", False)`` — the stream genuinely produced nothing — also
          belongs on the full-text path, which degrades to the delete on its
          own when there really is no message text. It has to go there for a
          second reason: the registry is keyed by *session*, and a late
          handler can read the **next** turn's relay, which truthfully reports
          "nothing yet, nothing delivered". On the full-text path that handler
          settles its own turn's stored answer; on the delete path it would
          erase the notice of a turn still in progress.
        * only ``("", True)`` — "everything is already on screen" — is a
          genuine no-op, and that is the ``""`` this returns.

        Two things it deliberately does **not** do:

        * It never calls ``relay.stop()``. ``ChannelRelayEventHandler``'s
          ``on_complete``/``on_error`` own that and fire once per *turn*;
          ``STREAM_COMPLETED`` fires once per *LLM batch*, so stopping here
          would stream batch 1 live and leave every batch after it silent.
        * It never pops the registry entry, for the same reason:
          ``maybe_attach_channel_relay`` is the only writer, and the next
          batch's handler must still find the relay.

        A :attr:`~ChannelStreamRelay.spent` relay is read as absent — it is the
        registry's own discriminator for "the entry under this session id
        belongs to a turn that is already finished business" — and is answered
        *without* taking its tail.

        **Called before the DB session is opened, not inside it.** ``take_tail``
        waits on the relay's flush lock, which a flush holds across its adapter
        round trips (429 backoff included) — and that flush needs a pooled
        connection of its own to make them. A handler that waited there while
        holding one would put the two on opposite sides of the pool. The cost
        is that a turn whose binding vanished mid-stream has its tail taken
        with nowhere to send it, which delivers exactly as much as the
        pre-relay code did in the same situation: nothing.

        Total, like everything else on this path: any failure is "no relay".

        ``partial=True`` is for a caller whose stream ended **mid-token** —
        the interrupt and error handlers — and is passed straight through to
        ``ChannelStreamRelay.take_tail``, which then also hides a tag that was
        still arriving when the stream stopped. See that method: it is an
        opt-in because the stricter stripping would cost a completed reply the
        odd trailing ``"<c"``.

        The implementation is :meth:`_take_stream_tail_ex` with its second
        answer dropped, which is the only difference between the two and the
        reason this one still exists: two of the three subscribers genuinely do
        not care why the relay had nothing, and folding the flag into their
        branches would invite a reader to think it changes something there.
        """
        tail, _relay_failed = await ChannelOutboundService._take_stream_tail_ex(
            session_id, partial=partial
        )
        return tail

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """STREAM_COMPLETED — deliver the agent's final message to the thread.

        Fires once per **LLM batch**, not once per turn, so with a relay
        attached each call delivers that batch's increment (``take_tail`` is
        idempotent and advances past what it hands over) and a duplicate event
        delivers nothing.

        **The relay-absent arm delivers by turn identity, never by recency.**
        The event names the agent ``SessionMessage`` its batch wrote
        (:attr:`AGENT_MESSAGE_ID_META_KEY`), and three states of that key are
        three different instructions:

        * **a uuid** → deliver *that* row's text, and nothing else. An empty
          row, or one that has since gone, reads as "the turn said nothing".
        * **an explicit ``None``** → the turn wrote no agent message (a command
          stream, a batch that never got an assistant event). Settle the
          notice; **never** fall back to the newest row. Doing so is the bug
          this arm exists to fix: it re-delivered the *previous* turn's answer
          into the thread as if it answered the new question.
        * **the key absent entirely** → an event from code predating the key
          (a rolling deploy, a stale fixture). Only there does the legacy
          newest-row query survive, logged at debug.

        A fourth outcome is not a state of the key but of the lookup: if
        reading the named row **fails** (:data:`_UNREADABLE`), this handler
        returns having done nothing — it may not settle a notice on a fact it
        failed to establish. That case and the ``None`` above look alike and
        are opposites; the sentinel's note says why.

        The relay-present arm above is untouched: its tail is turn-scoped by
        construction, because the relay is created per turn.

        **The turn-delivery ledger wraps all of it.** Before anything is sent,
        an existing ``final`` row for this batch's agent message means the turn
        is already answered and this event is a duplicate — the one place a
        racing or redelivered ``STREAM_COMPLETED`` is stopped. After the send,
        :meth:`_settle_turn_ledger` attributes the rows the relay wrote at its
        seals, records the final message, and checks that the finalized
        canonical text still starts with what was already sealed into the
        thread. That check **changes no behaviour in either outcome** — the
        settled reply is the relay's own text by decision, and stays so — it
        only turns a silent assumption into a warning that can fire. Every
        ledger call here is total: a delivery is never gated on, delayed by, or
        lost to bookkeeping.
        """
        try:
            from app.core.db import create_session

            meta = event_data.get("meta") or {}
            session_id = meta.get("session_id")
            if not session_id or meta.get("was_interrupted"):
                return

            # Read with the sentinel, not ``meta.get(...)``: "key absent" and
            # "key present, None" are opposite instructions here and a plain
            # ``get`` collapses them into one. See the docstring.
            raw_agent_message_id = meta.get(AGENT_MESSAGE_ID_META_KEY, _MISSING)
            # The same value as an id rather than as text: the ledger gates and
            # attributes on it. ``None`` for every shape that does not name a
            # row — see the helper.
            agent_message_uuid = _agent_message_uuid(raw_agent_message_id)

            tail = await ChannelOutboundService._take_stream_tail(session_id)

            with create_session() as db:
                resolved = ChannelOutboundService._resolve_channel_session(
                    db, session_id
                )
                if resolved is None:
                    return
                binding, channel = resolved
                binding_id = _binding_row_id(binding)

                if ChannelTurnDeliveryLedger.turn_already_settled(
                    db, agent_message_uuid, binding_id
                ):
                    # This batch's agent message already has a ``final`` row
                    # that reached the thread, so this event is a duplicate (a
                    # redelivered bus event, a racing scheduler flush) and
                    # delivering again would post the answer twice. Safe for a
                    # multi-batch turn: every batch writes its own agent
                    # message, so batch 2 asks about an id batch 1 never
                    # settled. A ``failed`` final row deliberately does not
                    # gate — see the ledger method.
                    logger.debug(
                        "%s Turn %s for session %s is already settled — "
                        "skipping a duplicate completion",
                        _LOG_PREFIX,
                        agent_message_uuid,
                        session_id,
                    )
                    return

                if tail is not None and not tail:
                    # The relay already put this batch's text on screen. Nothing
                    # to send, and nothing to clean up either way: usually the
                    # draft's id was released by the seal that emptied it, but
                    # an id CAN still be standing here (an earlier batch's
                    # ``take_tail`` empties the draft, and ``_deliver`` keeps
                    # the id whenever it fell back to a plain post), and it is
                    # left alone on purpose — a notice standing from an earlier
                    # state is patched by the next turn.
                    #
                    # It used to return above, before the DB session was
                    # opened, to keep the no-op free. It no longer can: the
                    # turn is over, the relay's boundary rows are sitting in
                    # the ledger unattributed, and the id that attributes them
                    # arrived on this event and nowhere else. Left unsettled
                    # they would be adopted by the *next* completion on this
                    # thread and recorded against the wrong turn. The cost is
                    # one pooled connection on a path that is already the
                    # rarer of the two (it needs a relay that delivered
                    # everything within the flush interval), and every other
                    # branch of this handler opens one anyway.
                    ChannelOutboundService._settle_turn_ledger(
                        db,
                        binding_id=binding_id,
                        session_message_id=agent_message_uuid,
                        external_message_id=None,
                        delivered=True,
                    )
                    return

                if tail is not None:
                    # The relay has been rewriting the notice all turn; the
                    # notice's slot holds the draft, and this patches the last
                    # increment into it. Deliberately the relay's own text and
                    # not the stored message (plan §1): they differ after
                    # webapp-action stripping and ``<cinna_attach>``
                    # materialisation, and the reader has been watching this
                    # one.
                    sent, written_id = await ChannelOutboundService._deliver_ex(
                        db=db,
                        channel=channel,
                        binding=binding,
                        text=tail,
                        into_status_notice=True,
                    )
                    # After the delivery, never before it: the ledger is
                    # bookkeeping about a message that has already gone out,
                    # and reading the canonical text ahead of the send would
                    # put a database round trip in front of the reader's reply
                    # for no gain.
                    ChannelOutboundService._settle_turn_ledger(
                        db,
                        binding_id=binding_id,
                        session_message_id=agent_message_uuid,
                        external_message_id=written_id,
                        delivered=sent,
                    )
                    return

                if raw_agent_message_id is _MISSING:
                    # Legacy arm, and the only surviving caller of the
                    # newest-row query. An event emitted before this key
                    # existed cannot say which message its turn wrote, so the
                    # honest fallback is the behaviour that event was written
                    # against — wrong on a turn that produced nothing, right
                    # on the overwhelmingly common turn that produced one.
                    logger.debug(
                        "%s Stream event for session %s carries no %s — "
                        "falling back to the newest agent message (an event "
                        "from code predating turn identity)",
                        _LOG_PREFIX,
                        session_id,
                        AGENT_MESSAGE_ID_META_KEY,
                    )
                    text = ChannelOutboundService._last_agent_message(
                        db, uuid.UUID(str(session_id))
                    )
                elif raw_agent_message_id is None:
                    # The emitter states this batch wrote no agent message.
                    # There is nothing to deliver, and looking for something
                    # anyway is the whole bug.
                    text = None
                else:
                    resolved_text = _agent_message_text(
                        db, raw_agent_message_id
                    )
                    if isinstance(resolved_text, _Unreadable):
                        # The read failed, so we do not know whether this turn
                        # said anything — and every other branch here acts on
                        # knowing. Falling through would reach
                        # ``clear_binding_status`` and DELETE the notice, which
                        # is where a broken relay's partial answer is standing:
                        # the reader's only copy of text that exists in
                        # ``SessionMessage`` and will never be sent again.
                        # Leaving the thread exactly as the relay left it costs
                        # this batch's delivery and keeps what is on screen,
                        # which is the same way round the tail contract
                        # resolves ``take_tail`` returning ``None``. Already
                        # logged at WARNING inside the helper.
                        #
                        # The ledger still gets its attribution, because it is
                        # not a statement about the thread: ``tail is None``
                        # covers a relay that **broke**, and such a relay may
                        # have sealed messages standing with rows waiting for
                        # a turn id. ``write_final=False`` — nothing was
                        # delivered here, so nothing may be recorded as final.
                        ChannelOutboundService._settle_turn_ledger(
                            db,
                            binding_id=binding_id,
                            session_message_id=agent_message_uuid,
                            external_message_id=None,
                            delivered=False,
                            write_final=False,
                        )
                        return
                    text = resolved_text
                    # A tool-only turn stores the "Agent response" finalize
                    # placeholder as its content (the row exists precisely
                    # because tool events are storable), and the web UI never
                    # shows it — it renders the stored events instead. A
                    # channel reader has no event renderer, so this is where
                    # the placeholder used to reach them verbatim. Deliver the
                    # channel-side equivalent of the UI's compact tool blocks
                    # instead: one fenced line per call, no payload content.
                    # Decided on events, never by comparing content against
                    # the placeholder literal — a summary replaces the text
                    # only when the events prove the turn said nothing.
                    # Reaches both arms that matter: with no relay this is the
                    # only delivery path, and with one, a tool-only turn gave
                    # the relay nothing so its ("", False) lands here too.
                    #
                    # Gated on ``text``: an EMPTY row keeps the documented
                    # "turn said nothing" contract below even when tool events
                    # are stored beside it (reachable — a turn whose whole
                    # output was an attachment tag has the tag stripped after
                    # the placeholder fallback ran). The actual tool-only turn
                    # always has the truthy placeholder as content, so the
                    # gate costs it nothing.
                    if text and agent_message_uuid is not None:
                        summary = tool_only_summary_for_message(
                            db, agent_message_uuid
                        )
                        if summary is not None:
                            text = summary

                if not text:
                    logger.debug(
                        "%s Nothing to deliver for session %s (%s=%s) — "
                        "settling the notice",
                        _LOG_PREFIX,
                        session_id,
                        AGENT_MESSAGE_ID_META_KEY,
                        raw_agent_message_id
                        if raw_agent_message_id is not _MISSING
                        else "<absent>",
                    )
                    # Nothing to put in the notice's slot, and the turn is over
                    # — so this is one of the two edges where the notice really
                    # is deleted. A "working on your message" left standing
                    # over a stream that produced nothing would outlive the
                    # work it narrates and be rewritten by the NEXT turn,
                    # telling the person we are still busy with a message they
                    # sent minutes ago.
                    #
                    # **No ``final`` row**: nothing was delivered, and a final
                    # row would record a delivery that did not happen. The
                    # pending rows are still attributed, though — ``tail is
                    # None`` is the tristate's *relay-failed* answer as well as
                    # its relay-absent one (see ``_take_stream_tail``), so a
                    # relay that narrated half an answer into sealed messages
                    # and then broke reaches exactly here. Leaving its rows
                    # unattributed would hand them to the next completion on
                    # this thread and record them against the wrong turn. With
                    # no relay at all there is nothing pending and this is a
                    # no-op.
                    #
                    # The remaining cost is that a duplicate completion is not
                    # gated here — it re-runs a delete that is already
                    # idempotent.
                    ChannelOutboundService._settle_turn_ledger(
                        db,
                        binding_id=binding_id,
                        session_message_id=agent_message_uuid,
                        external_message_id=None,
                        delivered=False,
                        write_final=False,
                    )
                    await ChannelOutboundService.clear_binding_status(
                        db=db, channel=channel, binding=binding
                    )
                    return

                # Into the notice's slot, not underneath it. See `_deliver`.
                sent, written_id = await ChannelOutboundService._deliver_ex(
                    db=db,
                    channel=channel,
                    binding=binding,
                    text=text,
                    into_status_notice=True,
                )
                ChannelOutboundService._settle_turn_ledger(
                    db,
                    binding_id=binding_id,
                    session_message_id=agent_message_uuid,
                    external_message_id=written_id,
                    delivered=sent,
                    canonical_text=text,
                )
        except Exception:
            # An event handler must never raise into the bus.
            logger.exception("%s handle_stream_completed failed", _LOG_PREFIX)

    @staticmethod
    def _settle_turn_ledger(
        db: DBSession,
        *,
        binding_id: uuid.UUID | None,
        session_message_id: uuid.UUID | None,
        external_message_id: str | None,
        delivered: bool,
        canonical_text: str | None = None,
        write_final: bool = True,
    ) -> None:
        """Close this turn in the delivery ledger. Never raises.

        Called from :meth:`handle_stream_completed` **after** the delivery, on
        every arm that actually wrote a message. It hands the turn's identity
        to the rows the relay wrote at its boundaries (the only place that
        column is ever filled in), settles the last one as ``final``, and runs
        the divergence check.

        ``canonical_text`` is the finalized ``SessionMessage`` content where
        the caller already has it — or, on a tool-only turn, the compact tool
        summary that was substituted for the stored placeholder and actually
        delivered: the ledger records what went out, and such a turn has no
        sealed rows for the divergence check to compare against anyway. Where it does not — the relay arms, which
        deliver the relay's own accumulated text and never load the row — it is
        read here, because the divergence check has nothing to compare against
        without it. That read is the *only* reason this arm touches the row at
        all, it happens after the reply is already out, and its failure
        (:data:`_UNREADABLE`) costs the check and nothing else: the ledger
        still records what was delivered.

        ``write_final=False`` is for the two arms that deliver **nothing** and
        still owe the ledger an attribution — see the call sites. They pass it
        because a ``final`` row would claim a delivery that did not happen,
        while leaving the rows pending hands a broken relay's already-posted
        messages to the *next* turn on this thread.

        Total twice over. The ledger's own entry points swallow everything, and
        this wrapper swallows again, because it is called from the middle of a
        handler whose remaining statements must run — §11a Rule 2, the same
        reason ``_deliver``'s argument expressions are hoisted.
        """
        try:
            if canonical_text is None and session_message_id is not None:
                resolved = _agent_message_text(db, session_message_id)
                canonical_text = resolved if isinstance(resolved, str) else None
            ChannelTurnDeliveryLedger.settle_turn(
                db,
                binding_id=binding_id,
                session_message_id=session_message_id,
                external_message_id=external_message_id,
                delivered=delivered,
                canonical_text=canonical_text,
                write_final=write_final,
            )
        except Exception:  # noqa: BLE001 — bookkeeping may not cost a reply
            logger.warning(
                "%s Could not settle the delivery ledger for turn %s",
                _LOG_PREFIX,
                session_message_id,
                exc_info=True,
            )

    @staticmethod
    def _close_out_unsettled_ledger(
        db: DBSession,
        *,
        binding_id: uuid.UUID | None,
        session_message_id: uuid.UUID | None,
    ) -> None:
        """Attribute a terminated turn's pending rows. Never raises, never sends.

        The counterpart to :meth:`_settle_turn_ledger` for the two handlers
        that end a turn **without** a completion: an interrupt and a
        mid-stream error. Without it their rows keep ``session_message_id
        IS NULL`` forever, and ``settle_turn``'s adoption is deliberately
        greedy — every pending row on the binding — so the *next* completion
        on this thread picks them up and stamps them with its own message id.
        That is wrong twice: the ledger records a previous turn's sealed
        message as part of a turn it was never in, and the divergence check
        then compares the new turn's answer against the old turn's prefix and
        reports a mismatch that did not happen. A check that cries wolf is
        worse than no check, because the mismatch policy is explicitly one we
        revisit "only if logs show it firing".

        **Attribution only — ``write_final=False`` and ``canonical_text=None``.**
        Nothing was delivered as this turn's answer, so no ``final`` row is
        written (which is also what keeps these rows out of
        :meth:`ChannelTurnDeliveryLedger.turn_already_settled`, whose gate
        matches on ``role == final``: a close-out can never be the reason a
        later real completion withholds a reply). And with no canonical text
        there is nothing for the divergence check to compare, which is correct
        rather than merely convenient — see below.

        **Why using ``agent_message_id`` here does not break the rule that
        this event's row must not be read.** The hazard the interrupt handler
        documents is that the row's *content* at this moment is whatever
        ``_flush_streaming_to_db`` last checkpointed — a partial of a partial,
        or the previous turn's text on a row not yet written. That forbids
        reading the content, and this does not read it: the id is used as a
        foreign key and nothing else, no text is loaded, and no claim is made
        about what was settled. It is deliberately routed around
        :meth:`_settle_turn_ledger`, whose convenience read of the canonical
        text is exactly the forbidden read on this path.

        **Idempotent by construction**, which matters because
        ``STREAM_INTERRUPTED`` fires once per interrupted *batch* and
        ``STREAM_ERROR`` has more than one emission shape. A second
        invocation finds nothing pending, stages nothing and commits nothing.

        A missing or unparseable id (``None``) leaves the rows pending and
        does nothing. Inventing a key would put a row under a turn that did
        not write it, which is the failure this method exists to prevent.
        """
        if binding_id is None or session_message_id is None:
            return
        try:
            ChannelTurnDeliveryLedger.settle_turn(
                db,
                binding_id=binding_id,
                session_message_id=session_message_id,
                external_message_id=None,
                delivered=False,
                canonical_text=None,
                write_final=False,
            )
        except Exception:  # noqa: BLE001 — §11a Rule 2; bookkeeping about a
            # turn that is already over may not disturb the handler that is
            # closing it, and the ledger's own entry points swallow first.
            logger.warning(
                "%s Could not close out the delivery ledger for turn %s",
                _LOG_PREFIX,
                session_message_id,
                exc_info=True,
            )

    @staticmethod
    async def handle_stream_error(event_data: dict[str, Any]) -> None:
        """STREAM_ERROR — tell the thread the turn failed, briefly.

        The error text itself is deliberately not forwarded: it can carry
        internal detail, and the external caller can act on neither.

        With a relay attached the apology goes out **under** whatever the agent
        had already streamed, rather than over it. The draft is the notice, so
        replacing it with the bare apology would take back the half-answer the
        reader has been watching for the last minute — and a half-answer plus
        "something went wrong" is strictly more useful than the apology alone.
        """
        try:
            from app.core.db import create_session

            meta = event_data.get("meta") or {}
            session_id = meta.get("session_id")
            if not session_id:
                return

            # For the ledger close-out below, and for nothing else — the
            # apology this handler delivers is fixed and reads no row. A plain
            # ``get`` rather than the completion handler's sentinel dance
            # because here "key absent" and "key present, None" really are one
            # instruction: both mean this event names no row to attribute to,
            # and there is no legacy fall-back branch to tell apart.
            agent_message_uuid = _agent_message_uuid(
                meta.get(AGENT_MESSAGE_ID_META_KEY)
            )

            # ``partial``: a failed stream can stop in the middle of a control
            # tag, and this text is settled into the thread as the answer.
            tail = await ChannelOutboundService._take_stream_tail(
                session_id, partial=True
            )

            with create_session() as db:
                resolved = ChannelOutboundService._resolve_channel_session(
                    db, session_id
                )
                if resolved is None:
                    return
                binding, channel = resolved

                # The failure notice takes the status notice's slot too: it is
                # this turn's answer, and the person should read one message,
                # not a spinner with an apology under it.
                #
                # ``tail`` is falsy for both "no relay" and "the relay already
                # delivered everything", and both want the same message here:
                # the bare apology, patched into the notice exactly as before.
                await ChannelOutboundService._deliver(
                    db=db,
                    channel=channel,
                    binding=binding,
                    text=(
                        f"{tail}\n\n{TURN_FAILED_TEXT}"
                        if tail
                        else TURN_FAILED_TEXT
                    ),
                    into_status_notice=True,
                )

                # Last statement, after the apology has already gone out: the
                # relay's boundary rows for this turn would otherwise stay
                # pending and be adopted by the next completion on the thread.
                # Attribution only — nothing here decides, delays or alters
                # what was just delivered.
                ChannelOutboundService._close_out_unsettled_ledger(
                    db,
                    binding_id=_binding_row_id(binding),
                    session_message_id=agent_message_uuid,
                )
        except Exception:
            logger.exception("%s handle_stream_error failed", _LOG_PREFIX)

    @staticmethod
    async def handle_stream_interrupted(event_data: dict[str, Any]) -> None:
        """STREAM_INTERRUPTED — settle the thread on what got said, and stop.

        Emitted **instead of** ``STREAM_COMPLETED`` when a turn is interrupted
        (``message_service`` guards the completion emission with
        ``if not was_interrupted``), so without this subscriber an interrupted
        channel turn leaves the notice stranded on "💬 Working on your
        message…" until the next turn patches it — telling the person we are
        still busy with something they cancelled. It is also what a channel
        ``/stop`` command will lean on for its visible acknowledgement once
        that command exists, which is why this is designed to say the whole
        thing itself rather than to be paired with a reply.

        **There is no full-text fallback here, and that is forced.**
        ``handle_stream_completed`` recovers by loading the message its event
        names (``handle_stream_error`` reads no row either — it has a fixed
        apology to fall back on); this one can do neither, because the stored
        message is not written yet when the event fires. That is also why no
        branch below resolves text through the event's ``agent_message_id``
        even though the event now carries one: the id may name a row whose
        content is still a mid-stream checkpoint, and reading it would
        reintroduce exactly the "a partial of a partial" text the branches
        below exist to avoid. The prohibition is on the row's **content**; the
        id is used once, below every branch, as the key that closes this
        turn's ledger rows out — see :meth:`_close_out_unsettled_ledger`.
        ``STREAM_INTERRUPTED`` is emitted the moment the ``interrupted`` event
        is seen, inside the streaming loop; ``_finalize_agent_message`` runs
        several hundred lines later, after the loop breaks — and bus handlers
        are dispatched with ``create_task``, so this one reaches the DB during
        that window. What it would read is whatever ``_flush_streaming_to_db``
        last checkpointed, which it does every two seconds against a relay fed
        per assistant event: a partial of a partial, or — since the row is
        created only on the first assistant event — the *previous* turn's
        completed answer, which is the shape a ``/stop`` before the agent says
        anything would produce every time. Every branch below therefore works
        from what is already on the reader's screen, and never from the row.
        Put the other way round: the standing draft is the best text available
        here — normally a superset of the row, and bounded-lossy by one flush
        interval against it.

        Five shapes, none of them clearing — the turn is over, and the
        tombstone rule in the band comment below applies:

        * **a tail to hand over** → into the notice, with the stopped marker
          under it, through ``_deliver`` so an over-long partial still chunks
          (``replace_message`` chunks; ``update_message``, which the notice
          verb uses, truncates — which is why the bare-marker branches may use
          that verb and this one may not).
        * **the relay put everything on screen already** (``""``) → the marker
          on its own, below the last sealed message.
        * **the relay broke and could not hand its tail over**
          (``relay_failed``) → **silence.** A relay that failed its tail read
          may still have a confirmed draft standing in the notice, and settling
          the bare marker over it would replace an answer the reader watched
          arrive with a two-word tombstone — permanently, since nothing else
          delivers that text. Leaving the draft alone costs the acknowledgement
          and keeps the answer, which is the right way round; the thread is
          then exactly where it would have been before this subscriber existed,
          and the next turn patches the notice.
        * **the relay is fine and the stream said nothing at all** → the
          marker, over the spinner. This is what ``/stop`` on a turn the agent
          has not started answering looks like, i.e. the likeliest interrupt
          there is, and it is the reason this handler asks
          :meth:`_take_stream_tail_ex` rather than its two-value sibling: that
          sibling reports this case and the broken-relay case with the same
          ``None``, and "is there a live relay at all" cannot separate them
          either — a turn interrupted before its first token has a registered,
          non-spent relay just the same. Answered with silence, which is what
          the collapsed form produced, the notice stays stranded on
          "💬 Working on your message…" on the very shape this subscriber was
          added for.
        * **no relay, and this thread was narrating** → the marker, settled
          over the spinner. Safe precisely because there was no relay: nothing
          but the pipeline's own progress text can be in that notice. Shares a
          branch with the case above: both have nothing to deliver and a notice
          that is standing, and both want the acknowledgement written into it.
        * **nothing narrating** → silence. A thread that was not showing a
          spinner gets no message about a turn it never saw start — which is
          also what holds the email transport at zero behaviour change, since a
          transport with no progress surface can never hold a notice id.

        **Why the ``""`` branch may settle over a standing notice id.** Not
        because the combination is unreachable; it is reachable, at least
        three ways. A prior batch's ``take_tail`` empties the draft while
        ``_deliver``'s fall-back-to-post keeps the id; a second interrupted
        batch reads the same relay again; and a concurrent next turn can write
        a fresh spinner onto the row before this handler runs. It is safe
        because of what such an id can *hold*: the relay releases the id on
        every seal, and ``_deliver`` keeps it only where it has already posted
        a superset of that text below. A notice standing at this point
        therefore never holds answer text that exists nowhere else. That is
        the property to re-check before touching this branch — not the
        reachability, which does not hold.

        Fires once per interrupted **batch**, like its sibling, so a turn whose
        second batch is also interrupted acknowledges twice. Left alone
        deliberately: the duplicate is one short line, and suppressing it means
        per-turn state in a handler whose whole virtue is having none.

        **It does not read the turn-delivery ledger either, and that is the
        same refusal.** "Was anything delivered this turn?" looks like a
        question the ledger could answer for the relay-absent arms, and it
        cannot answer it *for this turn*: the only key available here is the
        binding, because a row is attributed to its agent message at
        completion and this event is not one — and rows keyed by binding
        alone belong to whatever ran on this thread most recently, which is
        the recency inference the ledger exists to delete. The five branches
        below stay exactly as they are; they work from what is on the reader's
        screen, which is a fact this handler actually has.

        That refusal is about *reading* the ledger to decide a delivery, and
        it stands. It says nothing against *writing* to it once every such
        decision has been made and acted on, which is what the close-out in
        the ``finally`` does — and which is what stops this turn's rows from
        being adopted by the next completion on the thread.
        """
        try:
            from app.core.db import create_session

            meta = event_data.get("meta") or {}
            session_id = meta.get("session_id")
            if not session_id:
                return

            # The turn's key for the ledger close-out at the bottom, and the
            # only use this handler has for the id: none of the five branches
            # below consults it, and none of them may. Absent, ``None`` and
            # unparseable collapse to ``None`` here, which the close-out
            # reads as "leave these rows pending".
            agent_message_uuid = _agent_message_uuid(
                meta.get(AGENT_MESSAGE_ID_META_KEY)
            )

            # ``partial``: an interrupted stream can stop in the middle of a
            # control tag, and this text is settled into the thread as the
            # answer. ``_ex``: the flag below is the one fact the two-value
            # form has to drop, and this handler is the one that needs it.
            (
                tail,
                relay_failed,
            ) = await ChannelOutboundService._take_stream_tail_ex(
                session_id, partial=True
            )

            with create_session() as db:
                resolved = ChannelOutboundService._resolve_channel_session(
                    db, session_id
                )
                if resolved is None:
                    return
                binding, channel = resolved

                try:
                    if tail:
                        await ChannelOutboundService._deliver(
                            db=db,
                            channel=channel,
                            binding=binding,
                            text=f"{tail}\n\n{STOPPED_SUFFIX}",
                            into_status_notice=True,
                        )
                        return

                    if relay_failed:
                        # The relay owned this notice and could not tell us what is
                        # in it. Anything written here risks overwriting the only
                        # copy of the answer. See the docstring.
                        logger.warning(
                            "%s Stream interrupted for session %s while its relay "
                            "could not hand over a tail — leaving the draft "
                            "standing rather than settling over it",
                            _LOG_PREFIX,
                            session_id,
                        )
                        return

                    if tail is None and _binding_status_message_id(binding) is None:
                        # Nothing was narrating this thread — no relay, and no
                        # notice standing either. ``tail is None`` also covers the
                        # relay that is perfectly healthy and simply has nothing
                        # (a turn stopped before the agent spoke), and that is
                        # exactly right: such a turn *does* have a spinner
                        # standing, so it falls through to the settle below and
                        # gets its acknowledgement. Read through the total helper:
                        # this is a lazy reload on an instance the resolve above
                        # expired.
                        return

                    # ``settle``: the marker IS this turn's last word, so it is
                    # written once and the id let go. Where there is no id (the
                    # relay's last act was a seal) this posts it as a fresh message
                    # under the sealed text — the same acknowledgement, one message
                    # lower.
                    await ChannelOutboundService.set_binding_status(
                        db=db,
                        channel=channel,
                        binding=binding,
                        text=STOPPED_NOTICE,
                        settle=True,
                    )
                finally:
                    # Runs on every exit above, including each early
                    # ``return``, and deliberately as a ``finally`` rather than
                    # five copies: the branches are regression guards and the
                    # close-out has to be reachable from all of them without
                    # any of them being restructured to reach it. It cannot
                    # touch delivery — a ``finally`` that neither raises nor
                    # returns cannot change which branch ran or what it
                    # returned, the helper is total, and every send above has
                    # already been awaited by the time it runs. Attribution
                    # only; see the helper for why the id is usable here when
                    # the row it names is not.
                    ChannelOutboundService._close_out_unsettled_ledger(
                        db,
                        binding_id=_binding_row_id(binding),
                        session_message_id=agent_message_uuid,
                    )
        except Exception:
            logger.exception("%s handle_stream_interrupted failed", _LOG_PREFIX)

    # ------------------------------------------------------------------
    # Status notice
    # ------------------------------------------------------------------
    #
    # One message per thread that narrates the slow work and then turns into
    # the answer. The alternative — and what this replaced — is a notice per
    # state: "finding an assistant", "setting up X", "ready, working on it",
    # each a permanent message sitting above the answer the person actually
    # asked for. Chat gives us ``patch`` over our own posts, so the whole
    # narration can be one message that mutates all the way through.
    #
    # Three verbs, and the difference between them is the whole model:
    #
    #   set     post the notice, or rewrite it in place. The id is kept.
    #   settle  rewrite it one last time and let go of the id — for a text
    #           that IS the answer ("no assistant matched", "setup failed").
    #           The message stays; nothing will rewrite it again.
    #   clear   delete it. NOT the normal end of a turn — an agent's reply
    #           *takes* the slot (``_deliver(into_status_notice=True)``)
    #           rather than being posted under a notice that is then removed,
    #           because Chat leaves a "Message deleted by its author"
    #           tombstone and that put one above every answer. This is for the
    #           two edges with genuinely nothing to say: a stream that
    #           produced no message, and a routing race whose loser's notice
    #           has no thread left to narrate.
    #
    # Every one of them is best-effort and none of them raise: a thread whose
    # notice could not be posted, patched or removed still gets its answer, and
    # that is the ordering of priorities. A failed patch degrades to a fresh
    # post rather than to a lost update.

    @staticmethod
    async def set_status(
        *,
        channel: ServerChannel,
        thread_key: str,
        message_id: str | None,
        text: str,
    ) -> str | None:
        """Post or rewrite a thread's status notice. Returns its id, or None.

        **Session-free on purpose.** Everything here is transport work — an
        HTTP call and an in-memory debug record — and taking a ``Session`` it
        never used invited callers to hold a pooled connection open across it.
        ``_route_new_thread`` did exactly that: its notice went out with the
        outer transaction still open, above the ``db.commit()`` whose own
        comment forbids it. The persistence half lives in
        :meth:`set_binding_status`, which does have a row to write to.

        Thread-keyed rather than binding-keyed because the first notice of a
        new thread is posted **before** the binding exists — routing has to run
        before anything can be bound, and routing is the slow part the notice
        is narrating. The caller holds the returned id as a plain value and
        writes it onto the binding once there is one, the same way it carries
        ``policy`` across that hop.

        ``None`` comes back when the transport has no progress surface at all,
        when the notice was delivered as an ordinary message (a transport that
        can post but not edit), and when the post simply **failed** — in all
        three there is nothing for a later ``clear`` to act on, which is
        exactly what a ``None`` id means to every caller.

        Those three are not equally benign, and the caller has to tell them
        apart itself: a ``None`` from a transport that declares
        ``supports_status_notice`` is a notice that should have been posted and
        was not, which on that transport is the sender's whole acknowledgement.
        ``_route_new_thread`` warns on exactly that combination.

        **Total, and that is load-bearing.** The band comment above says none
        of these three verbs raise, and this line is what makes that true of
        this one. ``channel.channel_type`` is not a field read: every caller
        arrives after a ``db.commit()`` that expired the instance, so it is a
        lazy reload — ``ObjectDeletedError`` (row deleted concurrently),
        ``OperationalError`` (pool timeout, disconnect) and
        ``PendingRollbackError`` (a poisoned session, which is exactly the
        state the failure handlers that call this run in) are none of them
        ``ChannelError``. A raise here escapes ``set_binding_status`` and
        ``_settle_notice`` alike, and the callers it escapes into are the ones
        with no handler left: the inbound pipeline's ownership hand-off (the
        binding row keeps the id while the caller settles a stale local, so
        the flush loop patches "ready" over the sender's last word) and the
        outer failure handler itself (the notice is stranded on "Setting up…"
        forever). Same treatment, for the same hazard, as
        ``_debug_channel_key``, ``_binding_thread_key`` and
        ``_persist_status_message_id``.
        """
        try:
            adapter = get_adapter(channel.channel_type)
        except Exception:  # noqa: BLE001 — see the docstring: a lazy reload
            logger.warning(
                "%s Could not resolve the adapter for a status notice on "
                "thread %s — skipping it",
                _LOG_PREFIX,
                thread_key,
                exc_info=True,
            )
            return None
        capabilities = adapter.capabilities
        if not capabilities.supports_progress_updates:
            return None
        if not capabilities.supports_status_notice:
            # No edit/delete: fall back to a plain message and keep no id.
            await ChannelOutboundService._send_notice(channel, thread_key, text)
            return None

        if message_id:
            try:
                await adapter.update_message(channel, thread_key, message_id, text)
                ChannelOutboundService._record_notice(
                    channel, thread_key, text, "Status notice updated"
                )
                return message_id
            except Exception as exc:  # noqa: BLE001 — degrade to a fresh notice
                # The debug feed is where an operator diagnoses a channel that
                # has gone quiet, so a failed patch is recorded even though the
                # caller recovers from it — otherwise the only trace of a
                # channel whose every patch is failing is a doubled notice
                # nobody can explain.
                ChannelOutboundService._record_notice(
                    channel,
                    thread_key,
                    text,
                    f"Status notice update failed: "
                    f"{routing_trace.describe_exception(exc)} — reposting",
                    failed=True,
                )
                logger.warning(
                    "%s Could not rewrite the status notice on thread %s — "
                    "posting a new one",
                    _LOG_PREFIX,
                    thread_key,
                    exc_info=True,
                )

        return await ChannelOutboundService._send_notice(channel, thread_key, text)

    @staticmethod
    async def clear_status(
        *,
        channel: ServerChannel,
        thread_key: str,
        message_id: str | None,
    ) -> None:
        """Delete a thread's status notice. Never raises; already-gone is fine.

        **Not the normal end of a turn.** A reply takes the notice's slot
        instead — see ``_deliver(into_status_notice=True)`` — because a deleted
        Chat message leaves a "Message deleted by its author" tombstone, and
        clearing the notice after posting the reply beneath it put one of those
        above every answer. Reach for this only where the turn ends with
        nothing to put in the slot.

        Gated on ``supports_message_delete``, which is checked here rather than
        folded into ``supports_status_notice``: a transport that can edit but
        not delete runs the notice perfectly well and only loses this edge.
        """
        if not message_id:
            return
        try:
            adapter = get_adapter(channel.channel_type)
            if not adapter.capabilities.supports_message_delete:
                return
            await adapter.delete_message(channel, thread_key, message_id)
        except Exception:  # noqa: BLE001 — a stale notice is not worth a failure
            logger.warning(
                "%s Could not clear the status notice on thread %s",
                _LOG_PREFIX,
                thread_key,
                exc_info=True,
            )

    @staticmethod
    async def set_binding_status(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
        settle: bool = False,
    ) -> bool:
        """``set_status`` for a bound thread, persisting the notice id.

        ``settle=True`` writes the final word: the notice is rewritten and then
        *released* — the id is dropped so nothing later deletes it. Use it for
        text that is itself the answer, and never for a state the conversation
        moves on from.

        **The release happens only when the write actually landed.** A settle
        whose patch AND whose fallback post both failed has shown the sender
        nothing: the notice still stands with whatever it said before. Dropping
        the id there orphans that message — the next thing to write a notice on
        this thread has no id to patch, posts a fresh one, and the thread now
        carries the old text above the new. For the streaming relay it is worse
        than cosmetic: a seal that fails correctly does not advance the sealed
        offset, so the fresh message repeats the whole unsealed draft while the
        orphan stands with a prefix of the same paragraphs. Keeping the id
        instead costs nothing anywhere — the next notice patches the message
        that is already there, which is exactly the self-heal the pipeline
        already relies on — and it is what the module's stated invariant ("the
        notice id is released only when the patch really landed") always meant.

        **The same rule, and the same reason, without ``settle``.** A plain
        ``set`` whose patch and fallback post both failed used to write the
        ``None`` it got back onto the row, which is the identical orphan
        arrived at from the other side: the standing notice loses its only
        pointer. The pipeline's four ``set`` call sites — the two "working on
        your message…" patches, the "installing…" adopt-then-patch, and the
        "ready" that the drain's reply is about to take over — all want the
        stored id kept there, because each of them is followed by something
        that will patch that same message. Keeping it is also what makes the
        relay's failed *draft* patch survivable: the next flush retries the
        patch instead of posting the whole unsealed draft again underneath the
        orphan. Where there was no id, nothing changes: the write was a no-op
        either way.

        Returns **whether the text actually reached the thread**. Still total —
        the return value is the report, not an exception — and additive: every
        caller that narrates progress ignores it, because a notice that could
        not be posted is a cosmetic loss they cannot act on anyway.

        It exists for the one caller that can: the streaming relay
        (``channel_stream_relay``) *seals* a slice of the answer with this verb
        and then advances past it, never sending that text again. Advancing on
        an unconfirmed send turns a transport outage into a silent hole in the
        middle of the reply — strictly worse than today's loud total failure —
        so the relay needs to know.

        **Reading ``set_status``'s ``None`` as "it failed" is a borrowed
        certainty, so name what lends it.** That ``None`` has three meanings —
        the post failed, the transport has no progress surface at all, or the
        notice went out as an ordinary message on a transport that cannot edit.
        Only the first is a failure. The other two never occur on the paths
        that consult this return value, because ``maybe_attach_channel_relay``
        will not build a relay unless the channel's adapter declares
        ``supports_status_notice`` — the same capability ``set_status`` checks
        before it will keep an id — and the settle branch above only *keeps* an
        id a notice-less transport could never have had. Loosen that gate and
        this reading has to be revisited with it: a notice-less transport would
        then report every delivered message as a failure, and the relay would
        re-send every slice it had already put on screen.

        The one behaviour difference that reading is responsible for: on a
        transport with no progress surface at all, the guard now skips a
        ``_persist_status_message_id(db, binding, None)`` that used to run.
        It was a no-op — such a transport can never have obtained an id for
        the write to clear — and it is named here rather than left to be
        rediscovered, because it is the case a loosened capability gate would
        turn into a real one.

        The implementation is :meth:`set_binding_status_ex` with its second
        answer dropped — the same shape, and for the same reason, as
        :meth:`_take_stream_tail` over :meth:`_take_stream_tail_ex`. **The
        ``bool`` return is a contract**: the relay gates its seal advance on
        it, so this signature does not grow a tuple.
        """
        delivered, _message_id = await ChannelOutboundService.set_binding_status_ex(
            db=db, channel=channel, binding=binding, text=text, settle=settle
        )
        return delivered

    @staticmethod
    async def set_binding_status_ex(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
        settle: bool = False,
    ) -> tuple[bool, str | None]:
        """:meth:`set_binding_status`, plus the id of the message it wrote.

        Answers ``(delivered, external_message_id)``. Read
        :meth:`set_binding_status` for everything about the first value — it is
        the whole of that method's contract, unchanged, and this is where it is
        computed.

        The second value exists for the turn-delivery ledger, which records
        *which* external message now carries a sealed slice or a fresh draft.
        It is the id ``set_status`` reports, so it is the message actually
        written whichever path that took — the patched notice, or the fresh
        post a failed patch degraded to. ``None`` accompanies every
        ``False``, and is also what a transport with no progress surface
        returns; a ledger row records it as "delivered, message unknown"
        rather than inventing one.
        """
        thread_key = _binding_thread_key(binding, channel)
        if thread_key is None:
            return False, None
        message_id = await ChannelOutboundService.set_status(
            channel=channel,
            thread_key=thread_key,
            message_id=_binding_status_message_id(binding),
            text=text,
        )
        if message_id is None:
            # The write never reached the thread: leave the id alone so the
            # next turn patches the notice that is still standing, instead of
            # posting beneath it. See the docstring — this is one rule, not two
            # branches: a *settle* that failed would otherwise release an id
            # nothing had rewritten, and a *set* that failed would otherwise
            # overwrite the live id with ``None``, which orphans the same
            # message just as thoroughly. On the streaming path the second is
            # the more damaging of the two, because the very next flush finds
            # no id, posts the whole unsealed draft as a fresh message, and
            # leaves the orphan standing above it with a prefix of the same
            # paragraphs — permanently, since nothing can address it any more.
            #
            # Costs nothing where the id was already ``None``:
            # ``_persist_status_message_id`` was a no-op there too.
            return False, None
        ChannelOutboundService._persist_status_message_id(
            db, binding, None if settle else message_id
        )
        return True, message_id

    @staticmethod
    async def clear_binding_status(
        *, db: DBSession, channel: ServerChannel, binding: ChannelThreadBinding
    ) -> None:
        """Delete a bound thread's status notice and forget it.

        Read :meth:`clear_status` before adding a call site: this is the edge
        case, not the happy path.
        """
        message_id = _binding_status_message_id(binding)
        if message_id is None:
            return
        thread_key = _binding_thread_key(binding, channel)
        if thread_key is not None:
            await ChannelOutboundService.clear_status(
                channel=channel, thread_key=thread_key, message_id=message_id
            )
        ChannelOutboundService._persist_status_message_id(db, binding, None)

    @staticmethod
    def adopt_status_notice(
        db: DBSession, binding: ChannelThreadBinding, message_id: str | None
    ) -> None:
        """Hand a notice posted before the binding existed over to the binding.

        The first notice of a new thread is posted while routing runs, which is
        strictly before there is anything to bind. The id travels as a plain
        local through ``_route_new_thread`` — the same way ``policy`` and
        ``origin`` do across that hop — and lands here once the binding is
        created, so every later turn can find the notice it has to rewrite.

        Never raises; see :meth:`_persist_status_message_id`.
        """
        ChannelOutboundService._persist_status_message_id(db, binding, message_id)

    @staticmethod
    async def _send_notice(
        channel: ServerChannel, thread_key: str, text: str
    ) -> str | None:
        """Post a notice, swallowing delivery failure. Returns its id."""
        try:
            adapter = get_adapter(channel.channel_type)
            message_id = await adapter.send_message(channel, thread_key, text)
        except Exception as exc:  # noqa: BLE001 — best effort, like every notice
            ChannelOutboundService._record_notice(
                channel,
                thread_key,
                text,
                f"Notice delivery failed: {routing_trace.describe_exception(exc)}",
                failed=True,
            )
            logger.warning(
                "%s Could not post a status notice to thread %s",
                _LOG_PREFIX,
                thread_key,
                exc_info=True,
            )
            return None
        ChannelOutboundService._record_notice(
            channel, thread_key, text, "Status notice delivered"
        )
        # The id goes straight into a varchar column and is later handed back
        # to the transport as a message name, so it has to actually be one.
        # An adapter that returns anything else is a bug, and the honest
        # degradation is "this thread has no notice" — the next state posts a
        # fresh one — rather than a write that fails at commit time.
        if not isinstance(message_id, str) or not message_id:
            return None
        return message_id[:255]

    @staticmethod
    def _record_notice(
        channel: ServerChannel,
        thread_key: str,
        text: str,
        summary: str,
        *,
        failed: bool = False,
    ) -> None:
        """File a notice in the debug feed. Never raises.

        The status notice replaced ``_reply`` on several pipeline paths, and
        ``_reply`` recorded every one of them. Without this the admin panel
        would go blind exactly where the feature got chattier — a thread whose
        notice is being rewritten five times would show nothing at all.

        ``channel.id`` is read through the total helper for the reason the rest
        of this file states at length: it is an argument expression, evaluated
        before ``ChannelDebugBuffer.record``'s own guard can cover it, and on
        these paths it is a lazy reload.
        """
        from app.services.server_channels.channel_inbound_service import (
            _debug_channel_key,
        )

        debug_channel_id = _debug_channel_key(channel)
        if debug_channel_id is None:
            return
        ChannelDebugBuffer.record(
            channel_id=debug_channel_id,
            direction="outbound",
            kind=DEBUG_SEND_FAILED if failed else DEBUG_REPLIED,
            summary=summary,
            thread_key=thread_key,
            text=text,
        )

    @staticmethod
    def _persist_status_message_id(
        db: DBSession, binding: ChannelThreadBinding, message_id: str | None
    ) -> None:
        """Write the notice id onto the binding. Never raises.

        Guarded for the same reason ``_record_error`` is: the binding instance
        is expired after every commit on these paths, so both the read and the
        write are lazy reloads, and a concurrently deleted binding raises
        ``ObjectDeletedError`` out of what is only bookkeeping. A lost id costs
        one extra round trip on the next turn (the patch misses, a fresh notice
        is posted); a raise here would abort a delivery that already happened.
        """
        try:
            if binding.status_message_id == message_id:
                return
            binding.status_message_id = message_id
            db.add(binding)
            db.commit()
        except Exception:  # noqa: BLE001 — see the docstring
            logger.warning(
                "%s Could not persist the status notice id", _LOG_PREFIX, exc_info=True
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s Rollback after a failed status-id write also failed",
                    _LOG_PREFIX,
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_channel_session(
        db: DBSession, session_id: Any
    ) -> tuple[ChannelThreadBinding, ServerChannel] | None:
        """Cheap gate + binding/channel lookup for a stream event.

        Returns None for every session this feature does not own — which is
        almost all of them, so the ``integration_type`` check comes first and
        costs one already-loaded column.
        """
        try:
            session_uuid = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return None

        chat_session = db.get(ChatSession, session_uuid)
        if chat_session is None:
            return None
        integration_type = chat_session.integration_type or ""
        if not integration_type.startswith(CHANNEL_INTEGRATION_PREFIX):
            return None

        binding = db.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.session_id == session_uuid
            )
        ).first()
        if binding is None:
            logger.warning(
                "%s Session %s is a channel session with no binding",
                _LOG_PREFIX,
                session_id,
            )
            return None

        channel = db.get(ServerChannel, binding.server_channel_id)
        if channel is None or not channel.enabled:
            return None
        return binding, channel

    @staticmethod
    def _last_agent_message(db: DBSession, session_id: uuid.UUID) -> str | None:
        """The newest agent message in the session. **Never turn attribution.**

        A session outlives a turn, so "newest agent row" answers a question no
        caller here is actually asking. Resolving a completing turn's reply
        through it re-delivers the *previous* turn's answer whenever this turn
        produced no agent message — a tool-only batch, an empty model output, a
        command stream — which is the bug turn identity in the event meta was
        added to close. Deliver by :attr:`AGENT_MESSAGE_ID_META_KEY` via
        :func:`_agent_message_text` instead.

        It survives for exactly one caller: the backward-compatibility arm in
        :meth:`handle_stream_completed`, for events emitted by code predating
        that meta key (a rolling deploy, a stale test fixture). That arm is the
        only place the old query is still correct-by-default, because such an
        event carries nothing better. Do not add a second caller.
        """
        row = db.exec(
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "agent",
            )
            .order_by(SessionMessage.sequence_number.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return (row.content or "").strip() or None

    @staticmethod
    async def _deliver(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
        into_status_notice: bool = False,
    ) -> bool:
        """Whether :meth:`_deliver_ex` sent this text. See it for everything.

        The ``bool`` form is what every delivery call site wants, and it stays
        the signature so none of them have to unpack a tuple they would
        immediately discard — the same split as
        :meth:`set_binding_status` over :meth:`set_binding_status_ex`.
        """
        sent, _message_id = await ChannelOutboundService._deliver_ex(
            db=db,
            channel=channel,
            binding=binding,
            text=text,
            into_status_notice=into_status_notice,
        )
        return sent

    @staticmethod
    async def _deliver_ex(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        text: str,
        into_status_notice: bool = False,
    ) -> tuple[bool, str | None]:
        """Send through the adapter, recording failure on the binding.

        Answers ``(sent, external_message_id)``. The id is the transport's own
        name for the message this call wrote — the notice it patched, or the
        message it posted — and is carried for the turn-delivery ledger, which
        records which external message holds the turn's final text. ``None``
        where the adapter did not name one; the ledger stores that honestly
        rather than guessing.

        ``into_status_notice`` delivers **into** the thread's open status
        notice — the message that has been narrating this turn is rewritten to
        hold the answer — instead of posting a new one below it.

        That is the fix for what the delete-based version looked like in
        practice: Chat renders a deleted message as "Message deleted by its
        author", so clearing the notice after posting the reply put a tombstone
        above every single answer. Reusing the slot means the notice was never
        a separate message from the reader's point of view; it was the reply,
        arriving in stages.

        The id is released **when, and only when, the notice was really
        taken over**. It has to be: the message now holds the agent's answer,
        and the next turn's "working on your message…" would otherwise be
        patched straight over it.

        There are two ways not to take it over, and they used to be one.
        Delivery raising is the obvious one — the id stays, so the notice is
        still there to be rewritten or retried rather than orphaned
        mid-sentence. The other is invisible from here unless the adapter says
        so: ``replace_message`` degrades to an ordinary post when the patch
        fails, so a stale id (message hand-deleted, scope lost, id from before
        a redeploy) produced a *successful* delivery, a debug line claiming the
        reply had gone into the notice, and a released id — leaving "💬 Working
        on your message…" standing above the answer with nothing owning it and
        nothing able to rewrite it again. ``ChannelReplaceResult.replaced``
        makes that case visible, and the answer is to **keep** the id: the next
        turn patches that same message back to "working…" and self-heals, so a
        permanently broken patch costs one stale notice rather than one per
        turn. Deleting the orphan instead is not an option — that is the
        tombstone this whole mechanism exists to avoid.

        §11a Rule 2, and the worse twin of ``ChannelInboundService._reply``:
        this is the **agent-reply** path, so it carries the traffic the notice
        path does not. Five expressions were exposed inside the one ``except``
        — ``channel.id`` twice, ``binding.thread_key`` twice, and the
        ``f"...{exc}"`` summary, plus ``str(exc)`` in the ``_record_error``
        call and a lazily-interpolated ``exc`` in the log. Python evaluates
        every one of them *before* entering the callee, so neither
        ``ChannelDebugBuffer.record``'s never-raises guard nor
        ``_record_error``'s reached any of them. A raise in any one replaced
        the delivery exception, skipped the remaining statements, and left the
        failure invisible in the debug buffer **and** on the binding row — the
        two places an operator looks. Confirmed by firing poison objects, with
        ``logging`` disabled so the result is production behaviour and not
        pytest's re-raising capture handler.

        The success branch was exposed too, and had no ``try`` over it at all:
        ``channel_id=channel.id`` on a delivery that had already *succeeded*
        raised out of ``_deliver`` and turned a delivered reply into an error
        for the caller.

        Both reads are hoisted through total helpers and resolved once. The
        exception is rendered twice on purpose, by audience:
        ``describe_exception`` for the debug buffer, which is a superuser read
        surface an adapter's credential-echoing HTTP error must not reach, and
        ``_log_detail`` for the application log and the binding column, where
        the adapter's actual complaint is the whole diagnosis. Neither can
        raise; ``f"{exc}"`` and ``str(exc)`` both can.
        """
        from app.services.server_channels.channel_inbound_service import (
            _debug_channel_key,
            _log_detail,
        )

        # Imported inside the function, not at module scope:
        # ``channel_inbound_service`` imports *this* module at import time, so
        # the reverse edge would be circular. Resolved here rather than in the
        # ``except`` for the same reason the reads are hoisted — an
        # ``ImportError`` raised inside the handler would destroy the exception
        # just as surely as an attribute reload.
        debug_channel_id = _debug_channel_key(channel)
        thread_key = _binding_thread_key(binding, channel)
        if thread_key is None:
            # Nothing to address the message to. Sending anyway would post to a
            # null thread; the warning is already logged by the helper.
            return False, None
        # Hoisted with the rest, and for the same reason: this is a lazy reload
        # on an expired instance, and it is about to be read inside a `try`
        # whose `except` may not raise anything of its own.
        notice_id = (
            _binding_status_message_id(binding) if into_status_notice else None
        )
        # Whether the notice was actually taken over. Bound before the ``try``
        # so the failure path below can read it without a NameError, and only
        # ever set from the adapter's own report.
        replaced = False
        # The transport's name for the message this call wrote, for the ledger.
        # Bound before the ``try`` for the same reason ``replaced`` is.
        written_id: Any = None
        try:
            adapter = get_adapter(channel.channel_type)
            if notice_id:
                outcome = await adapter.replace_message(
                    channel, thread_key, notice_id, text
                )
                # Frozen-dataclass attributes, so these reads cannot raise
                # (§11a Rule 2) — unlike everything else being hoisted here.
                replaced = outcome.replaced
                written_id = outcome.message_id
            else:
                written_id = await adapter.send_message(channel, thread_key, text)
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            failure = routing_trace.describe_exception(exc)
            detail = _log_detail(exc)
            logger.warning(
                "%s Delivery failed for channel=%s thread=%s: %s",
                _LOG_PREFIX,
                # The hoisted values: this argument list is evaluated eagerly
                # too, so an inline ``channel.id`` here would destroy the
                # original exception just as surely as the one below. And
                # ``detail``, not ``exc``: ``logging`` interpolates lazily and
                # swallows its own formatting errors in production while
                # pytest's ``LogCaptureHandler`` re-raises them, so a raw
                # ``exc`` here is a guard whose correctness depends on which
                # handler is installed.
                debug_channel_id or "unknown",
                thread_key,
                detail,
            )
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="outbound",
                    kind=DEBUG_SEND_FAILED,
                    summary=f"Delivery failed: {failure}",
                    thread_key=thread_key,
                    text=text,
                )
            ChannelOutboundService._record_error(db, binding, detail)
            return False, None
        if debug_channel_id is not None:
            ChannelDebugBuffer.record(
                channel_id=debug_channel_id,
                direction="outbound",
                kind=DEBUG_REPLIED,
                summary=(
                    "Agent reply delivered into the status notice"
                    if replaced
                    # The notice could not be patched, so the reply was posted
                    # under it instead. Said out loud because the debug feed is
                    # where an operator diagnoses a channel whose every patch
                    # is failing, and the old wording asserted the opposite.
                    else "Agent reply delivered (status notice patch failed)"
                    if notice_id
                    else "Agent reply delivered"
                ),
                thread_key=thread_key,
                text=text,
            )
        if notice_id and replaced:
            # See the docstring: the slot now holds the answer, so nothing may
            # rewrite it again. Deliberately NOT released when the adapter fell
            # back to a plain post — that notice is still standing, and keeping
            # the id is what lets the next turn rewrite it instead of stranding
            # a new orphan every turn.
            ChannelOutboundService._persist_status_message_id(db, binding, None)
        # Normalised the same way ``_send_notice`` normalises its own: the id
        # goes into a varchar column, so an adapter that answered with
        # something other than a non-empty string is recorded as "delivered,
        # message unknown" rather than as a value nothing can use.
        external_id = (
            written_id[:255]
            if isinstance(written_id, str) and written_id
            else None
        )
        return True, external_id

    @staticmethod
    def _record_error(
        db: DBSession, binding: ChannelThreadBinding, error: str
    ) -> None:
        """Record a delivery failure — but never over a diagnosis. Never raises.

        A binding that already failed carries WHY it failed, which is far more
        useful than "and we also couldn't tell them about it". The delivery
        failure is still logged by the caller.

        **It could raise, and it is called from inside an ``except``** — so a
        raise here replaced the delivery exception exactly like an unguarded
        argument expression would, and hoisting ``_deliver``'s arguments alone
        would not have stopped it. Two paths, both confirmed by firing:

        1. ``binding.status`` and ``binding.last_error`` were read *above* the
           ``try``. They are the same expired-instance lazy reload
           :func:`_binding_thread_key` exists for, and this call site is
           reached only after a delivery has already failed — which is
           precisely when a concurrently-torn-down binding is plausible.
        2. ``db.rollback()`` sat unguarded inside the handler. A session
           rolled back into an unusable state raises again from the very call
           meant to clean it up.

        **What this does not fix, and must not be read as fixing:** when the
        ``commit`` genuinely fails, the rollback discards ``last_error`` and
        the binding row keeps no record of the delivery failure. Guarding the
        write makes the failure *reportable*; it cannot make it *durable*.
        Durability needs the persistent outbound queue named in the module
        docstring, which is a listed future enhancement — until then the
        application log is the only surviving copy, which is why the caller
        logs before it calls this.
        """
        from app.models import CHANNEL_BINDING_FAILED

        try:
            if binding.status == CHANNEL_BINDING_FAILED and binding.last_error:
                return
            binding.last_error = error[:2000]
            db.add(binding)
            db.commit()
        except Exception:
            logger.exception("%s Could not record delivery error", _LOG_PREFIX)
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "%s Rollback after a failed error-record also failed",
                    _LOG_PREFIX,
                )


__all__ = [
    "ChannelOutboundService",
    "CHANNEL_INTEGRATION_PREFIX",
    "STOPPED_NOTICE",
    "STOPPED_SUFFIX",
    "TURN_FAILED_TEXT",
]
