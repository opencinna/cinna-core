"""Pass D of the status-repair sweep — channel turn deliveries left as drafts.

A ``ChannelTurnDelivery`` row with ``role="draft"`` says: *there is a message
standing in an external thread that the streaming relay is still rewriting.* It
stops being a draft at one of three moments — a seal turns it into ``sealed``, a
completion turns it into ``final``, an interrupt or error closes it out — and
every one of those runs inside the turn. A process that dies mid-turn reaches
none of them, and the row stays a draft forever while the message it names has
stopped moving.

**What that costs, if nothing reaps it.** Adoption in
``ChannelTurnDeliveryLedger.settle_turn`` is deliberately greedy: the *next*
completion on that thread takes every unattributed row, so a crashed turn's
abandoned draft is adopted into a later turn and — being the last pending draft
— becomes **that** turn's ``final`` row. The ledger then records a message the
new turn never wrote as the message it answered with, and the divergence check
compares the new answer against the old one's prefix and reports a mismatch that
never happened. The model's own docstring names this and says to rule out a
crash before believing the warning; this pass is what removes the crashes from
that list.

**The repair, and why it is those two writes.** ``role="sealed"`` +
``status="diverged"``:

* ``sealed`` is the honest description of what the message now is — standing in
  the thread, never to be rewritten again, which is precisely what ``sealed``
  means here. It also takes the row out of the running for the next
  completion's ``final`` slot, which is the mis-attribution above.
* ``diverged`` takes it out of the next completion's prefix check, which only
  considers ``sealed`` rows still marked ``delivered``. Without it, sealing
  alone would swap one false warning for another.

The row keeps ``session_message_id IS NULL`` and will still be adopted. That is
left alone on purpose: adoption is what keeps ``part_index`` dense and the
unique constraint satisfiable, the attribution is bounded to this table, and
neither of the two consequences that actually mislead a reader survives the two
writes above.

**Verify, then repair — and the row's own clock is not the evidence.** A draft
row is written *once*, when the message is created; the relay's rolling
~3-second patches deliberately write nothing. So ``updated_at`` is the age of
the message, not a heartbeat, and a genuinely long turn's draft looks exactly
like an abandoned one. The evidence is therefore taken from elsewhere:

1. **The session.** A binding whose session is still ``interaction_status =
   "running"`` has a turn that may well still be live, and is left alone. This
   is where the pass ordering pays: Pass B has already cleared the sessions
   whose streams are genuinely over, so by the time this runs, "still running"
   means "still running".

   **With one exclusion, and it is not optional.** Pass B clears that column for
   any stream past its threshold *without cancelling the stream* — the turn
   keeps running. So a channel turn that legitimately exceeds the stream
   threshold would be cleared by B and then, in the same tick, seen here as "not
   running" with an old draft, and sealed as diverged while the relay is still
   patching the message this pass rewrites. B manufacturing the evidence D
   trusts is the golden rule inverted. Every session Pass B touched is recorded
   in the tick's ``RepairContext`` and skipped here.
2. **The relay registry**, as a veto only — the same shape as Pass A's
   ``is_build_in_flight``. It is process-local, so it can only ever *prevent* a
   repair on the leader, never license one; a live relay in another worker is
   covered by (1).

Finally, a reaped turn's thread is usually still showing the progress notice the
pipeline posted ("working…"), because the notice is cleared by the same terminal
handlers that never ran. The binding's ``status_message_id`` is dropped and the
notice **settled** — rewritten to say the turn was interrupted — best-effort and
strictly after the database write.
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.config import settings
from app.models.server_channels.channel_turn_delivery import (
    CHANNEL_DELIVERY_DIVERGED,
    CHANNEL_DELIVERY_DRAFT,
    CHANNEL_DELIVERY_SEALED,
    ChannelTurnDelivery,
)
from app.models.server_channels.channel_thread_binding import ChannelThreadBinding
from app.models.server_channels.server_channel import ServerChannel
from app.models.sessions.session import Session as ChatSession
from app.services.system.status_repair_context import RepairContext
from app.utils import as_utc

logger = logging.getLogger(__name__)

# What the reaped turn's progress notice is rewritten to say. The notice is
# settled rather than deleted: a reaped turn has something honest to put in the
# slot, and ``clear_status``'s own docstring reserves deletion for the case
# where it does not.
_INTERRUPTED_NOTICE = "This turn was interrupted and did not finish."


class _Claim(NamedTuple):
    """One delivery row's draft claim, as observed by the candidate query.

    ``(role, updated_at)`` together are the claim token. ``role`` alone is not
    enough for the usual reason — a binding opens a fresh draft on every turn,
    so the same row id can carry the word ``draft`` for a turn that started
    after we looked — but here the pair is doing something narrower than in the
    other passes: nothing rewrites a draft row in place except the seal and the
    close-out that end it, so a changed ``updated_at`` means the row has already
    reached the state this pass was going to put it in.
    """

    delivery_id: UUID
    binding_id: UUID
    role: str
    updated_at: datetime


def _relay_is_live(session_id: UUID | None) -> bool:
    """Veto-only liveness check against this process's relay registry.

    ``evictable`` is the registry's own discriminator for "no live turn is
    behind this relay any more" — it is the predicate the registry itself uses
    to decide which entries it may drop — so a relay that is not evictable is a
    turn that may still be writing into the thread. Borrowed rather than
    re-derived, because a second definition of "is this turn live" is how the
    two would drift apart.

    **Not ``spent``**, which is the narrower half of it: ``spent`` requires the
    flusher to have been *stopped*, and a turn cancelled mid-stream (client
    disconnect, ``/stop``, environment restart) never runs ``stop()``. Its relay
    is therefore never spent, and — since entries are only evicted under the
    registry's 500-entry cap — a ``spent``-based veto would stand forever,
    making this pass structurally incapable of repairing a draft abandoned by a
    cancelled turn. That is one of the two failure classes it exists for.
    ``evictable`` covers it: ``spent or (retired and not stopped)``, the second
    disjunct being exactly the cancelled-turn shape.

    Process-local, therefore **veto-only**: ``False`` never means "no turn is
    running anywhere", only "not one this process knows about", and the session
    check in ``_read_and_prepare`` is what covers the other workers. Never
    raises — a registry that cannot answer must not be able to stop the sweep,
    and it errs towards leaving the row alone.
    """
    if session_id is None:
        return False
    try:
        from app.services.server_channels.channel_stream_relay import (
            ChannelStreamRegistry,
        )

        relay = ChannelStreamRegistry.get(session_id)
        return relay is not None and not relay.evictable
    except Exception as exc:  # noqa: BLE001 — a veto may not raise
        logger.debug("Status repair: relay registry check failed: %s", exc)
        return True


class _Plan(NamedTuple):
    """What the read phase decided, in plain values only."""

    age: timedelta
    #: The notice to take down, if the binding still has one outstanding.
    status_message_id: str | None
    channel_id: UUID | None


def _read_and_prepare(
    ctx: RepairContext, claim: _Claim, now: datetime
) -> _Plan | None:
    """Verify the claim, check the live evidence, and read what the repair needs."""
    session = ctx.session
    delivery = session.get(ChannelTurnDelivery, claim.delivery_id)
    if delivery is None:
        return None
    if delivery.role != claim.role or as_utc(delivery.updated_at) != claim.updated_at:
        logger.debug(
            "Status repair: delivery row %s moved while we looked — leaving it",
            claim.delivery_id,
        )
        return None

    age = now - claim.updated_at
    if age < timedelta(minutes=settings.STATUS_REPAIR_CHANNEL_DRAFT_MAX_AGE_MINUTES):
        return None

    binding = session.get(ChannelThreadBinding, claim.binding_id)
    if binding is None:
        # The binding is gone, so the row is about to be cascaded away with it
        # (or already has been in another transaction). Nothing to reconcile.
        return None

    if ctx.is_session_in_motion(binding.session_id):
        # Pass B wrote (or withheld) this session's ``interaction_status`` this
        # very tick, so the column below is this sweep's own output rather than
        # evidence about the turn. Most consequentially: B clears a long stream
        # without cancelling it, so "not running" here would be a fact B
        # manufactured about a turn that is still writing — and sealing it would
        # rewrite the message the live relay is patching.
        logger.debug(
            "Status repair: delivery row %s is %s old but this sweep just "
            "touched session %s — leaving the turn for the next tick",
            claim.delivery_id,
            age,
            binding.session_id,
        )
        return None

    if binding.session_id is not None:
        chat_session = session.get(ChatSession, binding.session_id)
        if chat_session is not None and chat_session.interaction_status == "running":
            logger.debug(
                "Status repair: delivery row %s is %s old but session %s is "
                "still streaming — leaving the turn alone",
                claim.delivery_id,
                age,
                binding.session_id,
            )
            return None

    if _relay_is_live(binding.session_id):
        logger.info(
            "Status repair: delivery row %s is %s old but a relay for session "
            "%s is still live in this process — leaving the turn alone",
            claim.delivery_id,
            age,
            binding.session_id,
        )
        return None

    return _Plan(
        age=age,
        status_message_id=binding.status_message_id,
        channel_id=binding.server_channel_id,
    )


def _seal_abandoned_draft(session: Session, claim: _Claim, plan: _Plan) -> bool:
    """Write the repair: seal the row, mark it diverged, drop the notice id."""
    delivery = session.get(ChannelTurnDelivery, claim.delivery_id)
    if delivery is None:
        session.rollback()
        return False
    if delivery.role != claim.role or as_utc(delivery.updated_at) != claim.updated_at:
        session.rollback()
        return False

    now = datetime.now(UTC)
    delivery.role = CHANNEL_DELIVERY_SEALED
    delivery.status = CHANNEL_DELIVERY_DIVERGED
    delivery.updated_at = now
    session.add(delivery)

    binding = session.get(ChannelThreadBinding, claim.binding_id)
    if (
        binding is not None
        and plan.status_message_id is not None
        and binding.status_message_id == plan.status_message_id
    ):
        # Cleared in the same transaction as the seal so the two can never
        # disagree: the notice belongs to the turn being reaped, and a thread
        # whose turn is over has no notice outstanding by definition. The
        # external message is settled separately and best-effort — a notice id
        # that outlives its message only costs the next turn one extra round
        # trip (the patch misses, a fresh notice is posted), while a *stale* id
        # left behind here would make the next turn rewrite a message belonging
        # to a turn that failed.
        #
        # **Only when it is still the same id.** The settle below targets
        # ``plan.status_message_id``, read in the read phase; if a new turn has
        # posted a fresh notice in between, nulling unconditionally would drop
        # the *new* id while settling the *old* message — the new turn's notice
        # then belongs to nobody and is never settled.
        binding.status_message_id = None
        binding.updated_at = now
        session.add(binding)

    session.commit()
    return True


def _load_notice_target(
    session: Session, claim: _Claim, plan: _Plan
) -> tuple[ServerChannel, str] | None:
    """Read the channel and thread key for the notice settle, detached.

    The channel instance is **expunged** and the transaction closed before it is
    handed back, so the transport round-trip that follows does not run with a
    database transaction open on the sweep's pinned leader connection. Safe
    because ``ServerChannel`` is plain columns with no relationships: every
    attribute the adapter reads is already loaded, and nothing will lazy-load
    off a detached instance.
    """
    from app.services.server_channels.channel_outbound_service import (
        _binding_thread_key,
    )

    binding = session.get(ChannelThreadBinding, claim.binding_id)
    channel = (
        session.get(ServerChannel, plan.channel_id)
        if plan.channel_id is not None
        else None
    )
    if binding is None or channel is None:
        session.rollback()
        return None
    thread_key = _binding_thread_key(binding, channel)
    session.expunge(channel)
    session.rollback()
    if thread_key is None:
        return None
    return channel, thread_key


async def _settle_stale_notice(session: Session, claim: _Claim, plan: _Plan) -> None:
    """Rewrite the reaped turn's progress notice to say what became of it.

    ``set_status`` and not ``clear_status``: the thread is showing "working…"
    for a turn that will never finish, and a reaped turn has something honest to
    put in that slot — which is precisely the case ``clear_status``'s own
    docstring says *not* to use it for ("only where the turn ends with nothing
    to put in the slot"). Deleting it would also leave a "Message deleted by its
    author" tombstone on Google Chat, in a thread whose last visible event would
    then be nothing at all.

    Not ``set_binding_status``, which is the persisting variant: it takes the
    ORM binding and would hold it — and this sweep's pinned leader connection —
    across the HTTP call. The binding half of the write already happened, in the
    same transaction as the seal.

    Best-effort in every sense, and the ``except`` is deliberately broad. This
    runs on the shared leader connection inside the pass loop, so an exception
    escaping here would abort the rest of the sweep, and "the notice service
    only raises ``ChannelError``" has been wrong before — an EXPIRED Google Chat
    instance raises things that are not ``ChannelError`` at all (which is why
    ``set_status`` itself catches broadly around its adapter lookup). A notice
    we fail to rewrite is a cosmetic leftover; the database has already recorded
    the turn as over.

    Note what a *failed patch* does inside ``set_status``: it degrades to
    posting a **fresh** message, whose id we discard (the binding's
    ``status_message_id`` was nulled with the seal). The thread is then left
    with the old "working…" still standing plus the new note beneath it. That is
    the accepted worst case — two visible messages instead of one — and it is
    better than the alternative of keeping an id whose message a later turn
    would rewrite.
    """
    if plan.status_message_id is None:
        return
    from app.services.server_channels.channel_outbound_service import (
        ChannelOutboundService,
    )

    target = _load_notice_target(session, claim, plan)
    if target is None:
        return
    channel, thread_key = target
    try:
        await ChannelOutboundService.set_status(
            channel=channel,
            thread_key=thread_key,
            message_id=plan.status_message_id,
            text=_INTERRUPTED_NOTICE,
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning(
            "Status repair: could not settle the progress notice for delivery "
            "row %s: %s",
            claim.delivery_id,
            exc,
            exc_info=True,
        )


async def _repair_delivery(
    ctx: RepairContext, claim: _Claim, now: datetime
) -> bool:
    """Read → verify → re-verify → write one abandoned draft row."""
    session = ctx.session
    try:
        plan = _read_and_prepare(ctx, claim, now)
    finally:
        # Closes the read transaction on every exit, early returns included.
        session.rollback()

    if plan is None:
        return False

    if not _seal_abandoned_draft(session, claim, plan):
        return False

    logger.info(
        "Status repair: channel delivery row %s on binding %s sealed as "
        "diverged after %s as an unfinished draft",
        claim.delivery_id,
        claim.binding_id,
        plan.age,
    )

    # Strictly after the commit: the repair is recorded whatever the transport
    # does next, and the await below is the pass's only network round-trip.
    await _settle_stale_notice(session, claim, plan)
    return True


async def repair_channel_deliveries(ctx: RepairContext) -> int:
    """Seal channel turn deliveries abandoned as drafts by a dead stream.

    Registered as Pass D of the status-repair sweep, after Pass B — whose
    session repairs are what make "the session is not streaming" trustworthy
    evidence that the turn behind a draft row is over, *except* for the sessions
    B touched this tick, which it records in ``ctx`` and this pass skips.

    Returns the number of rows sealed; it is also the pass's test surface, since
    the ``TESTING`` gate keeps the scheduler from running under pytest — a test
    builds ``RepairContext(session=db)`` and awaits this directly.
    """
    session = ctx.session
    now = datetime.now(UTC)

    cutoff = now - timedelta(
        minutes=settings.STATUS_REPAIR_CHANNEL_DRAFT_MAX_AGE_MINUTES
    )
    # The age predicate is in SQL here, unlike the other passes: there is one
    # threshold rather than two, and this table grows a row per external message
    # of every channel turn ever streamed — a full scan of the drafts would be
    # the one candidate query in this sweep that does not stay small.
    #
    # ``updated_at`` is a naive column and ``cutoff`` is tz-aware. Postgres
    # resolves that by casting the parameter with the connection's TimeZone,
    # which is UTC here, so the comparison is correct — and it is the same
    # aware-cutoff-against-naive-column shape every other sweep in the codebase
    # uses (``app_data_service``, ``mfa_cleanup_service``, ``routing_trace_service``).
    # Everything read *back* out still goes through ``as_utc``, because Python
    # comparison, unlike Postgres, raises on the mix rather than resolving it.
    claims = [
        _Claim(delivery_id, binding_id, role, as_utc(updated_at))
        for delivery_id, binding_id, role, updated_at in session.exec(
            select(
                ChannelTurnDelivery.id,
                ChannelTurnDelivery.binding_id,
                ChannelTurnDelivery.role,
                ChannelTurnDelivery.updated_at,
            ).where(
                ChannelTurnDelivery.role == CHANNEL_DELIVERY_DRAFT,
                col(ChannelTurnDelivery.updated_at) < cutoff,
            )
        ).all()
    ]
    session.rollback()

    if not claims:
        return 0

    repaired = 0
    for claim in claims:
        try:
            if await _repair_delivery(ctx, claim, now):
                repaired += 1
        except Exception as exc:
            # Per-row isolation, rollback first: the transaction is shared with
            # every later row and every later pass.
            session.rollback()
            logger.error(
                "Status repair: failed to seal delivery row %s: %s",
                claim.delivery_id,
                exc,
                exc_info=True,
            )
    return repaired
