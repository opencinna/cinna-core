"""``ChannelOutboundService._deliver`` must not destroy the failure it records.

The twin of ``test_channel_reply_instrumentation.py``, and the worse half of
the pair: that file covers the *notice* path, this one covers the
**agent-reply** path, which carries the traffic. Same §11a Rule 2 defect
shape — "the test for a new instrumentation point is not 'is the recorder
guarded' but 'can anything in the caller's argument list raise'", proved by
firing a poison object rather than by reading the code (see
``docs/plans/auto_routing_tuning_plan.md`` §11a).

``_deliver``'s ``except`` held **five** unguarded expressions where ``_reply``
held two, and one more consequence: besides the debug-buffer record, the
handler also calls ``_record_error``. A raise anywhere in the handler
therefore lost the delivery diagnosis in *both* places an operator looks — the
debug panel and the binding row — and handed the caller an unrelated database
error in its place.

What each test below pins, and why that expression was exposed:

* ``channel.id`` — read inside the ``except`` (twice) and, worse, in the
  **success** branch where there was no ``try`` at all. Every path into
  ``_deliver`` arrives after a ``db.commit()`` that expired the instance, so
  the read is a lazy reload; reloading a concurrently deleted row raises
  ``ObjectDeletedError``. ``_Vanished`` stands in for that (a unit test has no
  session to expire).
* ``binding.thread_key`` — the same reload hazard on the binding.
* ``f"Delivery failed: {exc}"`` and ``str(exc)`` — an exception with a raising
  ``__str__``, one of the five shapes Rule 2 names.
* ``logger.warning(..., exc)`` — lazy interpolation. ``logging`` swallows its
  own formatting errors in production while pytest's ``LogCaptureHandler``
  re-raises them, so a raw ``exc`` here is a guard whose correctness depends
  on which handler is installed. Pre-formatted through ``_log_detail``.
* ``_record_error`` itself — it reads ``binding.status`` and rolls back a
  session, either of which can raise from inside the handler. Covered by its
  own tests at the bottom of this file, including the one thing the fix
  deliberately does **not** solve.

Pure logic with fakes: no DB, no ``TestClient``, no HTTP.

A note on how failures are pinned below: this file used to lean on
``caplog.text`` substring checks throughout. Those are **vacuous** once the
session-scoped ``setup_db`` fixture has run Alembic — ``alembic.config.Config``
calls ``logging.config.fileConfig`` with its default
``disable_existing_loggers=True``, which leaves every application logger
(this module's included) permanently ``disabled`` for the rest of the test
session (see ``tests/README.md``, "caplog assertions are vacuous for the rest
of the session"). Every assertion below is instead made either directly on
the object flowing through the code (``_spy_log_detail``,
``_spy_binding_thread_key``) or, for the one property that genuinely IS a log
line, on a real ``LogRecord`` captured off an explicitly un-disabled logger
(``_unswallowed_channel_debug_key_warnings``) — the same discrimination
``test_channel_reply_instrumentation.py`` uses, and for the same reason.
"""
from __future__ import annotations

import asyncio
import logging
import types
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy.exc import OperationalError, PendingRollbackError

from app.models import CHANNEL_BINDING_FAILED
from app.services.server_channels import channel_inbound_service as _cis
from app.services.server_channels import channel_outbound_service as _outbound_svc
from app.services.server_channels.adapters.base import ChannelError
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_REPLIED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)
from app.services.server_channels.channel_outbound_service import (
    ChannelOutboundService,
)

_MODULE = "app.services.server_channels.channel_outbound_service"
_INBOUND_MODULE = "app.services.server_channels.channel_inbound_service"


# ── Poison objects ────────────────────────────────────────────────────────────


class _Vanished(Exception):
    """Stands in for ``ObjectDeletedError`` on an expired-instance reload."""


class _PoisonStr(Exception):
    """An exception that cannot be turned into a string.

    ``status_code`` is present so the tests can assert what a *total*
    describer still salvages from it.
    """

    status_code = 503

    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


class _LiveChannel:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.channel_type = "google_chat"


