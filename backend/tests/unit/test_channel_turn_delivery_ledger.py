"""``ChannelTurnDeliveryLedger`` — the state machine, and its totality.

The ledger records what a channel turn actually put on a reader's screen: one
row per external message, written only at boundaries (a fresh draft, a seal,
the final delivery), never at the rolling draft patch. Two properties decide
whether it is safe to have at all, and both are asserted here:

* **The state machine.** ``draft`` → ``sealed`` (updated *in place*, because
  the draft and the message it sealed are the same external message) →
  ``final``. Attribution happens once, at completion, from the id the stream
  event carried — never from a lookup — and renumbers the rows it adopts so
  ``part_index`` is dense and the ``(session_message_id, part_index)`` unique
  constraint is satisfiable even when a previous turn left rows behind.
* **Totality.** Every entry point is called *after* the message has already
  gone out, from a relay that is a passenger on somebody else's turn and from
  event subscribers whose discipline forbids raising on the delivery path. A
  ledger write that raised would take down bookkeeping about a delivery that
  already happened — or the delivery's own handler. So every one of them
  swallows, logs, hands the caller's session back in a usable state, and
  returns a value meaning "no ledger".

Pure logic against a fake session, per ``tests/unit/README.md``: this module
never opens a session, never holds one and never keeps an ORM instance past
its own call, which is exactly what makes it unit-testable without a
database. The same machine driven over a real Postgres by real turns — a
sealing relay's rows, a stopped turn's close-out, the retry after a failed
final delivery — is
``tests/api/server_channels/server_channels_turn_identity_test.py``.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.models import ChannelTurnDelivery
from app.services.server_channels.channel_turn_delivery_service import (
    ChannelTurnDeliveryLedger,
    delivered_prefix_key,
    visible_digest,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeDB:
    """A session that answers from canned results and records what it was told.

    ``exec`` hands back the next queued result; ``get`` answers from a small
    id-keyed store. Either can be made to raise, which is how the totality
    half below is driven — a real ``OperationalError`` is not reproducible in
    a unit test, and what the guards promise is about *any* exception.
    """

    def __init__(
        self,
        *,
        rows: dict[uuid.UUID, Any] | None = None,
        results: list[list[Any]] | None = None,
    ) -> None:
        self._rows = rows or {}
        self._results = list(results or [])
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.raise_on_get = False
        self.raise_on_exec = False
        self.raise_on_commit = False

    def get(self, _model: Any, obj_id: Any) -> Any:
        if self.raise_on_get:
            raise RuntimeError("the connection went away mid-SELECT")
        return self._rows.get(obj_id)

    def exec(self, _statement: Any) -> _Result:
        if self.raise_on_exec:
            raise RuntimeError("the connection went away mid-SELECT")
        return _Result(self._results.pop(0) if self._results else [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        if self.raise_on_commit:
            raise RuntimeError("the transaction is in a failed state")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _row(role: str, part_index: int, **kw: Any) -> ChannelTurnDelivery:
    return ChannelTurnDelivery(
        binding_id=kw.pop("binding_id", uuid.uuid4()),
        role=role,
        part_index=part_index,
        **kw,
    )


# ---------------------------------------------------------------------------
# The two pure helpers
# ---------------------------------------------------------------------------


def test_the_recorded_prefix_is_measured_the_way_the_canonical_text_is() -> None:
    """``delivered_prefix_key`` normalises the one end the other side drops.

    The two halves of the divergence check are computed in different places
    from differently-shaped strings: the relay's buffer is the agent's raw
    output, while the stored answer is ``.strip()``ed at finalize and again on
    the way out. Only the **leading** whitespace is common to both, so only
    that is stripped here — and a reply that opens with a blank line would
    otherwise shift every recorded offset and make the check fire on a turn
    where nothing diverged at all.

    Interior and trailing whitespace inside a *prefix* must survive: a seal
    cuts at a paragraph break, so its trailing newlines sit in the middle of
    the finished answer, and stripping them would break the comparison rather
    than fix it. Both directions are asserted, and then the round trip the
    check actually performs is run end to end.
    """
    end, digest = delivered_prefix_key("\n\n  First paragraph.\n\n")
    assert end == len("First paragraph.\n\n")
    assert digest == visible_digest("First paragraph.\n\n")
    assert digest != visible_digest("First paragraph.")

    # The round trip: a reply that opens with whitespace, sealed after its
    # first paragraph, still matches the canonical answer's own prefix.
    raw = "\n\nFirst paragraph.\n\nSecond paragraph."
    canonical = raw.strip()
    end, digest = delivered_prefix_key(raw[: raw.index("Second")])
    assert len(canonical) >= end
    assert visible_digest(canonical[:end]) == digest

    # And a genuine divergence does not match.
    assert visible_digest("Something else entirely"[:end]) != digest


def test_the_digest_is_a_plain_sha256_of_the_visible_text() -> None:
    """Pinned as a value, not as "whatever the function returns".

    Both halves of the check call this, so a self-consistent change would keep
    every comparison passing while silently changing what is stored — and the
    column is a diagnostic a person reads. ``""`` for the empty string is the
    honest hash, not a sentinel.
    """
    assert visible_digest("hello") == hashlib.sha256(b"hello").hexdigest()
    assert visible_digest("") == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# draft → sealed → final
# ---------------------------------------------------------------------------


def test_a_fresh_draft_is_recorded_once_and_hands_its_id_back() -> None:
    """One row per external message, and the id the relay keeps hold of.

    The relay stores that id and passes it to the next seal, which is what
    makes the seal an update rather than an insert. Losing it is not cosmetic:
    the relay would see "no row" on every 3-second patch of the same standing
    message and open a new one each time, which is the one-row-per-flush shape
    this table's grain forbids. So the id is read before the commit — after
    it, the instance is expired and reading it is a refresh SELECT that can
    fail on a row which committed perfectly well.

    The offsets stay NULL on a draft, deliberately: its content is still
    moving, and anything stored now would be stale before the next flush.
    """
    db = _FakeDB()
    binding_id = uuid.uuid4()

    row_id = ChannelTurnDeliveryLedger.record_draft(
        db, binding_id=binding_id, part_index=0,
        external_message_id="spaces/AAA/messages/m1",
    )

    assert row_id is not None
    assert db.commits == 1
    (row,) = db.added
    assert row.id == row_id
    assert (row.role, row.status, row.part_index) == ("draft", "delivered", 0)
    assert row.binding_id == binding_id
    assert row.external_message_id == "spaces/AAA/messages/m1"
    assert row.visible_char_end is None and row.content_sha256 is None
    # Written at a boundary, before the emitter has named the turn.
    assert row.session_message_id is None


def test_a_seal_settles_the_draft_row_in_place() -> None:
    """The ``draft`` row **becomes** the ``sealed`` row — it is not superseded.

    They are the same external message. Inserting a second row would leave the
    draft standing in the ledger as a message that is no longer a draft and no
    longer anywhere, and would double-count the turn's parts.
    """
    draft = _row("draft", 0, external_message_id="spaces/AAA/messages/m1")
    db = _FakeDB(rows={draft.id: draft})

    ChannelTurnDeliveryLedger.record_seal(
        db,
        binding_id=draft.binding_id,
        row_id=draft.id,
        part_index=0,
        external_message_id="spaces/AAA/messages/m1",
        visible_char_end=87,
        content_sha256=visible_digest("First paragraph.\n\n"),
    )

    assert db.added == [draft], db.added
    assert (draft.role, draft.status) == ("sealed", "delivered")
    assert draft.visible_char_end == 87
    assert draft.content_sha256 == visible_digest("First paragraph.\n\n")
    assert db.commits == 1


def test_a_seal_with_no_draft_row_inserts_one() -> None:
    """``row_id=None`` is a normal state, not an error.

    It happens when a flush seals before any draft patch has landed, when the
    previous batch's hand-over released the row, and when the draft row's own
    write could not commit. In all three the seal still describes a message
    standing in the thread, so it is recorded.
    """
    db = _FakeDB()
    binding_id = uuid.uuid4()

    ChannelTurnDeliveryLedger.record_seal(
        db,
        binding_id=binding_id,
        row_id=None,
        part_index=2,
        external_message_id="spaces/AAA/messages/m3",
        visible_char_end=120,
        content_sha256="deadbeef",
    )

    (row,) = db.added
    assert (row.role, row.part_index, row.status) == ("sealed", 2, "delivered")
    assert row.binding_id == binding_id


def test_a_failed_seal_is_flagged_without_losing_the_draft_role() -> None:
    """``mark_failed`` records the failure and nothing else.

    The message the row names is still standing and still being rewritten, so
    it keeps its ``draft`` role; the relay does not advance past a seal it
    could not send, retries it on the next flush, and a successful retry flips
    the row to ``sealed``/``delivered``. ``None`` and a row that has gone are
    both no-ops — there is nothing to flag and nothing worth failing over.
    """
    draft = _row("draft", 0)
    db = _FakeDB(rows={draft.id: draft})

    ChannelTurnDeliveryLedger.mark_failed(db, None)
    assert db.added == [] and db.commits == 0

    ChannelTurnDeliveryLedger.mark_failed(db, uuid.uuid4())
    assert db.added == [] and db.commits == 0

    ChannelTurnDeliveryLedger.mark_failed(db, draft.id)
    assert db.added == [draft]
    assert (draft.role, draft.status) == ("draft", "failed")


# ---------------------------------------------------------------------------
# Attribution at completion
# ---------------------------------------------------------------------------


def test_completion_adopts_the_turns_rows_and_settles_the_last_draft() -> None:
    """The one place ``session_message_id`` is ever filled in.

    A boundary write records what it honestly knows and leaves attribution
    NULL, because the id only exists once the batch's terminal event is
    emitted. Completion adopts every pending row on the binding, renumbers
    them densely in delivery order, and turns the last standing ``draft`` into
    the ``final`` row — the message the final text went into, rather than a
    fresh row superseding it.

    The final row records the **whole** canonical answer, not its own
    increment: it is the value a sealed prefix is checked against.
    """
    binding_id = uuid.uuid4()
    message_id = uuid.uuid4()
    sealed = _row("sealed", 0, binding_id=binding_id, visible_char_end=18,
                  content_sha256=visible_digest("First paragraph.\n\n"))
    draft = _row("draft", 1, binding_id=binding_id)
    canonical = "First paragraph.\n\nSecond paragraph."

    db = _FakeDB(results=[[sealed, draft], [], [sealed]])
    ChannelTurnDeliveryLedger.settle_turn(
        db,
        binding_id=binding_id,
        session_message_id=message_id,
        external_message_id="spaces/AAA/messages/m2",
        delivered=True,
        canonical_text=canonical,
    )

    assert sealed.session_message_id == message_id
    assert draft.session_message_id == message_id
    assert (sealed.part_index, draft.part_index) == (0, 1)
    # The draft became the turn's final row rather than being superseded.
    assert draft.role == "final" and draft.status == "delivered"
    assert draft.external_message_id == "spaces/AAA/messages/m2"
    assert draft.visible_char_end == len(canonical)
    assert draft.content_sha256 == visible_digest(canonical)
    # The sealed prefix still matches, so nothing is marked.
    assert sealed.status == "delivered"


def test_a_turn_with_no_rows_at_all_still_gets_its_final_row() -> None:
    """No relay, nothing pending — the completion writes the one row itself."""
    binding_id = uuid.uuid4()
    message_id = uuid.uuid4()
    db = _FakeDB(results=[[], [], []])

    ChannelTurnDeliveryLedger.settle_turn(
        db,
        binding_id=binding_id,
        session_message_id=message_id,
        external_message_id="spaces/AAA/messages/m1",
        delivered=True,
        canonical_text="A short answer.",
    )

    (row,) = db.added
    assert (row.role, row.part_index, row.status) == ("final", 0, "delivered")
    assert row.session_message_id == message_id
    assert row.visible_char_end == len("A short answer.")


def test_write_final_false_attributes_without_claiming_a_delivery() -> None:
    """The close-out shape, and why it can never gate a later completion.

    Two arms of ``handle_stream_completed`` deliver nothing and still owe the
    ledger an attribution — a broken relay may have left sealed messages
    standing — and both handlers that end a turn *without* a completion (an
    interrupt, a mid-stream error) owe the same. A ``final`` row there would
    record a delivery that did not happen; leaving the rows pending would hand
    them to the next completion on the thread, which then records one turn's
    messages as part of another.

    So: attributed, renumbered, and **no ``final`` row** — which is also what
    keeps a close-out out of ``turn_already_settled``, whose gate matches on
    ``role == "final"``. A close-out can therefore never be the reason a later
    legitimate completion withholds its reply.
    """
    binding_id = uuid.uuid4()
    message_id = uuid.uuid4()
    sealed = _row("sealed", 0, binding_id=binding_id)
    db = _FakeDB(results=[[sealed], [], []])

    ChannelTurnDeliveryLedger.settle_turn(
        db,
        binding_id=binding_id,
        session_message_id=message_id,
        external_message_id=None,
        delivered=False,
        canonical_text=None,
        write_final=False,
    )

    assert sealed.session_message_id == message_id
    assert sealed.role == "sealed"
    assert db.added == [sealed], "no extra row may be written"
    assert all(getattr(r, "role", None) != "final" for r in db.added)


def test_a_retry_after_a_failed_final_updates_the_row_it_already_owns() -> None:
    """Idempotent against its own re-invocation, which is reachable.

    ``turn_already_settled`` deliberately lets a turn whose final delivery
    *failed* be retried — a row recording a delivery that never reached the
    thread is a reason to try again, not a reason to stop. So the second pass
    must find the existing ``final`` row and correct it in place: inserting a
    second one lands straight on the ``(session_message_id, part_index)``
    unique constraint, the write is swallowed by the guards, and the
    correction is lost silently.

    The renumbering also has to start **above** what this message already
    owns, or a newly adopted row would collide with the first pass's indexes.
    """
    binding_id = uuid.uuid4()
    message_id = uuid.uuid4()
    existing_final = _row("final", 0, binding_id=binding_id,
                          session_message_id=message_id, status="failed")
    latecomer = _row("sealed", 0, binding_id=binding_id)
    canonical = "The answer, second time lucky."

    db = _FakeDB(results=[[latecomer], [existing_final], []])
    ChannelTurnDeliveryLedger.settle_turn(
        db,
        binding_id=binding_id,
        session_message_id=message_id,
        external_message_id="spaces/AAA/messages/m9",
        delivered=True,
        canonical_text=canonical,
    )

    # The retry corrected the row it already owned…
    assert existing_final.status == "delivered"
    assert existing_final.external_message_id == "spaces/AAA/messages/m9"
    assert existing_final.visible_char_end == len(canonical)
    # …and the newly adopted row was numbered above it, not onto it.
    assert latecomer.part_index == 1
    assert latecomer.part_index != existing_final.part_index
    assert existing_final in db.added and latecomer in db.added


def test_the_divergence_check_marks_only_a_real_mismatch() -> None:
    """Observational: it marks a row and changes no delivery, in either outcome.

    The candidate seal is drawn from the rows *this invocation* adopted, not
    from a re-query on the message id: adoption is deliberately greedy, so a
    re-query would also return rows an earlier pass had attributed and compare
    this turn's answer against a prefix that is not this turn's — a false
    positive on a check whose whole value is that it is worth believing.
    """
    binding_id = uuid.uuid4()
    message_id = uuid.uuid4()
    canonical = "First paragraph.\n\nSecond paragraph."

    # Match: the sealed prefix is still the head of the finalized answer.
    ok = _row("sealed", 0, binding_id=binding_id, visible_char_end=18,
              content_sha256=visible_digest(canonical[:18]))
    db = _FakeDB(results=[[ok], [], [ok]])
    ChannelTurnDeliveryLedger.settle_turn(
        db, binding_id=binding_id, session_message_id=message_id,
        external_message_id=None, delivered=True, canonical_text=canonical,
        write_final=False,
    )
    assert ok.status == "delivered"

    # Mismatch: same length, different content.
    bad = _row("sealed", 0, binding_id=binding_id, visible_char_end=18,
               content_sha256=visible_digest("x" * 18))
    db = _FakeDB(results=[[bad], [], [bad]])
    ChannelTurnDeliveryLedger.settle_turn(
        db, binding_id=binding_id, session_message_id=message_id,
        external_message_id=None, delivered=True, canonical_text=canonical,
        write_final=False,
    )
    assert bad.status == "diverged"

    # Shorter than what was recorded as already shown — the residual edge a
    # seal covering the whole answer plus its trailing break produces.
    short = _row("sealed", 0, binding_id=binding_id,
                 visible_char_end=len(canonical) + 2,
                 content_sha256=visible_digest(canonical + "\n\n"))
    db = _FakeDB(results=[[short], [], [short]])
    ChannelTurnDeliveryLedger.settle_turn(
        db, binding_id=binding_id, session_message_id=message_id,
        external_message_id=None, delivered=True, canonical_text=canonical,
        write_final=False,
    )
    assert short.status == "diverged"


# ---------------------------------------------------------------------------
# The idempotency gate
# ---------------------------------------------------------------------------


def test_a_failed_final_row_does_not_gate_a_retry() -> None:
    """The asymmetry that keeps this gate from ever costing a reply.

    This is the one place in the feature that *suppresses* a delivery, so it
    answers ``True`` only for a ``final`` row that actually reached the
    thread. A ``failed`` one is a reason to try again. An unknown turn (no id)
    is never settled, and a read that fails answers ``False`` — and hands the
    session back usable, because the caller is about to persist its own rows
    on it.
    """
    binding_id, message_id = uuid.uuid4(), uuid.uuid4()

    # No id at all: never gate, never touch the session.
    db = _FakeDB()
    assert ChannelTurnDeliveryLedger.turn_already_settled(db, None, binding_id) is False

    # A delivered final row: gate.
    db = _FakeDB(results=[[_row("final", 0)]])
    assert (
        ChannelTurnDeliveryLedger.turn_already_settled(db, message_id, binding_id)
        is True
    )

    # Nothing matching (the query itself excludes ``failed``): do not gate.
    db = _FakeDB(results=[[]])
    assert (
        ChannelTurnDeliveryLedger.turn_already_settled(db, message_id, binding_id)
        is False
    )

    # A failed read: do not gate, and leave the session usable.
    db = _FakeDB()
    db.raise_on_exec = True
    assert (
        ChannelTurnDeliveryLedger.turn_already_settled(db, message_id, binding_id)
        is False
    )
    assert db.rollbacks == 1


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------


def test_no_entry_point_raises_into_the_delivery_path() -> None:
    """A lost ledger write costs observability, never a reply.

    Every one of these is called *after* the message has gone out, from a
    relay that is a passenger on somebody else's turn or from an event
    subscriber that may not raise into the bus. So each is driven against a
    session that fails on every verb, and none of them may propagate — nor
    leave the session unusable for the caller's own next statement, which is
    why the rollbacks are counted rather than only the absence of a raise.
    """
    draft = _row("draft", 0)
    binding_id, message_id = uuid.uuid4(), uuid.uuid4()

    def _broken() -> _FakeDB:
        db = _FakeDB(rows={draft.id: draft})
        db.raise_on_get = True
        db.raise_on_exec = True
        db.raise_on_commit = True
        return db

    db = _broken()
    ChannelTurnDeliveryLedger.record_seal(
        db, binding_id=binding_id, row_id=draft.id, part_index=0,
        external_message_id="m1", visible_char_end=1, content_sha256="x",
    )
    assert db.rollbacks >= 1

    db = _broken()
    assert ChannelTurnDeliveryLedger.record_draft(
        db, binding_id=binding_id, part_index=0, external_message_id="m1"
    ) is None
    assert db.rollbacks >= 1

    db = _broken()
    ChannelTurnDeliveryLedger.mark_failed(db, draft.id)
    assert db.rollbacks >= 1

    db = _broken()
    assert (
        ChannelTurnDeliveryLedger.turn_already_settled(db, message_id, binding_id)
        is False
    )

    db = _broken()
    ChannelTurnDeliveryLedger.settle_turn(
        db, binding_id=binding_id, session_message_id=message_id,
        external_message_id="m1", delivered=True, canonical_text="text",
    )
    assert db.rollbacks >= 1


def test_an_unnamed_turn_writes_nothing_rather_than_a_row_it_cannot_key() -> None:
    """``session_message_id=None`` (or no binding) leaves the rows pending.

    A ledger row that cannot name its turn is worse than no row: it would be
    adopted by whatever completes next on this thread and recorded against the
    wrong turn. Pending is the correct resting state — the next completion on
    this thread will attribute it, and the close-out exists so a *terminated*
    turn's rows are attributed to itself first.
    """
    db = _FakeDB(results=[[_row("sealed", 0)]])
    ChannelTurnDeliveryLedger.settle_turn(
        db, binding_id=uuid.uuid4(), session_message_id=None,
        external_message_id="m1", delivered=True, canonical_text="text",
    )
    assert db.added == [] and db.commits == 0

    db = _FakeDB(results=[[_row("sealed", 0)]])
    ChannelTurnDeliveryLedger.settle_turn(
        db, binding_id=None, session_message_id=uuid.uuid4(),
        external_message_id="m1", delivered=True, canonical_text="text",
    )
    assert db.added == [] and db.commits == 0


def test_an_unusable_transport_id_is_recorded_as_unknown_not_invented() -> None:
    """The column is a ``varchar(255)`` and its only consumer is a person.

    An adapter that answers with something other than a non-empty string, or
    with an id longer than the column, would fail this module's commit — and
    the guards would swallow it, so the row would be lost silently rather than
    loudly. ``None`` is the honest "delivered, message unknown".
    """
    db = _FakeDB()
    ChannelTurnDeliveryLedger.record_draft(
        db, binding_id=uuid.uuid4(), part_index=0, external_message_id=None
    )
    ChannelTurnDeliveryLedger.record_draft(
        db, binding_id=uuid.uuid4(), part_index=0, external_message_id=""
    )
    ChannelTurnDeliveryLedger.record_draft(
        db, binding_id=uuid.uuid4(), part_index=0, external_message_id=12345
    )
    ChannelTurnDeliveryLedger.record_draft(
        db, binding_id=uuid.uuid4(), part_index=0, external_message_id="x" * 400
    )

    unknown, empty, numeric, long = db.added
    assert unknown.external_message_id is None
    assert empty.external_message_id is None
    assert numeric.external_message_id is None
    assert long.external_message_id == "x" * 255
