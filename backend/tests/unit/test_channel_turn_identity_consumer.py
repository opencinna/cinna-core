"""``handle_stream_completed``'s branch matrix on the event's turn identity.

The completion handler used to resolve its text with ``_last_agent_message``
— the newest ``role="agent"`` row in the session — which is a question about
the session and not about the turn. The terminal stream events now carry
``agent_message_id``, and **three states of that one key are three different
instructions**. They are easy to collapse by accident and expensive to get
wrong, so each is pinned here separately, along with a fourth outcome that is
not a state of the key at all but of the lookup.

| meta                        | what the handler must do                     |
|-----------------------------|----------------------------------------------|
| a uuid naming a row         | deliver **that** row's text                   |
| a uuid whose row is gone    | nothing to deliver — settle the notice        |
| an explicit ``None``        | the turn wrote nothing — settle the notice,   |
|                             | and **never** query the newest row            |
| the key absent entirely     | legacy event: the newest-row query survives   |
| the read itself **raised**  | leave the thread untouched — settle nothing   |

The last row is the one a review caught as a regression and is the reason
``_UNREADABLE`` is a typed sentinel rather than folded into ``None``: a
transient failure reading the row would otherwise reach
``clear_binding_status`` and **delete** the status notice, which is where a
broken relay's partial answer is standing — the reader's only copy of text
that exists in ``SessionMessage`` and would never be sent again.

The two middle rows are the whole point of ``_MISSING``: ``meta.get(key)``
cannot tell "absent" from "present and ``None``", and those two are opposite
instructions. A test suite that only covered one of them would be green on a
handler that had collapsed them.

Pure logic with fakes: no DB, no ``TestClient``, no HTTP. The API-observable
end of this — a real command turn on a real thread not being answered with the
previous turn's text — is
``tests/api/server_channels/server_channels_turn_identity_test.py``.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

from app.services.server_channels.channel_outbound_service import (
    ChannelOutboundService,
)

_MODULE = "app.services.server_channels.channel_outbound_service"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.channel_type = "google_chat"
        self.enabled = True


class _FakeBinding:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status_message_id = "spaces/AAA/messages/notice"


class _FakeRow:
    def __init__(
        self, content: str | None, message_metadata: dict | None = None
    ) -> None:
        self.content = content
        # Read by ``tool_only_summary_for_message`` on the uuid arm; ``None``
        # is what a row without stored events answers, and keeps content
        # delivery in charge.
        self.message_metadata = message_metadata


class _FakeDB:
    """A session whose only job is answering ``get(SessionMessage, id)``.

    ``raises`` is the ``_UNREADABLE`` arm: a transient failure on the lookup,
    which is a different fact from "the row is not there" and must produce a
    different outcome.
    """

    def __init__(self, row: Any = None, *, raises: bool = False) -> None:
        self.row = row
        self.raises = raises
        self.get_calls: list[Any] = []

    def get(self, _model: Any, obj_id: Any) -> Any:
        self.get_calls.append(obj_id)
        if self.raises:
            raise RuntimeError("connection reset while reading the agent message")
        return self.row


class _Harness:
    """Everything the handler reaches, captured.

    Every seam below the branch under test is mocked so a failure names the
    branch and not its neighbourhood: the relay registry is genuinely empty
    (no relay for this session, which is the arm turn identity governs), the
    ledger is a recorder, and the two ways text can reach a thread —
    ``_deliver_ex`` and ``clear_binding_status`` — are the observable pair.
    """

    def __init__(self, db: _FakeDB) -> None:
        self.db = db
        self.binding = _FakeBinding()
        self.channel = _FakeChannel()
        self.delivered: list[str] = []
        self.cleared = 0
        self.legacy_calls = 0
        self.settled: list[dict[str, Any]] = []

    @contextmanager
    def install(self):
        async def _deliver_ex(*, text: str, **_kw: Any):
            self.delivered.append(text)
            return True, "spaces/AAA/messages/written"

        async def _clear(**_kw: Any) -> None:
            self.cleared += 1

        def _legacy(*_a: Any, **_kw: Any) -> str:
            self.legacy_calls += 1
            return "LEGACY: the newest agent row in this session"

        def _settle_turn(_db: Any, **kwargs: Any) -> None:
            self.settled.append(kwargs)

        @contextmanager
        def _session():
            yield self.db

        ledger = mock.MagicMock()
        ledger.turn_already_settled.return_value = False
        ledger.settle_turn.side_effect = _settle_turn

        with ExitStack() as stack:
            stack.enter_context(mock.patch("app.core.db.create_session", _session))
            stack.enter_context(
                mock.patch.object(
                    ChannelOutboundService,
                    "_resolve_channel_session",
                    return_value=(self.binding, self.channel),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    ChannelOutboundService, "_deliver_ex", side_effect=_deliver_ex
                )
            )
            stack.enter_context(
                mock.patch.object(
                    ChannelOutboundService, "clear_binding_status", side_effect=_clear
                )
            )
            stack.enter_context(
                mock.patch.object(
                    ChannelOutboundService, "_last_agent_message", side_effect=_legacy
                )
            )
            stack.enter_context(
                mock.patch(f"{_MODULE}.ChannelTurnDeliveryLedger", ledger)
            )
            self.ledger = ledger
            yield self


def _complete(meta: dict[str, Any]) -> None:
    asyncio.run(ChannelOutboundService.handle_stream_completed({"meta": meta}))


def _base_meta() -> dict[str, Any]:
    return {"session_id": str(uuid.uuid4())}


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_a_named_row_is_the_text_that_is_delivered() -> None:
    """A uuid in the meta → that row's content, and no newest-row query.

    The positive case, and the one that makes every negative below meaningful:
    the handler is wired up correctly and does deliver when there is something
    to deliver.
    """
    message_id = uuid.uuid4()
    db = _FakeDB(row=_FakeRow("  This turn's own answer.  "))
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(message_id)})

    # Whitespace-stripped, exactly as the canonical answer is stored.
    assert h.delivered == ["This turn's own answer."], h.delivered
    assert h.cleared == 0
    assert h.legacy_calls == 0, "the newest-row query must not be reachable here"
    # Two reads of the SAME row, both by the id the event named: the content
    # (``_agent_message_text``) and the stored events, which decide whether a
    # tool-only turn substitutes its compact summary
    # (``tool_only_summary_for_message``). Any other id appearing here is the
    # newest-row bug coming back.
    assert db.get_calls == [message_id, message_id], db.get_calls
    # And the turn is closed in the ledger with the id the event named.
    assert h.settled and h.settled[-1]["session_message_id"] == message_id
    assert h.settled[-1]["delivered"] is True


def test_a_tool_only_row_delivers_the_compact_summary_not_the_placeholder() -> None:
    """Tool-only turn → the compact tool summary, never ``"Agent response"``.

    The row is exactly what ``message_service`` finalizes for such a batch:
    the placeholder as content, the tool events in ``streaming_events``. The
    decision is made on the events, not on the placeholder literal — see
    ``channel_tool_summary``; what the summary *says* is unit-tested in
    ``test_channel_tool_summary.py``, so this asserts substitution, not
    wording.
    """
    message_id = uuid.uuid4()
    db = _FakeDB(
        row=_FakeRow(
            "Agent response",
            message_metadata={
                "streaming_events": [
                    {
                        "type": "tool",
                        "tool_name": "read",
                        "metadata": {
                            "tool_input": {"file_path": "/app/workspace/a.md"}
                        },
                    }
                ]
            },
        )
    )
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(message_id)})

    assert h.delivered == ["```\nRead file: a.md\n```"], h.delivered
    assert h.cleared == 0
    assert h.settled and h.settled[-1]["delivered"] is True


def test_an_empty_row_with_tool_events_still_reads_as_a_silent_turn() -> None:
    """The summary never converts "turn said nothing" into a delivery.

    An empty-content row WITH stored tool events is reachable — a turn whose
    whole output was an attachment tag has the tag stripped *after* the
    placeholder fallback ran — and the handler's documented contract is that
    an empty row reads as "the turn said nothing". The summary hook is gated
    on ``text`` so this shape keeps clearing the notice; only the truthy
    placeholder shape substitutes.
    """
    message_id = uuid.uuid4()
    db = _FakeDB(
        row=_FakeRow(
            "",
            message_metadata={
                "streaming_events": [
                    {
                        "type": "tool",
                        "tool_name": "read",
                        "metadata": {
                            "tool_input": {"file_path": "/app/workspace/a.md"}
                        },
                    }
                ]
            },
        )
    )
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(message_id)})

    assert h.delivered == [], h.delivered
    assert h.cleared == 1


def test_a_row_with_prose_delivers_the_prose_even_beside_tool_events() -> None:
    """A turn that said anything delivers what it said — no summary."""
    message_id = uuid.uuid4()
    db = _FakeDB(
        row=_FakeRow(
            "The real answer.",
            message_metadata={
                "streaming_events": [
                    {
                        "type": "tool",
                        "tool_name": "read",
                        "metadata": {
                            "tool_input": {"file_path": "/app/workspace/a.md"}
                        },
                    },
                    {"type": "assistant", "content": "The real answer."},
                ]
            },
        )
    )
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(message_id)})

    assert h.delivered == ["The real answer."], h.delivered


def test_a_named_row_that_has_gone_settles_the_notice() -> None:
    """The id names a row that is no longer there → nothing to deliver.

    Folded together with an empty row and an unparseable id on purpose: each
    is a fact the handler actually established, and each means the same thing
    to the reader. What it must *not* do is fall back to the newest row.
    """
    db = _FakeDB(row=None)
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(uuid.uuid4())})

    assert h.delivered == []
    assert h.cleared == 1
    assert h.legacy_calls == 0


def test_an_empty_row_reads_as_a_turn_that_said_nothing() -> None:
    """Whitespace-only content is not a reply. Same outcome, different cause."""
    db = _FakeDB(row=_FakeRow("   \n  "))
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(uuid.uuid4())})

    assert h.delivered == []
    assert h.cleared == 1
    assert h.legacy_calls == 0


def test_an_explicit_none_never_falls_back_to_the_newest_row() -> None:
    """**The bug, in one assertion.**

    An emitter that says ``agent_message_id=None`` is stating on the record
    that this batch wrote no agent message — a command stream, a batch with no
    storable events. Asking "well, what is the newest agent row then?" is
    exactly how the previous turn's answer got re-delivered into the thread as
    if it answered the new question.

    The row is *present* in the fake session, so a handler that took the
    legacy path would have something to deliver and this test would see it.
    """
    db = _FakeDB(row=_FakeRow("the PREVIOUS turn's answer"))
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": None})

    assert h.delivered == [], h.delivered
    assert h.legacy_calls == 0, "an explicit None must never reach the newest-row query"
    assert db.get_calls == [], "and must not read a row at all"
    assert h.cleared == 1


def test_an_absent_key_keeps_the_legacy_newest_row_behaviour() -> None:
    """The backward-compatibility arm — and the reason ``_MISSING`` exists.

    An event emitted by code predating turn identity carries no key at all and
    cannot say what its turn wrote, so the honest fallback is the behaviour it
    was written against. ``meta.get(key)`` would report this as ``None`` and
    send it down the branch above; the two are opposite instructions, and this
    test paired with the previous one is what keeps them apart.
    """
    db = _FakeDB(row=_FakeRow("unused — the legacy arm does not read a row"))
    with _Harness(db).install() as h:
        _complete(_base_meta())

    assert h.legacy_calls == 1
    assert h.delivered == ["LEGACY: the newest agent row in this session"]
    assert h.cleared == 0


def test_a_failed_read_leaves_the_thread_exactly_as_it_was() -> None:
    """``_UNREADABLE`` — the fourth outcome, and a blocking review finding.

    The lookup raising is **not** "the turn said nothing". Treating it as such
    reaches ``clear_binding_status`` and deletes the status notice — which is
    where a broken relay's partial answer is standing, the reader's only copy
    of text that exists in ``SessionMessage`` and will never be sent again.
    Before turn identity such a raise propagated to the handler's outer
    ``except`` and left the notice alone, so folding it into ``None`` would be
    a regression against the code this feature replaced.

    Neither verb may fire. The ledger still gets its attribution, because that
    is not a statement about the thread — but with ``write_final=False``,
    because nothing was delivered.
    """
    db = _FakeDB(raises=True)
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": str(uuid.uuid4())})

    assert h.delivered == [], "nothing may be sent on a fact we failed to establish"
    assert h.cleared == 0, "and the notice may not be deleted either"
    assert h.settled and h.settled[-1]["write_final"] is False
    assert h.settled[-1]["delivered"] is False


def test_an_unparseable_id_is_a_turn_that_said_nothing_not_a_crash() -> None:
    """The meta crosses a process boundary as JSON, so "it is a uuid" is an
    expectation and not a guarantee. A value that cannot name a row is a
    genuine ``None`` — we read the event fine; what it named cannot exist —
    and must not raise into the bus or reach the newest-row query.
    """
    db = _FakeDB(row=_FakeRow("the PREVIOUS turn's answer"))
    with _Harness(db).install() as h:
        _complete({**_base_meta(), "agent_message_id": "not-a-uuid"})

    assert h.delivered == []
    assert h.legacy_calls == 0
    assert h.cleared == 1


def test_an_already_settled_turn_is_not_delivered_twice() -> None:
    """The ledger's idempotency gate, seen from the handler.

    A duplicate ``STREAM_COMPLETED`` for a batch whose ``final`` row already
    reached the thread must return before anything is sent — and before the
    notice is touched in either direction.
    """
    message_id = uuid.uuid4()
    db = _FakeDB(row=_FakeRow("an answer that is already standing in the thread"))
    with _Harness(db).install() as h:
        h.ledger.turn_already_settled.return_value = True
        _complete({**_base_meta(), "agent_message_id": str(message_id)})

    assert h.delivered == [] and h.cleared == 0
    assert h.settled == [], "a duplicate must not re-settle the turn either"
    # Keyed on the turn AND the thread, not on the least that happens to work.
    args = h.ledger.turn_already_settled.call_args
    assert args.args[1] == message_id
    assert args.args[2] == h.binding.id


def test_an_interrupted_completion_is_not_a_completion() -> None:
    """``was_interrupted`` short-circuits above everything else.

    ``STREAM_INTERRUPTED`` is the event that owns an interrupted turn, and its
    handler's five branches are the previous feature's regression guards. A
    completion carrying the flag must not reach any of the branches above and
    must not settle the turn behind that handler's back.
    """
    db = _FakeDB(row=_FakeRow("half an answer"))
    with _Harness(db).install() as h:
        _complete(
            {
                **_base_meta(),
                "was_interrupted": True,
                "agent_message_id": str(uuid.uuid4()),
            }
        )

    assert h.delivered == [] and h.cleared == 0
    assert h.settled == []
    assert db.get_calls == []
