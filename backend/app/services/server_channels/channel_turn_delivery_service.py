"""Writes and reads of the per-turn outbound delivery ledger.

The durable counterpart to ``channel_stream_relay``'s in-memory state: one row
per external message a channel turn wrote. See
``app/models/server_channels/channel_turn_delivery.py`` for the schema and for
why ``session_message_id`` is nullable.

**Every function here is total.** Not as defensive habit — as the module's
contract. Its two callers are the streaming relay (a passenger on somebody
else's turn) and ``channel_outbound_service``'s event subscribers (whose §11a
discipline forbids a raise on the delivery path), and both call it *after* the
message has already gone out. A ledger write that raised would abort
bookkeeping about a delivery that already happened, or worse, take the
delivery's own handler down with it. A lost ledger write costs observability,
never a reply — so every entry point swallows, logs, and returns a value that
tells the caller "no ledger" rather than propagating.

**Commit discipline is copied from ``_persist_status_message_id``**: each
function owns its write, commits it, and rolls back inside its own guard on
failure. Callers hand in the session they already have — the relay's per-flush
session, the subscriber's ``create_session()`` — and this module never opens
one, never holds one, and never keeps an ORM instance past its own call.

**Boundary writes only.** Nothing here is called from the relay's rolling
~3-second draft patch. The three write moments are a seal, the fresh draft
that opens after a seal, and the final delivery at completion.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session as DBSession, col, select

from app.models import (
    CHANNEL_DELIVERY_DELIVERED,
    CHANNEL_DELIVERY_DIVERGED,
    CHANNEL_DELIVERY_DRAFT,
    CHANNEL_DELIVERY_FAILED,
    CHANNEL_DELIVERY_FINAL,
    CHANNEL_DELIVERY_SEALED,
    ChannelTurnDelivery,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelTurnLedger]"


def visible_digest(visible_text: str) -> str:
    """sha256 (hex) of a visible-space string. Total; ``""`` on failure.

    Kept next to the writers rather than inlined at each call site so the two
    halves of the divergence check — the digest a seal records and the digest
    the completion recomputes — can never be spelled two different ways.
    """
    try:
        return hashlib.sha256(visible_text.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 — a hash is never worth a failed delivery
        logger.warning("%s Could not digest delivered text", _LOG_PREFIX, exc_info=True)
        return ""


def delivered_prefix_key(visible_prefix: str) -> tuple[int, str]:
    """The ``(visible_char_end, content_sha256)`` pair recorded for a prefix.

    The two halves of the divergence check have to be measured in the same
    space, and by default they are not: the canonical answer is
    ``.strip()``ed — at finalize, and again by ``_agent_message_text`` — while
    the relay's buffer is the agent's raw output. A reply that begins with
    whitespace would therefore shift every offset by that much and make the
    check fire on a turn where nothing whatsoever diverged.

    ``lstrip`` closes that, and only that: the leading whitespace is the part
    the canonical text has dropped. Interior and trailing whitespace inside a
    *prefix* is not stripped there — a seal cuts at a paragraph break, so its
    trailing newlines sit in the middle of the finished answer — and stripping
    it here would break the comparison rather than fix it. The residual edge is
    a seal that covers the entire answer *and* trails whitespace, where the
    canonical text is shorter than the recorded end and the check reports a
    divergence; it costs a log line and nothing else.
    """
    normalized = visible_prefix.lstrip()
    return len(normalized), visible_digest(normalized)


def _external_id(value: Any) -> str | None:
    """A transport message id fit for a ``varchar(255)``. Total.

    One rule, applied at the writers rather than at each caller: the id
    crosses in from an adapter, and a long or non-string one would fail this
    module's commit — swallowed by the guards, so the row would be lost
    silently rather than loudly. ``None`` is the column's honest "delivered,
    message unknown".
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:255]


def _visible(text: str) -> str:
    """The relay's visible-space notion, borrowed rather than re-modelled.

    Function-level import: ``channel_stream_relay`` imports the outbound
    service at module scope and this module is imported by both, so keeping
    the edge lazy is what stops the cycle. The point of borrowing at all is
    that there is exactly one model of which control tags finalize strips —
    the regexes the relay itself imports from ``message_service`` — and a
    second copy here is how the two would drift apart while a drift-guard test
    kept passing on the wrong pair.
    """
    try:
        from app.services.server_channels.channel_stream_relay import (
            _visible as relay_visible,
        )

        return relay_visible(text)
    except Exception:  # noqa: BLE001 — total, like everything here
        logger.warning("%s Could not strip tags for the ledger", _LOG_PREFIX, exc_info=True)
        return text


