"""``RoutingTraceService.persist`` owns its session — proved, not assumed.

WHY THIS FILE EXISTS, AND WHY IT ESCAPES THE DOMAIN FIXTURE
-----------------------------------------------------------
Read this before touching anything here. It is the entire point of the file.

``persist()`` takes no ``db`` argument and opens its **own** short-lived
session (``with create_session() as db:``). That is not a style choice — it is
the fix for §11a Rule 2, instance 3 of
``docs/plans/auto_routing_tuning_plan.md``:

    "``persist()`` borrowing the caller's session — a failed diagnostic write
    silently discarded the caller's uncommitted work, and an expired-instance
    reload sent ``REPLY_SETUP_FAILED`` to a sender whose message had routed
    fine."

**The domain conftest defeats that property for every other test in this
directory, on purpose.** ``tests/utils/fixtures.py`` lists
``app.services.routing.routing_trace_service.create_session`` in
``CREATE_SESSION_TARGETS_AGENT``, and ``patched_create_sessions`` rewrites it
to a ``NonClosingSessionProxy`` wrapping the *test* session. So under the
standard fixture ``persist()`` is handed **the caller's own session** — the
exact thing the fix eliminated. Every other test in ``tests/api/routing/``
would pass identically against the pre-fix implementation.

That patch target is **not a bug and must not be removed**: without it,
``persist()`` would write to the real application engine in every
agent-domain test and leak rows past the test transaction's rollback. It buys
isolation at the cost of hiding the one property this module exists to pin.

So this file **escapes the patch for the duration of its own tests only**, by
nesting a second ``patch()`` over the same target that hands ``persist()`` a
genuinely independent ``Session`` on its own pooled connection — the
production shape. Precedent for this exact manoeuvre in this same feature:
``tests/unit/test_routing_trace.py``'s
``test_capture_survives_anyio_to_thread_run_sync``, which likewise refuses the
``patch_anyio_to_thread`` fixture because that fixture runs the offload inline
and would make the cross-thread ContextVar property untestable.

    THE HAZARD, NAMED: if a future cleanup "helpfully" folds these tests back
    onto the standard fixture — deleting ``_persist_owns_its_session`` and
    letting the conftest's proxy through — they will keep passing while
    proving nothing, and the safety property goes back to being verified by
    zero tests. That is the situation this file was written to end. The
    escape is load-bearing. If these tests ever look like they need the
    fixture, they are being rewritten into uselessness.

CLEANUP OBLIGATION
------------------
Because the escape is real, a successful ``persist()`` here commits a
``routing_decision`` row on a **separate connection**, outside the test
transaction. The per-test ``ROLLBACK`` cannot undo it, and this domain has no
autouse purge of ``routing_decision`` — one leaked row is visible to every
sibling test's reads, permanently (it would, for instance, inflate
``routing_trace_clear_lifecycle_test.py``'s ``deleted_count`` assertion, and
that file sorts *after* this one).

So ``persisted_rows`` deletes registered ids on its own independent session,
pass or fail — and ids are registered **before** the write, never after.
``persist`` reuses ``trace.trace_id`` as the row's primary key, so the id is
knowable in advance; deleting an id that was never written is a harmless
no-op. Registering afterwards would leave the window between the commit and
the ``append`` uncovered, which is precisely the window a failing assertion
lands in.

FK COLUMNS MUST BE NULL, OR NAME A ROW COMMITTED OUTSIDE THE TEST TRANSACTION
-----------------------------------------------------------------------------
This one bites as a **hang, not a failure**, so it is called out rather than
left to be discovered. ``persist``'s INSERT takes ``FOR KEY SHARE`` on the
parent row of every non-NULL foreign key (``channel_id``, ``user_id``,
``actor_user_id``, ``selected_agent_id``, ``selected_bundle_uuid``). If a test
here points one of those at a row created *inside* the test transaction, the
escaped connection blocks on a transaction that cannot commit until the test
ends — a single-threaded deadlock that stalls the run instead of reporting
anything.

The two tests below stay clear of it deliberately, not accidentally: the
success case leaves every FK NULL, and the failure case points ``channel_id``
at a uuid no row has (which is *how* it fails).

DIRECT-SERVICE EXEMPTION
------------------------
``tests/README.md`` prefers API-driven tests. This one cannot be: the property
under test is about *transaction ownership between two Python objects*, which
no HTTP response can observe. Same class of documented exemption as
``purge_routing_traces`` / ``seed_routing_trace`` in ``tests/utils/routing.py``.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import delete, inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.routing.routing_decision import RoutingDecision
from app.models.users.user import User
from app.services.routing import routing_trace
from app.services.routing.routing_trace import RoutingTrace
from app.services.routing.routing_trace_service import RoutingTraceService

#: The single patch target this module fights with. Kept as a constant so a
#: grep for it lands on the explanation above as well as on
#: ``CREATE_SESSION_TARGETS_AGENT``.
_PERSIST_CREATE_SESSION = "app.services.routing.routing_trace_service.create_session"


# ── The escape ─────────────────────────────────────────────────────────────


class _RecordingSession(Session):
    """A real ``Session`` that remembers which connection it landed on.

    Nothing is stubbed: it begins, commits and closes for real. The recorded
    connection is what lets a test assert *separateness* directly rather than
    inferring it from side effects.
    """

    def __enter__(self) -> Session:  # type: ignore[override]
        super().__enter__()
        # Materialise the connection eagerly so it is observable even after
        # ``persist`` has closed the session.
        self.opened_on = self.connection()
        return self


@contextmanager
def _persist_owns_its_session(db: Session):
    """Undo the conftest's session proxy for ``persist`` only.

    Yields the list of sessions ``persist`` actually opened, so a test can
    assert it opened one at all (distinguishing "the write failed" from "the
    enabled-gate returned early") and that it was not the caller's.

    The engine is taken from the caller's own bind, so this stays pointed at
    the **test** database — the escape restores production *shape* (an
    independent session/connection/transaction), never production *data*.
    """
    engine = db.get_bind().engine
    opened: list[_RecordingSession] = []

    def _factory() -> _RecordingSession:
        session = _RecordingSession(engine)
        opened.append(session)
        return session

    # Nested over the autouse conftest patch: this wins for the duration and
    # the proxy is restored on exit.
    try:
        with patch(_PERSIST_CREATE_SESSION, _factory):
            yield opened
    finally:
        # These are real pooled connections, unlike the conftest proxy's
        # no-op ``close``. ``persist``'s own ``with`` closes them on both the
        # success and the failure path, so this is belt-and-braces against a
        # future refactor leaking one and quietly draining the pool.
        for session in opened:
            session.close()


@contextmanager
def _swallowed_failures():
    """Collect what ``persist``'s never-raises guard logged instead of raising.

    A handler attached straight to the module logger, not ``caplog``, and the
    logger is force-enabled for the duration. Both are necessary, and neither
    is obvious:

    - ``caplog`` installs its handler on the **root** logger, so it only sees
      what propagates there.
    - Nothing propagates, because the session-scoped ``setup_db`` fixture runs
      Alembic, and ``alembic.config.Config`` calls ``logging.config.fileConfig``
      with its default ``disable_existing_loggers=True``. Every logger created
      before that point — including this module's — comes back with
      ``disabled = True`` for the rest of the session.

    An assertion written the natural way (``caplog.text``) is therefore
    **vacuously true against an empty string** and proves nothing. That is the
    same failure this file exists to end, one level down, so it is written out
    here rather than left as a shrug.

    Records are kept whole so a test can assert on the *exception type* the
    guard swallowed, not merely on a message string.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger("app.services.routing.routing_trace_service")
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


@pytest.fixture
def persisted_rows(db: Session):
    """Ids written outside the test transaction, removed however the test ends."""
    ids: list[uuid.UUID] = []
    yield ids
    if not ids:
        return
    engine = db.get_bind().engine
    with Session(engine) as cleanup:
        cleanup.execute(delete(RoutingDecision).where(RoutingDecision.id.in_(ids)))
        cleanup.commit()


# ── Caller-side scaffolding ────────────────────────────────────────────────


def _anchor_user(db: Session) -> User:
    """A committed, fully loaded ORM instance held by the caller.

    ``persist``'s commit must not expire this. An expired instance is what
    produced instance 3's user-visible symptom: the next attribute read fired
    a lazy reload on a transaction that was no longer valid, and the sender
    got ``REPLY_SETUP_FAILED`` for a message that had routed fine.
    """
    user = User(
        email=f"anchor-{uuid.uuid4().hex[:12]}@persist-ownership.test",
        hashed_password="not-a-real-hash",
    )
    db.add(user)
    db.commit()
    db.refresh(user)  # commit expired it; reload so "not expired" means something
    return user


def _uncommitted_work(db: Session) -> User:
    """Work the caller is holding but has not flushed — its transaction in flight."""
    pending = User(
        email=f"inflight-{uuid.uuid4().hex[:12]}@persist-ownership.test",
        hashed_password="not-a-real-hash",
    )
    db.add(pending)
    assert pending in db.new, "precondition: the caller's work must start unflushed"
    return pending


def _closed_trace(*, channel_id: uuid.UUID | None = None) -> RoutingTrace:
    """A settled trace, ready to persist. Vocabulary from the constants, not
    literals, so a rename in ``routing_trace`` breaks this loudly."""
    with RoutingTrace.capture(
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        channel_id=channel_id,
        message="does persist move the caller's transaction?",
    ) as trace:
        trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
    return trace


# ── 1. A successful persist does not move the caller's transaction ─────────


def test_persist_neither_commits_nor_expires_the_callers_transaction(
    db: Session, persisted_rows: list[uuid.UUID]
) -> None:
    """§11a Rule 2 instance 3, positive half.

    With the conftest proxy escaped, ``persist`` runs on its own session and
    the caller's in-flight work is untouched: still unflushed, still
    committable, and its loaded instances still valid.

    Every assertion below fails under the standard fixture — that is the
    point. Borrowing the caller's session would flush the pending row into
    ``persist``'s commit and expire the anchor.
    """
    anchor = _anchor_user(db)
    anchor_email = anchor.email
    pending = _uncommitted_work(db)
    pending_email = pending.email

    trace = _closed_trace()
    # Registered BEFORE the write, not after: see CLEANUP OBLIGATION above.
    persisted_rows.append(uuid.UUID(trace.trace_id))

    with _persist_owns_its_session(db) as opened:
        row_id = RoutingTraceService.persist(trace)

    assert row_id is not None

    # The mechanism itself, asserted directly rather than inferred: a
    # different Session object, on a different connection, hence a different
    # transaction. Nothing it does can reach the caller's.
    assert len(opened) == 1
    assert opened[0] is not db
    assert opened[0].opened_on is not db.connection()

    # (a) The caller's uncommitted work is still uncommitted.
    assert pending in db.new, "persist flushed/committed the caller's pending work"

    # (b) The caller's loaded instances were not expired by persist's commit.
    assert not sa_inspect(anchor).expired, "persist's commit expired a caller instance"
    assert anchor.email == anchor_email

    # (c) The caller's transaction is still its own to finish.
    db.commit()
    assert db.get(User, pending.id) is not None
    assert pending.email == pending_email

    # (d) And persist's transaction really committed, on its own. Read the row
    #     back from a THIRD connection: the caller's transaction is still open
    #     and uncommitted, so a borrowed session would have left this row
    #     invisible here. This is the most direct statement of the property —
    #     ``row_id is not None`` only proves persist *returned* an id.
    with Session(db.get_bind().engine) as probe:
        assert probe.get(RoutingDecision, row_id) is not None


# ── 2. A FAILING persist does not damage the caller either ─────────────────


def test_failed_persist_leaves_the_caller_intact_and_raises_nothing(
    db: Session, persisted_rows: list[uuid.UUID]
) -> None:
    """§11a Rule 2 instance 3, the half that actually bit.

    The failure is a real database error, not a mock: the trace carries a
    ``channel_id`` that no ``server_channel`` row has, so the INSERT trips the
    foreign key. That is the shape of the original incident — a diagnostic
    write that fails for a reason nobody anticipated.

    Three things must hold at once: nothing escapes ``persist``'s never-raises
    guard, the caller's uncommitted work survives, and the caller's session is
    still usable. Under a borrowed session none of the three hold — the
    ``IntegrityError`` would poison the caller's session and its next
    ``commit()`` would raise ``PendingRollbackError`` on work that had nothing
    to do with the diagnostic.
    """
    anchor = _anchor_user(db)
    pending = _uncommitted_work(db)

    trace = _closed_trace(channel_id=uuid.uuid4())  # FK points at nothing
    # The FK is expected to stop the write, so this should have nothing to
    # delete — registered anyway, because "the write cannot succeed" is the
    # assumption under test, not a hygiene guarantee to build cleanup on.
    persisted_rows.append(uuid.UUID(trace.trace_id))

    with _swallowed_failures() as logged:
        with _persist_owns_its_session(db) as opened:
            # No pytest.raises wrapper: an escaping exception IS the failure.
            row_id = RoutingTraceService.persist(trace)

    # It really tried and really failed — not an early return from the
    # enabled-gate, which would also produce ``None``.
    assert len(opened) == 1, "persist never opened a session; wrong failure mode"
    assert row_id is None
    # And it failed at the database, on the write, for the reason intended —
    # not on some earlier expression that would leave the INSERT untried.
    assert [r.getMessage() for r in logged] == ["Failed to persist routing trace"]
    assert logged[0].exc_info is not None
    assert issubclass(logged[0].exc_info[0], IntegrityError)

    # (a) The caller's uncommitted work survived the failed diagnostic write.
    assert pending in db.new
    # (b) No expiry, no lazy reload on the way out.
    assert not sa_inspect(anchor).expired
    # (c) The caller's session is not poisoned: it can still commit its own
    #     work, which is what "the diagnostic did not discard it" means.
    db.commit()
    assert db.get(User, pending.id) is not None

    # And nothing half-written survived on the real connection, so there is
    # nothing for ``persisted_rows`` to clean up in this test.
    engine = db.get_bind().engine
    with Session(engine) as probe:
        assert probe.get(RoutingDecision, uuid.UUID(trace.trace_id)) is None