class _ChannelLostMidSend:
    """Readable up to the send, unreadable after it — the discriminating case.

    A fully poisoned channel raises on ``channel_type`` at the top of the
    ``try``, before the adapter is reached, so there is no delivery failure
    left to destroy. This shape reproduces the sequence that matters.
    """

    channel_type = "google_chat"

    def __getattr__(self, name: str):  # noqa: ANN401 — deliberate catch-all
        raise _Vanished(f"could not refresh {name}: row is gone")


class _LiveBinding:
    def __init__(self, status: str = "active", last_error: str | None = None) -> None:
        self.thread_key = "spaces/AAA/threads/BBB"
        self.status = status
        self.last_error = last_error
        self.server_channel_id = uuid.uuid4()
        # Present on the real model, and read by ``_binding_thread_key`` for
        # the polled transports' reply context (settled decision §2.7). A fake
        # missing it would raise AttributeError inside that helper's guard and
        # be mistaken for the vanished-row case ``_BindingVanished`` exists to
        # cover.
        self.last_external_message_id = None
        # Read by ``_binding_status_message_id`` on every status-notice path,
        # for the same reason: a fake missing it raises AttributeError inside
        # that helper's guard and looks like a vanished row.
        self.status_message_id: str | None = None


class _BindingVanished:
    """A binding whose row is gone: every attribute read is a failed reload."""

    def __getattr__(self, name: str):  # noqa: ANN401 — deliberate catch-all
        raise _Vanished(f"could not refresh {name}: row is gone")


class _Adapter:
    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error
        self.calls: list[tuple[str, str]] = []
        self.attempts = 0

    async def send_message(self, channel, thread_key: str, text: str) -> None:
        self.attempts += 1
        if self._error is not None:
            raise self._error
        self.calls.append((thread_key, text))


class _FakeDB:
    """Just enough session to exercise ``_record_error``'s write path."""

    def __init__(
        self,
        commit_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
    ) -> None:
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error


# ── Fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_buffer():
    """The buffer is process-global class state — isolate every test."""
    ChannelDebugBuffer.reset()
    yield
    ChannelDebugBuffer.reset()


def _deliver(channel, binding, adapter, monkeypatch, *, db=None, text="hi") -> bool:
    """Drive ``_deliver`` to completion with ``adapter`` installed."""
    monkeypatch.setattr(_MODULE + ".get_adapter", lambda _type: adapter)
    return asyncio.run(
        ChannelOutboundService._deliver(
            db=db if db is not None else _FakeDB(),
            channel=channel,
            binding=binding,
            text=text,
        )
    )


def _spy_log_detail(monkeypatch) -> list[tuple[BaseException, str]]:
    """Capture every ``(exc, result)`` pair the ``except`` branch hands to
    ``_log_detail``, while still running the real implementation.

    This replaces a ``caplog.text`` substring check. ``_log_detail`` is the
    one place in ``_deliver`` that decides what an exception looks like once
    it reaches the log and the binding row — total by construction, per its
    own docstring — so spying on it directly proves *the surviving exception
    itself* reached that decision point, and what it decided, without going
    anywhere near ``logging``. That distinction matters here specifically:
    the session-scoped ``setup_db`` fixture runs Alembic before any test, and
    ``alembic.config.Config`` calls ``logging.config.fileConfig`` with its
    default ``disable_existing_loggers=True``, which leaves every
    application logger — this module's included — permanently ``disabled``
    for the rest of the session (see ``tests/README.md``, "caplog assertions
    are vacuous for the rest of the session"). Any assertion routed through
    ``caplog.text`` is comparing against ``''`` in that scope and passes
    whether or not the call ever happened.

    ``_log_detail`` lives in ``channel_inbound_service`` and ``_deliver``
    imports it *locally*, on every call (to avoid a circular import — see
    ``_deliver``'s own docstring), so patching the attribute on the
    ``channel_inbound_service`` module object here is picked up by that
    fresh ``from ... import`` the very next time ``_deliver`` runs.
    """
    calls: list[tuple[BaseException, str]] = []
    original = _cis._log_detail

    def _spy(exc: BaseException) -> str:
        result = original(exc)
        calls.append((exc, result))
        return result

    monkeypatch.setattr(_cis, "_log_detail", _spy)
    return calls