def _recover(db: DBSession, what: str) -> None:
    """Roll the session back after a failed ledger statement. Never raises.

    The session belongs to the caller and outlives this module's call: the
    relay flushes several boundaries through one, and the outbound handler
    persists message ids and error rows through the one it gates on. A failed
    statement leaves the transaction unusable, so **every** guard that returns
    early has to hand the session back in a state the next statement can use —
    otherwise this module's "a lost ledger write costs observability, never a
    reply" contract is quietly broken by taking somebody else's write down
    with it.
    """
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("%s Rollback after %s also failed", _LOG_PREFIX, what)


def _commit(db: DBSession, what: str) -> bool:
    """Commit, or log and roll back. Never raises. See the module docstring."""
    try:
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("%s Could not persist %s", _LOG_PREFIX, what, exc_info=True)
        _recover(db, "a failed ledger write")
        return False


class ChannelTurnDeliveryLedger:
    """Boundary writes and completion reads of ``channel_turn_delivery``."""

    # ------------------------------------------------------------------
    # Writers (called from the relay's flush, under its lock)
    # ------------------------------------------------------------------

    @staticmethod
    def record_seal(
        db: DBSession,
        *,
        binding_id: uuid.UUID,
        row_id: uuid.UUID | None,
        part_index: int,
        external_message_id: str | None,
        visible_char_end: int,
        content_sha256: str,
    ) -> None:
        """Record a slice the relay sealed off and will never send again.

        ``row_id`` is the ``draft`` row this seal *settled*, and it is updated
        **in place** into the ``sealed`` row rather than superseded by a new
        one: the draft and the message it sealed are the same external message,
        and inserting a second row would leave the draft standing in the ledger
        as a message that is no longer a draft and no longer anywhere.

        ``None`` means the relay was holding no draft row, and the seal is
        inserted as a new row instead. That happens when a flush seals before
        any draft patch has landed (so no row was ever written), when the
        previous batch's hand-over released the row, and when
        :meth:`record_draft` could not commit its row.

        ``visible_char_end`` / ``content_sha256`` describe the cumulative
        **visible prefix** the relay has now put on screen, not this slice
        alone: they are one half of the completion's divergence check, whose
        other half is the whole finalized answer.
        """
        try:
            now = datetime.now(UTC)
            row = db.get(ChannelTurnDelivery, row_id) if row_id else None
            if row is None:
                row = ChannelTurnDelivery(binding_id=binding_id, created_at=now)
            row.part_index = part_index
            row.role = CHANNEL_DELIVERY_SEALED
            row.status = CHANNEL_DELIVERY_DELIVERED
            row.external_message_id = _external_id(external_message_id)
            row.visible_char_end = visible_char_end
            row.content_sha256 = content_sha256
            row.updated_at = now
            db.add(row)
        except Exception:  # noqa: BLE001 — see the module docstring
            logger.warning("%s Could not stage a sealed row", _LOG_PREFIX, exc_info=True)
            # The ``db.get`` above is a SELECT, so this guard can be reached
            # with the transaction already failed — and the relay flushes the
            # rest of its boundaries through this same session.
            _recover(db, "a failed sealed-row stage")
            return
        _commit(db, "a sealed delivery row")

    @staticmethod
    def record_draft(
        db: DBSession,
        *,
        binding_id: uuid.UUID,
        part_index: int,
        external_message_id: str | None,
    ) -> uuid.UUID | None:
        """Record a fresh draft message. Returns its row id.

        Called for the turn's first draft and for each one opened after a seal
        — both created a new external message, and one row per external
        message is this table's grain. The rolling patches in between write
        nothing.

        ``None`` back means "no ledger row for this draft", which is a
        perfectly workable state for the caller: the next seal inserts a row
        instead of updating one.

        **No ``visible_char_end`` or digest**, deliberately. A draft is
        rewritten every few seconds and this row is written once, so any offset
        stored here would be stale before the next flush — and the rolling
        patches must never write (the whole point of a boundary-only ledger).
        The offsets are filled in when the row's content stops moving: at the
        seal that settles it, or at the completion that finalizes it.
        """
        try:
            now = datetime.now(UTC)
            row = ChannelTurnDelivery(
                binding_id=binding_id,
                part_index=part_index,
                role=CHANNEL_DELIVERY_DRAFT,
                status=CHANNEL_DELIVERY_DELIVERED,
                external_message_id=_external_id(external_message_id),
                created_at=now,
                updated_at=now,
            )
            # The id is read **before** the flush, and that ordering is
            # load-bearing rather than tidy. ``id`` is a ``default_factory``,
            # so it is already populated here; after ``commit()`` it is
            # expired — ``create_session`` is a plain ``Session(engine)``, so
            # ``expire_on_commit`` is on — and reading it there is a refresh
            # SELECT that can fail on a row which committed perfectly well.
            # Losing the id is not cosmetic at that point: the relay would go
            # on seeing ``_ledger_row_id is None`` and open a **new** draft row
            # on every 3-second patch of the same standing message, which is
            # exactly the one-row-per-flush shape this table's grain forbids.
            row_id = row.id
            db.add(row)
        except Exception:  # noqa: BLE001
            logger.warning("%s Could not stage a draft row", _LOG_PREFIX, exc_info=True)
            return None
        return row_id if _commit(db, "a draft delivery row") else None

    @staticmethod
    def mark_failed(db: DBSession, row_id: uuid.UUID | None) -> None:
        """Flag a row whose boundary delivery failed. Never blocks the turn.

        The relay calls this when a **seal** could not be sent: the slice is
        not advanced past, the next flush retries the same seal, and if the
        turn ends first the text is still in the tail. The row keeps its
        ``draft`` role — the message it names is still standing and still being
        rewritten — and ``failed`` records that a boundary write on it did not
        land. A successful retry flips it to ``sealed``/``delivered``.
        """
        if row_id is None:
            return
        try:
            row = db.get(ChannelTurnDelivery, row_id)
            if row is None:
                return
            row.status = CHANNEL_DELIVERY_FAILED
            row.updated_at = datetime.now(UTC)
            db.add(row)
        except Exception:  # noqa: BLE001
            logger.warning("%s Could not stage a failed row", _LOG_PREFIX, exc_info=True)
            # Same reason as :meth:`record_seal`: the ``db.get`` is a SELECT,
            # and the flush that called this goes on using the session.
            _recover(db, "a failed failed-row stage")
            return
        _commit(db, "a failed delivery row")

    # ------------------------------------------------------------------
    # Completion (called from handle_stream_completed)
    # ------------------------------------------------------------------

    @staticmethod
    def turn_already_settled(
        db: DBSession,
        session_message_id: uuid.UUID | None,
        binding_id: uuid.UUID | None = None,
    ) -> bool:
        """Whether this turn's final delivery is already on the record.

        The idempotency gate against a duplicate or racing
        ``STREAM_COMPLETED``. Safe for the multi-batch turn by construction:
        each LLM batch writes its own agent message, so batch 2's event asks
        about a different id than batch 1 settled.

        **A ``failed`` final row does not count**, and that asymmetry is the
        point: this gate may never be the reason a reply is not sent. A row
        that records a delivery which did not reach the thread is a reason to
        try again, not a reason to stop.

        ``False`` on any failure, for the same reason.

        ``binding_id`` narrows it to the thread. A message id belongs to one
        thread in practice, so this changes no outcome today — but this is the
        one place in the feature that **suppresses a delivery**, and it should
        be keyed on everything the caller knows rather than on the least that
        happens to work.
        """
        if session_message_id is None:
            return False
        try:
            conditions = [
                ChannelTurnDelivery.session_message_id == session_message_id,
                ChannelTurnDelivery.role == CHANNEL_DELIVERY_FINAL,
                ChannelTurnDelivery.status != CHANNEL_DELIVERY_FAILED,
            ]
            if binding_id is not None:
                conditions.append(ChannelTurnDelivery.binding_id == binding_id)
            row = db.exec(select(ChannelTurnDelivery).where(*conditions)).first()
            return row is not None
        except Exception:  # noqa: BLE001 — never gate a delivery on a failed read
            logger.warning(
                "%s Could not check whether the turn was already settled",
                _LOG_PREFIX,
                exc_info=True,
            )
            # The only ledger call that runs *before* a send, and so the only
            # one whose failure would hand a poisoned session to somebody
            # else: a DBAPI error leaves the transaction failed, and the
            # delivery that follows this gate persists its own message id and
            # its own error rows on this very session. Every other entry point
            # recovers through ``_commit``; this read never reaches it, so it
            # recovers here.
            _recover(db, "a failed settled-turn check")
            return False

    @staticmethod
    def settle_turn(
        db: DBSession,
        *,
        binding_id: uuid.UUID | None,
        session_message_id: uuid.UUID | None,
        external_message_id: str | None,
        delivered: bool,
        canonical_text: str | None,
        write_final: bool = True,
    ) -> None:
        """Attribute this turn's rows and close it with a ``final`` one.

        Three things, in one commit, all of them best-effort:

        1. **Adoption.** Every pending row for this thread (written at a
           boundary, before the emitter had named the turn's agent message)
           gets ``session_message_id``. This is the only place the column is
           ever filled in, and the id always comes from the stream event —
           never from a lookup. The rows are renumbered ``0..n-1`` in delivery
           order, which is what makes ``part_index`` dense and the unique
           constraint satisfiable even when a previous interrupted turn left
           its own pending rows behind.
        2. **The final row.** The last pending ``draft`` becomes ``final``; if
           there is none — a turn that never sealed, or one with no relay — a
           fresh row is inserted.
        3. **The divergence check**, in :meth:`_check_prefix`.

        ``session_message_id`` of ``None`` means the emitter said this turn
        wrote no agent message (or the event predates turn identity). Nothing
        is adopted and no final row is written: a ledger row that cannot name
        its turn is worse than no row, and the pending rows stay pending for
        the next completion on this thread.

        ``write_final=False`` does step 1 and step 3 without step 2. It is for
        the arms of ``handle_stream_completed`` that deliver **nothing** and
        still have to attribute what a broken relay already put on screen —
        writing a ``final`` row there would record a delivery that did not
        happen, while leaving the rows pending hands them to the next
        completion on the thread.

        **Idempotent against its own re-invocation**, which is reachable: the
        gate in :meth:`turn_already_settled` deliberately lets a turn whose
        final delivery *failed* be retried. So the final row is looked up
        before it is inserted, and the renumbering starts above whatever this
        message already owns — otherwise the second pass would insert straight
        onto the unique constraint and lose the row it was trying to correct.
        """
        if session_message_id is None or binding_id is None:
            return
        try:
            pending = list(
                db.exec(
                    select(ChannelTurnDelivery)
                    .where(
                        ChannelTurnDelivery.binding_id == binding_id,
                        col(ChannelTurnDelivery.session_message_id).is_(None),
                    )
                    .order_by(
                        col(ChannelTurnDelivery.created_at),
                        col(ChannelTurnDelivery.part_index),
                    )
                ).all()
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s Could not read the pending rows for binding %s",
                _LOG_PREFIX,
                binding_id,
                exc_info=True,
            )
            pending = []

        try:
            # What this message already owns, from an earlier pass over the
            # same turn. Read before the renumbering, because it decides where
            # the renumbering may start.
            existing = list(
                db.exec(
                    select(ChannelTurnDelivery).where(
                        ChannelTurnDelivery.session_message_id == session_message_id
                    )
                ).all()
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s Could not read the settled rows for message %s",
                _LOG_PREFIX,
                session_message_id,
                exc_info=True,
            )
            existing = []

        # ``.strip()`` to match how finalize stores the canonical answer (and
        # how ``_agent_message_text`` hands it over), so this side is
        # normalised here rather than relying on every caller having done it —
        # the other side of the comparison is normalised in
        # :func:`delivered_prefix_key`, and the two rules only work as a pair.
        visible_canonical = (
            _visible(canonical_text).strip() if canonical_text else None
        )
        now = datetime.now(UTC)
        adopted_ids: list[uuid.UUID] = []
        try:
            # Above everything already attributed to this message, so a second
            # pass cannot collide with the first one's indexes.
            base = max((row.part_index for row in existing), default=-1) + 1
            for offset, row in enumerate(pending):
                row.session_message_id = session_message_id
                row.part_index = base + offset
                row.updated_at = now
                db.add(row)
                # Collected here, before the commit expires these instances,
                # and handed to the prefix check so that it compares against
                # rows *this* invocation attributed rather than re-querying
                # everything the message now owns. See :meth:`_check_prefix`.
                adopted_ids.append(row.id)

            if write_final:
                final_row = None
                # The draft this turn was still rewriting is the message the
                # final text went into, so it becomes the final row rather than
                # being superseded by one.
                for row in reversed(pending):
                    if row.role == CHANNEL_DELIVERY_DRAFT:
                        final_row = row
                        break
                if final_row is None:
                    # A retry: this message already has its final row. Update
                    # it in place — inserting a second would hit the unique
                    # constraint and lose the correction.
                    final_row = next(
                        (
                            row
                            for row in existing
                            if row.role == CHANNEL_DELIVERY_FINAL
                        ),
                        None,
                    )
                if final_row is None:
                    final_row = ChannelTurnDelivery(
                        binding_id=binding_id,
                        session_message_id=session_message_id,
                        part_index=base + len(pending),
                        created_at=now,
                    )
                final_row.role = CHANNEL_DELIVERY_FINAL
                final_row.status = (
                    CHANNEL_DELIVERY_DELIVERED
                    if delivered
                    else CHANNEL_DELIVERY_FAILED
                )
                if external_message_id:
                    final_row.external_message_id = _external_id(external_message_id)
                if visible_canonical is not None:
                    # The whole canonical answer, not this row's increment: the
                    # final row is where the turn's total is recorded, and it is
                    # the value a sealed prefix is compared against.
                    final_row.visible_char_end = len(visible_canonical)
                    final_row.content_sha256 = visible_digest(visible_canonical)
                final_row.updated_at = now
                db.add(final_row)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s Could not stage the settled rows for message %s",
                _LOG_PREFIX,
                session_message_id,
                exc_info=True,
            )
            _recover(db, "a failed settle")
            return

        if not _commit(db, "the settled delivery rows"):
            return
        ChannelTurnDeliveryLedger._check_prefix(
            db,
            session_message_id=session_message_id,
            row_ids=adopted_ids,
            visible_canonical=visible_canonical,
        )

    @staticmethod
    def _check_prefix(
        db: DBSession,
        *,
        session_message_id: uuid.UUID,
        row_ids: list[uuid.UUID],
        visible_canonical: str | None,
    ) -> None:
        """Does the finalized answer still start with what was already sealed?

        **Observational only, and that is deliberate.** The settled reply is
        the relay's own accumulated text and not the stored ``SessionMessage``
        content — decided with the user, and recorded in
        ``server_channels_tech.md`` under "The finalize divergence policy". So
        this method delivers nothing, withholds nothing and re-sends nothing
        in either outcome: the tail went out before it ran, exactly as it did
        before this table existed. What it adds is that the assumption is now
        *checked*: a mismatch marks the sealed row ``diverged`` and logs a
        warning, so "does this ever actually happen" becomes a question the
        logs can answer instead of one the design has to assume.

        The comparison is a prefix test in visible space — the sealed row's
        ``visible_char_end`` characters of the canonical answer, digested and
        compared against what the relay recorded having shown.

        ``row_ids`` are the rows :meth:`settle_turn` attributed **in this
        invocation**, and the candidate seals are drawn from them rather than
        from a re-query on ``session_message_id``. The two differ, and the
        difference is a false positive: adoption is deliberately greedy — it
        takes every pending row on the thread so a previous turn's rows cannot
        leak forward — so a re-query would also return rows a previous pass
        over this same message had already attributed, and compare this turn's
        answer against a prefix that is not this turn's. Narrowing to what was
        just adopted keeps the check keyed to the delivery it is checking; a
        turn with nothing to adopt has nothing to compare and returns.
        """
        if not visible_canonical or not row_ids:
            return
        try:
            sealed = db.exec(
                select(ChannelTurnDelivery)
                .where(
                    col(ChannelTurnDelivery.id).in_(row_ids),
                    ChannelTurnDelivery.role == CHANNEL_DELIVERY_SEALED,
                    ChannelTurnDelivery.status == CHANNEL_DELIVERY_DELIVERED,
                    # Postgres sorts NULLs FIRST under DESC, so without this a
                    # row with no recorded offset would be picked as "the max"
                    # and the guard below would make the whole check a silent
                    # no-op for the turn.
                    col(ChannelTurnDelivery.visible_char_end).is_not(None),
                )
                .order_by(col(ChannelTurnDelivery.visible_char_end).desc())
            ).first()
            if sealed is None:
                return
            end = sealed.visible_char_end
            expected = sealed.content_sha256
            if not end or not expected:
                return
            if len(visible_canonical) >= end and visible_digest(
                visible_canonical[:end]
            ) == expected:
                return
            logger.warning(
                "%s Finalized text for message %s no longer starts with the %d "
                "visible characters already sealed into the thread — the relay "
                "tail was delivered as usual and the row is marked diverged",
                _LOG_PREFIX,
                session_message_id,
                end,
            )
            sealed.status = CHANNEL_DELIVERY_DIVERGED
            sealed.updated_at = datetime.now(UTC)
            db.add(sealed)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s Could not run the completion prefix check for message %s",
                _LOG_PREFIX,
                session_message_id,
                exc_info=True,
            )
            _recover(db, "a failed prefix check")
            return
        _commit(db, "a diverged delivery row")


__all__ = ["ChannelTurnDeliveryLedger", "visible_digest"]
