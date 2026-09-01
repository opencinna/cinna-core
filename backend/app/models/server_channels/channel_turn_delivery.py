"""ChannelTurnDelivery — what a turn actually put on the reader's screen.

One row per **external message** a channel turn wrote: the rolling draft that
the streaming relay rewrites, each slice it *sealed* off when the draft grew
past what one transport message can hold, and the final text the completion
handler settles. It is the durable half of a mechanism whose live half
(``channel_stream_relay``) is deliberately in-memory: the relay knows what is
standing in the thread only for as long as the process running that turn does,
and this table is what survives it.

What it buys, concretely:

* **Idempotency at completion.** ``STREAM_COMPLETED`` fires once per LLM batch
  and can arrive twice for the same batch; a ``final`` row for a batch's agent
  message is the record that says "this one is already answered".
* **An observable divergence check.** The settled reply is the relay's own
  accumulated text, not the stored ``SessionMessage`` content — decided with
  the user, and documented in ``server_channels_tech.md`` ("The finalize
  divergence policy"). The sealed rows' ``visible_char_end`` +
  ``content_sha256`` turn "the canonical text still starts with what we have
  already shown" from a silent assumption into a check that can fire.
* **Crash knowledge.** Sealed messages are final — the relay advances past
  them and never offers that text again — so a process that dies mid-turn
  leaves messages standing in the thread that nothing else records.

Boundary writes only
--------------------

Rows are written when a **fresh draft message** appears (the turn's first, and
the one that opens after each seal), when a slice is **sealed**, and at the
**final** delivery. The rolling ~3-second draft patches write **nothing**,
ever: they rewrite a message that already has its row, they are ephemeral by
design, and persisting them would turn one row per turn into hundreds. The
grain is one row per external message, never one per flush. Every write is
best-effort — a lost ledger write costs observability, never a reply.

Three roles, not four
---------------------

``draft`` | ``sealed`` | ``final``. The design sketch also carried a
``notice`` role for the status notice adopted by ``adopt_status_notice``, and
it was dropped for **simplicity**: three roles cover every message this
ledger actually needs to describe, and a fourth bought nothing. (An earlier
argument for dropping it — that a ``NOT NULL`` FK made such a row unwritable
before the binding had a session or an agent message — dissolved the moment
``session_message_id`` became nullable, and is not the reason; do not
resurrect the role on the grounds that obstacle is gone.)
``ChannelThreadBinding.status_message_id`` alone carries the notice, exactly
as it did before this table existed, and remains the **only thing consulted**
for "which message do I patch next" — the ``draft`` row here mirrors it for
observability, and nothing ever compares the two or reconciles them.

Why ``session_message_id`` is nullable
--------------------------------------

It is the one deviation from the plan's schema, and it is forced by *when*
turn identity becomes knowable. The relay seals mid-stream and holds only
plain ids — ``session_id``, ``binding_id``, ``channel_id`` — never the agent
``SessionMessage`` the batch is writing; the only place that id is handed over
is the terminal stream event's meta, which arrives at the *end* of the batch
(``channel_outbound_service.AGENT_MESSAGE_ID_META_KEY``).

The alternative was for the relay to *infer* the row — "the newest agent
message in this session", or "the one whose metadata says it is streaming" —
which is exactly the inference this whole feature exists to delete. So a
boundary write records what it honestly knows (the binding, the part, the
external message) and leaves attribution ``NULL``; ``handle_stream_completed``
**adopts** the turn's pending rows once the emitter has named the row. Every
attributed id in this table therefore came from the emitter, never from a
lookup.

Consequences worth knowing:

* The unique constraint is unenforced while a row is pending — Postgres treats
  ``NULL`` as distinct — and enforced from adoption on, which is when
  ``part_index`` becomes meaningful. Adoption renumbers the rows it takes, so
  the relay's own indexes are provisional.
* A turn that seals and is then **interrupted** (or errors) never reaches a
  completion — but its rows do NOT wait for the next turn: the interrupt and
  error handlers close them out themselves
  (``_close_out_unsettled_ledger``, using the event's id as a key only,
  ``write_final=False``). What still leaves rows pending for the *next*
  completion to adopt is a turn that ends with no terminal event at all — a
  process crash mid-turn — and that adoption is a mis-attribution in the
  ledger, bounded to "these messages are standing in this thread", costs
  nothing outside this table, and can surface as a spurious ``diverged``
  (the documented operational rule: rule out a crash on the binding before
  believing that warning).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# Role values. Plain constants (not an Enum) to match the codebase's
# status-string convention and keep the columns bare varchars.

#: The message currently being rewritten in place. Mirrors
#: ``ChannelThreadBinding.status_message_id``, which stays authoritative.
CHANNEL_DELIVERY_DRAFT = "draft"
#: A slice cut off the front of the draft and left standing. Final: the relay
#: advances past it and never sends that text again.
CHANNEL_DELIVERY_SEALED = "sealed"
#: The last message of the turn — the draft the completion handler settled, or
#: a fresh message where there was no draft to settle.
CHANNEL_DELIVERY_FINAL = "final"

CHANNEL_DELIVERY_ROLES = (
    CHANNEL_DELIVERY_DRAFT,
    CHANNEL_DELIVERY_SEALED,
    CHANNEL_DELIVERY_FINAL,
)

#: The transport confirmed the write.
CHANNEL_DELIVERY_DELIVERED = "delivered"
#: A boundary delivery for this row failed. Never blocks the turn: the relay
#: retries a failed seal on its next flush, and the text is still in the tail
#: if the turn ends first.
CHANNEL_DELIVERY_FAILED = "failed"
#: The finalized canonical text no longer starts with what this row recorded
#: as delivered. The tail is still delivered exactly as it would have been —
#: this is a marker, not a policy change.
CHANNEL_DELIVERY_DIVERGED = "diverged"

CHANNEL_DELIVERY_STATUSES = (
    CHANNEL_DELIVERY_DELIVERED,
    CHANNEL_DELIVERY_FAILED,
    CHANNEL_DELIVERY_DIVERGED,
)


class ChannelTurnDelivery(SQLModel, table=True):
    """One external message a channel turn wrote. See the module docstring."""

    __tablename__ = "channel_turn_delivery"
    __table_args__ = (
        # Enforced from adoption on; see the module docstring on why pending
        # rows escape it.
        UniqueConstraint(
            "session_message_id",
            "part_index",
            name="uq_channel_turn_delivery_part",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Which thread these messages are standing in. Always known at write time —
    # it is what the relay holds — and the key adoption looks pending rows up
    # by. Cascades: a thread whose binding is gone has no ledger to keep.
    binding_id: uuid.UUID = Field(
        foreign_key="channel_thread_binding.id", ondelete="CASCADE", index=True
    )
    # The agent ``SessionMessage`` this delivery belongs to — the FK is to the
    # ``message`` table, which is what ``SessionMessage`` is called. ``NULL``
    # means "written at a boundary, not yet attributed"; see the module
    # docstring. Never filled in from a lookup, only from the id the terminal
    # stream event carried.
    session_message_id: uuid.UUID | None = Field(
        default=None, foreign_key="message.id", ondelete="CASCADE", index=True
    )
    # Order of this message within the turn: 0, 1, 2… Assigned provisionally by
    # the relay and renumbered at adoption, so it is dense and ordered only for
    # attributed rows.
    part_index: int = Field(default=0)
    role: str = Field(default=CHANNEL_DELIVERY_DRAFT, max_length=16)
    # Transport-native message id — opaque here, and never interpreted. NULL
    # where the transport delivered without telling us which message it wrote:
    # an honest unknown beats a fabricated id, since the only consumer of this
    # column is a human diagnosing a thread.
    external_message_id: str | None = Field(default=None, max_length=255)
    # How far into the turn's **visible** text this row's delivery reached.
    # Visible space is the relay's ``_visible()`` — the agent's markdown with
    # the control tags finalize strips already removed — and stripping is
    # additive across seal cuts, which is what makes the offset stable.
    #
    # On a ``sealed`` row this is the cumulative end of the relay's delivered
    # prefix **within its LLM batch**; on a ``final`` row it is the length of
    # the whole finalized canonical answer of that same batch. Batch-scoped on
    # both sides on purpose — ``STREAM_COMPLETED`` fires per batch and the
    # answer a prefix is checked against is that batch's own ``SessionMessage``,
    # so a turn-cumulative offset would report a divergence on every
    # multi-batch turn. Comparing the two IS the divergence check, and both
    # sides go through ``delivered_prefix_key`` / the same ``_visible`` so a
    # leading-whitespace reply cannot shift one against the other.
    visible_char_end: int | None = Field(default=None)
    # sha256 (hex) of the visible text ``visible_char_end`` measures. Same
    # split of meaning between ``sealed`` and ``final`` rows.
    content_sha256: str | None = Field(default=None, max_length=64)
    status: str = Field(default=CHANNEL_DELIVERY_DELIVERED, max_length=16)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