def _spy_binding_thread_key(monkeypatch) -> list[tuple[object, str | None]]:
    """Capture every ``(binding, result)`` pair ``_binding_thread_key`` produces.

    Replaces a ``caplog.text`` substring check on "declined to send because
    the thread key could not be read". ``_binding_thread_key`` is total by
    construction (see its own docstring in ``channel_outbound_service``) — it
    never lets the reload failure escape, it turns it into ``None`` — so
    spying on it directly proves ``_deliver`` actually reached that decision
    and got back the "nothing to address this to" answer, without going
    anywhere near ``logging`` (vacuous in this scope; see ``_spy_log_detail``
    above).

    Unlike ``_log_detail``, ``_binding_thread_key`` is referenced as a bare
    module-global name inside ``_deliver`` (no local import), so patching the
    attribute on the ``channel_outbound_service`` module object is enough —
    the name lookup at call time finds the spy.
    """
    calls: list[tuple[object, str | None]] = []
    original = _outbound_svc._binding_thread_key

    def _spy(binding, channel=None):
        # ``channel`` is the reply-context argument the polled transports
        # added (settled decision §2.7); mirrored here so the spy keeps the
        # helper's real signature. Forwarded unchanged — the spy observes, it
        # never decides.
        result = original(binding, channel)
        calls.append((binding, result))
        return result

    monkeypatch.setattr(_outbound_svc, "_binding_thread_key", _spy)
    return calls


