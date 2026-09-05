"""The per-tick context every repair pass is handed.

**Why the passes need to talk to each other at all.** Each pass verifies live
evidence before it writes — that is the sweep's golden rule — but two of the
passes *manufacture* evidence a later pass would otherwise read as the truth:

* Pass A repairs an environment to ``running`` and emits
  ``ENVIRONMENT_ACTIVATED``. That event drains the environment's
  ``pending_stream`` sessions, but the drain is **fire-and-forget and slow**:
  ``event_service._call_backend_handlers`` dispatches handlers through
  ``create_task_with_error_logging``, so ``await emit_event(...)`` returns
  before ``handle_environment_activated`` has run at all, and even once it
  does, neither it nor ``initiate_stream``'s env-running branch writes the
  session row — the status only becomes ``running`` when the ``STREAM_STARTED``
  handler lands, several task hops and a credential refresh later. Milliseconds
  after Pass A's emit, a drained session therefore still looks *exactly* like an
  abandoned one to Pass B.
* Pass B clears a long-running session's ``interaction_status`` **without
  cancelling the stream behind it**. Pass D's cross-process evidence that a
  channel turn is over is that same column, so a turn that legitimately exceeds
  the stream threshold would be cleared by B and then sealed by D in the same
  tick — while the relay is still patching the message D deletes.

Neither hazard is visible from inside the pass that suffers it: the row it reads
is genuinely in the state it reads. The only thing that distinguishes "abandoned"
from "another pass just moved this, this tick" is knowing what the tick has
already done. That is what this object carries — nothing else. It is a
scratchpad for one sweep, never persisted, and always empty at the start of a
tick.

**Why a context object rather than return values.** A pass returns a count, and
threading a second return value through would make every pass's signature
describe what the *next* pass happens to need. The context keeps each pass
signature identical (``async def pass(ctx) -> int``), keeps the passes directly
invocable for tests — a test builds ``RepairContext(session=db)`` and calls one
pass against the test transaction, which is the only test surface the
``TESTING`` gate leaves — and makes the cross-pass coupling a named, greppable
thing instead of an ordering comment nobody can verify.
"""
from dataclasses import dataclass, field
from uuid import UUID

from sqlmodel import Session


@dataclass
class RepairContext:
    """One sweep's shared scratchpad. Created per tick, discarded after it."""

    #: The leader session every pass reads and writes through. All passes share
    #: it (and its pinned connection), which is why each of them rolls back
    #: before handing control on.
    session: Session

    #: Environments Pass A repaired to ``running`` this tick. Each one emitted
    #: ``ENVIRONMENT_ACTIVATED``, so its ``pending_stream`` sessions are already
    #: being drained through the ordinary path — asynchronously, and without
    #: having written the session row yet.
    drained_environment_ids: set[UUID] = field(default_factory=set)

    #: Sessions this tick has set moving or settled: the ones Pass B re-drained
    #: or cleared, and the ones it left alone *because* Pass A's drain has them.
    #: Pass D must not read any of these rows' ``interaction_status`` as
    #: evidence about a turn — the value it would read was written (or withheld)
    #: by this same sweep, moments ago.
    sessions_in_motion: set[UUID] = field(default_factory=set)

    def note_drained_environment(self, env_id: UUID) -> None:
        """Record that Pass A started this environment's session drain."""
        self.drained_environment_ids.add(env_id)

    def is_environment_draining(self, env_id: UUID | None) -> bool:
        """True if Pass A set this environment's sessions moving this tick."""
        return env_id is not None and env_id in self.drained_environment_ids

    def note_session_in_motion(self, session_id: UUID) -> None:
        """Record that this sweep moved (or is about to move) this session."""
        self.sessions_in_motion.add(session_id)

    def is_session_in_motion(self, session_id: UUID | None) -> bool:
        """True if this sweep already touched this session's stream state."""
        return session_id is not None and session_id in self.sessions_in_motion