@contextmanager
def _unswallowed_channel_debug_key_warnings():
    """Real ``LogRecord``s from ``channel_inbound_service``'s own logger.

    Used only for the one property in this file that genuinely IS a log
    line — "the drop of the unfileable debug event is itself observable
    rather than silent" (Rule 1's half of the bargain). An empty debug buffer
    alone cannot distinguish "no event was ever generated" from "one was
    generated and dropped", so the log is the only surviving signal of the
    drop.

    ``_debug_channel_key`` lives in ``channel_inbound_service``, not this
    module (``_deliver`` imports it locally rather than duplicating it — see
    its docstring), and logs on that module's own logger. ``caplog`` cannot
    answer this in this scope (see ``_spy_log_detail``), so this attaches a
    handler straight to the module logger and force-enables it for the
    duration instead — the same manoeuvre as
    ``test_channel_reply_instrumentation.py``'s ``_unswallowed_module_warnings``
    and ``tests/api/routing/routing_persist_session_ownership_test.py``'s
    ``_swallowed_failures``.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger(_INBOUND_MODULE)
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


# ── The regressions this file exists for ──────────────────────────────────────


def test_expired_channel_does_not_replace_the_delivery_failure(monkeypatch) -> None:
    """The original send failure survives an unreadable ``channel.id``.

    Before the fix, ``channel_id=channel.id`` in the ``except`` raised on the
    lazy reload, *in place of* the send failure: the caller got a database
    error and the diagnosis vanished. ``_deliver`` is best-effort by contract,
    so "intact" means it returns ``False`` and the failure reaches the log.
    """
    original = RuntimeError("google chat rejected the post")
    adapter = _Adapter(error=original)
    detail_calls = _spy_log_detail(monkeypatch)

    with _unswallowed_channel_debug_key_warnings() as records:
        delivered = _deliver(_ChannelLostMidSend(), _LiveBinding(), adapter, monkeypatch)

    assert delivered is False
    assert adapter.attempts == 1, "the send must actually have been attempted"
    # The failure that reached the log is the ORIGINAL send failure, not a
    # reload error that displaced it — proved directly on what reached
    # _log_detail rather than by matching a substring in caplog.text (see
    # _spy_log_detail for why that check is unconditionally vacuous here).
    assert detail_calls, "the except-branch never reached _log_detail"
    assert detail_calls[-1][0] is original
    # Rule 1's half of the bargain: the drop of the unfileable debug event is
    # itself observable rather than silent. See
    # _unswallowed_channel_debug_key_warnings for why this one property is
    # checked via a real LogRecord instead.
    assert any(
        "Could not identify a channel for the debug buffer" in r.getMessage()
        for r in records
    )
    # Nowhere to file it, so it was not filed anywhere.
    assert ChannelDebugBuffer._buffers == {}


def test_expired_binding_does_not_replace_the_delivery_failure(monkeypatch) -> None:
    """The second exposed reload: ``binding.thread_key``.

    Unreadable in the ``except`` *and* unreadable at the send, so there is
    nothing to address the message to — ``_deliver`` declines rather than
    posting to a null thread. Proved directly on ``_binding_thread_key``'s
    own return value (total by construction) rather than on a caplog
    substring — see ``_spy_binding_thread_key``.
    """
    adapter = _Adapter(error=RuntimeError("nope"))
    thread_key_calls = _spy_binding_thread_key(monkeypatch)

    delivered = _deliver(_LiveChannel(), _BindingVanished(), adapter, monkeypatch)

    assert delivered is False
    assert adapter.attempts == 0
    assert thread_key_calls, "_binding_thread_key was never reached"
    # None is the "nothing to address this to" answer, not a raise escaping.
    assert thread_key_calls[-1][1] is None


def test_exception_with_a_raising_str_does_not_break_the_handler(monkeypatch) -> None:
    """A poison ``__str__`` is described, not detonated — and still recorded.

    Three expressions called it: the f-string summary, ``str(exc)`` in the
    ``_record_error`` call, and the log's lazy interpolation. All three ran
    before the callee's own guard could apply.
    """
    channel = _LiveChannel()
    adapter = _Adapter(error=_PoisonStr())
    detail_calls = _spy_log_detail(monkeypatch)

    delivered = _deliver(channel, _LiveBinding(), adapter, monkeypatch)

    assert delivered is False
    events = ChannelDebugBuffer.list_events(channel.id)
    assert [e.kind for e in events] == [DEBUG_SEND_FAILED]
    summary = events[0].summary
    assert summary.startswith("Delivery failed: ")
    # Better than merely surviving: ``describe_exception`` never calls
    # ``str(exc)``, so the admin still gets a usable line out of the poison.
    assert "_PoisonStr" in summary
    assert "HTTP 503" in summary
    # And the log survives it rather than being the thing that raises —
    # proved directly on _log_detail's own return value (total by
    # construction), not via caplog.text (see _spy_log_detail).
    assert detail_calls, "the except-branch never reached _log_detail"
    assert isinstance(detail_calls[-1][0], _PoisonStr)
    assert detail_calls[-1][1] == "<unprintable _PoisonStr>"


def test_send_failure_summary_carries_the_diagnosis_not_the_message(
    monkeypatch,
) -> None:
    """Type and HTTP status survive into the buffer; the message body does not.

    Pins the choice of ``describe_exception`` over an f-string. An adapter's
    HTTP error echoes the request it just made — a request carrying the
    channel's service-account credentials — and the debug buffer is a
    superuser read surface. A refactor back to ``f"{exc}"`` fails here.
    """

    class _Rejected(Exception):
        status_code = 403

    channel = _LiveChannel()
    adapter = _Adapter(error=_Rejected("service account key AKIA-SECRET denied"))

    assert _deliver(channel, _LiveBinding(), adapter, monkeypatch) is False

    summary = ChannelDebugBuffer.list_events(channel.id)[0].summary
    assert "_Rejected" in summary
    assert "HTTP 403" in summary
    assert "AKIA-SECRET" not in summary


def test_binding_row_and_log_keep_the_full_operator_diagnosis(monkeypatch) -> None:
    """The other half of the audience split.

    The buffer is de-tainted; the application log and ``binding.last_error``
    are operator surfaces where the adapter's actual complaint is the whole
    diagnosis. Dropping it in all three places would leave nobody able to
    answer why a reply failed.
    """
    binding = _LiveBinding()
    original = RuntimeError("permission denied for space X")
    adapter = _Adapter(error=original)
    detail_calls = _spy_log_detail(monkeypatch)

    assert _deliver(_LiveChannel(), binding, adapter, monkeypatch) is False

    # The log and the binding row are handed the same detail, computed from
    # the ORIGINAL exception — proved directly on _log_detail's inputs and
    # output rather than on caplog.text (see _spy_log_detail).
    assert detail_calls, "the except-branch never reached _log_detail"
    assert detail_calls[-1][0] is original
    assert binding.last_error is not None
    assert "permission denied for space X" in binding.last_error


def test_both_poisons_at_once_still_returns_quietly(monkeypatch) -> None:
    """Neither expression can raise, so both failing at once is survivable."""
    adapter = _Adapter(error=_PoisonStr())
    detail_calls = _spy_log_detail(monkeypatch)

    delivered = _deliver(_ChannelLostMidSend(), _LiveBinding(), adapter, monkeypatch)

    assert delivered is False
    assert adapter.attempts == 1
    # Nothing was filed under a key no panel reads.
    assert ChannelDebugBuffer._buffers == {}
    # The log survives an unprintable exception rather than being the thing
    # that raises — proved directly on _log_detail's own return value
    # (total by construction), not via caplog.text (see _spy_log_detail).
    assert detail_calls, "the except-branch never reached _log_detail"
    assert isinstance(detail_calls[-1][0], _PoisonStr)
    assert detail_calls[-1][1] == "<unprintable _PoisonStr>"


def test_successful_delivery_survives_an_unreadable_channel_id(monkeypatch) -> None:
    """The success branch had no ``try`` over it at all.

    ``channel_id=channel.id`` on a delivery that had already **succeeded**
    raised out of ``_deliver`` and turned a delivered reply into an error for
    the caller — a debug record breaking the thing it was observing, on the
    path where nothing had gone wrong.
    """
    adapter = _Adapter()

    assert _deliver(_ChannelLostMidSend(), _LiveBinding(), adapter, monkeypatch) is True
    assert adapter.attempts == 1
    assert ChannelDebugBuffer._buffers == {}


def test_successful_delivery_still_records_under_the_channel_key(monkeypatch) -> None:
    """The hoist must not have cost the happy path its event."""
    channel = _LiveChannel()
    binding = _LiveBinding()
    adapter = _Adapter()

    assert _deliver(channel, binding, adapter, monkeypatch, text="all good") is True

    events = ChannelDebugBuffer.list_events(channel.id)
    assert [e.kind for e in events] == [DEBUG_REPLIED]
    assert events[0].text == "all good"
    assert events[0].thread_key == binding.thread_key
    assert adapter.calls == [(binding.thread_key, "all good")]


def test_adapter_lookup_failure_is_still_recorded(monkeypatch, caplog) -> None:
    """The ``except`` also covers ``get_adapter`` / ``channel_type`` raising."""
    channel = _LiveChannel()

    def _boom(_type):
        raise LookupError("no adapter registered")

    monkeypatch.setattr(_MODULE + ".get_adapter", _boom)

    with caplog.at_level("WARNING"):
        delivered = asyncio.run(
            ChannelOutboundService._deliver(
                db=_FakeDB(), channel=channel, binding=_LiveBinding(), text="x"
            )
        )

    assert delivered is False
    assert "LookupError" in ChannelDebugBuffer.list_events(channel.id)[0].summary


# ── `_record_error`: the question of whether this is a fix or the look of one ──


def test_record_error_survives_an_expired_binding() -> None:
    """``binding.status`` was read *above* its own ``try``.

    That read is the same expired-instance reload the thread key is, and this
    function is called only after a delivery has already failed — precisely
    when a concurrently torn-down binding is plausible. A raise here escaped
    ``_deliver`` and replaced the delivery exception, so hoisting ``_deliver``'s
    argument expressions alone would not have been enough.
    """
    # Returning at all is the assertion.
    ChannelOutboundService._record_error(_FakeDB(), _BindingVanished(), "boom")


def test_record_error_survives_a_rollback_that_also_fails() -> None:
    """``db.rollback()`` sat unguarded inside the handler.

    A session rolled back into an unusable state raises again from the very
    call meant to clean it up — and that raise, too, escaped ``_deliver``.
    Returning at all (rather than propagating the rollback's own exception)
    is the assertion that both ``except`` branches were survived; ``caplog``
    added nothing here that ``db.commits``/``db.rollbacks`` don't already
    prove more directly — the counters increment before either exception is
    raised, so ``rollbacks == 1`` already proves the inner call was actually
    reached rather than skipped, and it cannot be a stale count from a
    previous call because ``_FakeDB`` is constructed fresh per test.
    """
    db = _FakeDB(
        commit_error=RuntimeError("constraint violation"),
        rollback_error=RuntimeError("session is closed"),
    )

    ChannelOutboundService._record_error(db, _LiveBinding(), "boom")

    assert db.commits == 1 and db.rollbacks == 1


def test_a_failed_write_makes_the_error_reportable_but_not_durable() -> None:
    """The distinction the fix must not paper over.

    Guarding the write makes the failure **reportable** — ``_deliver`` returns,
    the debug buffer keeps its event, the log keeps the diagnosis (that claim
    is pinned end-to-end, through ``_deliver``, by
    ``test_binding_row_and_log_keep_the_full_operator_diagnosis`` above). It
    does not make it **durable**: the rollback discards ``last_error`` and the
    binding row ends up with no record of the delivery failure at all. That
    second problem needs the persistent outbound queue named in the module
    docstring, not a guard, and this test exists so nobody reads the guard as
    having solved it.
    """
    binding = _LiveBinding()
    db = _FakeDB(commit_error=RuntimeError("constraint violation"))

    # Reportable: calling this does not raise.
    ChannelOutboundService._record_error(db, binding, "the real diagnosis")

    # Not durable: the write was attempted and then rolled back.
    assert db.commits == 1 and db.rollbacks == 1


def test_record_error_never_writes_over_an_existing_diagnosis() -> None:
    """A binding that already failed carries WHY, which beats "and we also
    couldn't tell them about it". Pinned because the early return moved inside
    the ``try`` and an early return is easy to lose in a reshuffle."""
    binding = _LiveBinding(status=CHANNEL_BINDING_FAILED, last_error="earlier diagnosis")
    db = _FakeDB()

    ChannelOutboundService._record_error(db, binding, "later noise")

    assert binding.last_error == "earlier diagnosis"
    assert db.commits == 0


# ── ``set_status`` must be TOTAL, not merely ``ChannelError``-safe ────────────
#
# The band comment above the status-notice verbs states the contract out loud:
# "Every one of them is best-effort and none of them raise". The only
# unguarded expression in ``set_status`` is its adapter lookup, and
# ``channel.channel_type`` inside it is the same expired-instance lazy reload
# every helper in this file exists for — so a guard that named only
# ``ChannelError`` did not deliver that contract.
#
# Three callers depend on it, and none of them can absorb a raise:
#
#  * ``_route_new_thread``'s Pass 1 hand-off — a raise between the adopt and
#    ``bound = binding`` leaves the row owning the notice while the failure
#    handler settles a stale local, so the flush loop patches "ready" over the
#    sender's last word.
#  * ``_install_and_park`` — the same window, across its ``return``.
#  * The outer failure handler itself, which settles THROUGH these verbs. The
#    exception it is handling is often a DB error, so the session is poisoned
#    and the reload raises ``PendingRollbackError`` out of the handler — the
#    notice is then stranded on "Setting up…" with no code path left to touch
#    it.


class _NoticeChannelVanished:
    """A channel whose row is gone: even ``channel_type`` is a failed reload."""

    def __getattr__(self, name: str):  # noqa: ANN401 — deliberate catch-all
        raise _Vanished(f"could not refresh {name}: row is gone")


def _set_status(channel) -> str | None:
    return asyncio.run(
        ChannelOutboundService.set_status(
            channel=channel,
            thread_key="spaces/AAA/threads/BBB",
            message_id=None,
            text="🔎 Finding the right assistant for you…",
        )
    )


def test_set_status_survives_an_expired_channel_instance() -> None:
    """``channel.channel_type`` is a reload, and reloads of deleted rows raise.

    ``ObjectDeletedError`` is not a ``ChannelError``; ``_Vanished`` stands in
    for it (a unit test has no session to expire). ``None`` is the documented
    answer for "no notice", which every caller already handles.
    """
    assert _set_status(_NoticeChannelVanished()) is None


@pytest.mark.parametrize(
    "error",
    [
        # Pool timeout / connection dropped mid-reload.
        OperationalError("SELECT 1", {}, Exception("connection lost")),
        # The failure handler's own case: the session is already poisoned by
        # the exception being handled, so ANY reload raises this.
        PendingRollbackError("session must be rolled back"),
        # And the case the original guard did name, kept so the broadening
        # cannot silently drop it.
        ChannelError("no adapter for 'nope'"),
    ],
)
def test_set_status_survives_a_failed_adapter_lookup(monkeypatch, error) -> None:
    def _boom(_channel_type: str):
        raise error

    monkeypatch.setattr(_MODULE + ".get_adapter", _boom)
    assert _set_status(_LiveChannel()) is None


def test_set_binding_status_is_total_by_extension(monkeypatch) -> None:
    """The verb the inbound pipeline actually calls, and the one that matters.

    It has no ``try`` of its own — its totality is entirely inherited from
    ``set_status`` and ``_persist_status_message_id``. Asserted separately
    because it is this signature, not ``set_status``, that sits inside the
    ownership hand-off windows described above.
    """
    monkeypatch.setattr(
        _MODULE + ".get_adapter",
        lambda _type: (_ for _ in ()).throw(
            PendingRollbackError("session must be rolled back")
        ),
    )
    db = _FakeDB()

    asyncio.run(
        ChannelOutboundService.set_binding_status(
            db=db,
            channel=_LiveChannel(),
            binding=_LiveBinding(),
            text="💬 Working on your message…",
            settle=True,
        )
    )

    # Nothing to release: the notice was never posted, so the row's id (already
    # ``None``) is left alone rather than written redundantly.
    assert db.commits == 0


# ── settle: the id is released only when the write landed ────────────────────
#
# ``settle=True`` means "rewrite the notice one last time and let go of the
# id". The release used to be unconditional, which quietly assumed the rewrite
# had happened. When both the patch and its fallback post fail, the sender has
# been shown nothing — the notice still stands with whatever it said before —
# and dropping the id orphans that message: the next thing to write a notice on
# this thread finds no id, posts a fresh one, and the thread carries the old
# text above the new. On the streaming path it is worse, because a seal that
# fails correctly does NOT advance the relay's sealed offset: the fresh message
# repeats the whole unsealed draft while the orphan stands with a prefix of the
# same paragraphs, so the reader sees the same text twice, permanently.


class _MuteAdapter:
    """A notice-capable transport on which nothing actually works."""

    capabilities = types.SimpleNamespace(
        supports_progress_updates=True,
        supports_status_notice=True,
        supports_message_delete=True,
    )

    def __init__(self) -> None:
        self.updates = 0
        self.posts = 0

    async def update_message(self, channel, thread_key, message_id, text) -> None:
        self.updates += 1
        raise ChannelError("patch failed")

    async def send_message(self, channel, thread_key, text) -> str:
        self.posts += 1
        raise ChannelError("post failed")


class _WorkingAdapter(_MuteAdapter):
    """The same transport, with a patch that lands."""

    async def update_message(self, channel, thread_key, message_id, text) -> None:
        self.updates += 1


def _settle(adapter, binding, monkeypatch) -> bool:
    monkeypatch.setattr(_MODULE + ".get_adapter", lambda _type: adapter)
    return asyncio.run(
        ChannelOutboundService.set_binding_status(
            db=_FakeDB(),
            channel=_LiveChannel(),
            binding=binding,
            text="Sorry — something went wrong setting that up.",
            settle=True,
        )
    )


def test_a_settle_that_never_reached_the_thread_keeps_the_notice_id(
    monkeypatch,
) -> None:
    """Both the patch and the fallback post fail: the id must survive.

    Keeping it is what lets the next turn patch the message that is still
    standing instead of posting beneath it — the orphan-plus-duplicate above.
    """
    binding = _LiveBinding()
    binding.status_message_id = "spaces/AAA/messages/OLD"
    adapter = _MuteAdapter()

    assert _settle(adapter, binding, monkeypatch) is False
    # Both routes were genuinely tried before we concluded "nothing landed".
    assert (adapter.updates, adapter.posts) == (1, 1)
    assert binding.status_message_id == "spaces/AAA/messages/OLD"


def test_a_settle_that_landed_still_releases_the_notice_id(monkeypatch) -> None:
    """The other half of the pair: a confirmed write releases, as it always did.

    Without this the fix above could be "never release", which strands the id
    for the pending-flush loop to patch "ready — working on your message…"
    over the last word this thread was told.
    """
    binding = _LiveBinding()
    binding.status_message_id = "spaces/AAA/messages/OLD"

    assert _settle(_WorkingAdapter(), binding, monkeypatch) is True
    assert binding.status_message_id is None
