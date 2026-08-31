"""``ChannelInboundService._reply`` must not destroy the failure it records.

The companion to ``test_channel_debug_buffer.py``. That file pins the buffer's
*own* never-raises guard; this one pins the thing that guard cannot reach — the
caller's argument list, which Python evaluates before ``record`` is entered.

Why this call site rather than the others: ``_reply``'s recorder sits **inside
an ``except``**. Elsewhere in the inbound pipeline an unguarded argument
expression turns into a 500 the platform retries; here it *replaces* the
exception being recorded, so the delivery failure the debug panel exists to
show is the one thing that disappears. See
``docs/plans/auto_routing_tuning_plan.md`` §11a Rule 2 — "the test for a new
instrumentation point is not 'is the recorder guarded' but 'can anything in the
caller's argument list raise'", proved by firing a poison object rather than by
reading the code.

Two expressions were exposed at that call site, and each gets a poison here:

* ``channel.id`` — every caller reaches ``_reply`` after a ``db.commit()`` that
  expired the instance, so the read is a lazy reload and a reload of a
  concurrently deleted row raises ``ObjectDeletedError``. ``_Vanished`` below
  stands in for that (a unit test has no session to expire).
* ``f"...{exc}"`` — an exception whose ``__str__`` raises, which is exactly the
  shape Rule 2 names.

Pure logic with fakes: no DB, no ``TestClient``, no HTTP. ``_reply``'s own
``db`` argument is never touched by the code under test, so ``None`` is passed.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager

import pytest

from app.services.server_channels import channel_inbound_service as _cis
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_REPLIED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)
from app.services.server_channels.channel_inbound_service import ChannelInboundService

_MODULE = "app.services.server_channels.channel_inbound_service"


# ── Poison objects ────────────────────────────────────────────────────────────


class _Vanished(Exception):
    """Stands in for ``ObjectDeletedError`` on an expired-instance reload."""


class _PoisonStr(Exception):
    """An exception that cannot be turned into a string.

    ``status_code`` is present so the test can also assert what a *total*
    describer still manages to salvage from it.
    """

    status_code = 503

    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


class _LiveChannel:
    """A channel whose ``id`` reads cleanly — the freshly-loaded case."""

    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.channel_type = "google_chat"


class _ExpiredChannel:
    """A channel whose row is gone: every attribute read is a failed reload."""

    def __getattr__(self, name: str):  # noqa: ANN401 - deliberate catch-all
        raise _Vanished(f"could not refresh {name}: row is gone")


class _ChannelLostMidSend(_ExpiredChannel):
    """Readable up to the send, unreadable after it — the discriminating case.

    The fully-poisoned ``_ExpiredChannel`` does not actually exercise the
    reported defect: ``channel.channel_type`` is read at the *top* of the
    ``try``, so it raises before the adapter is ever called and there is no
    delivery failure left to destroy. (Learned by running it, which is the
    entire argument for §11a Rule 2's "fire a poison object" clause.)

    This subclass reproduces the sequence that matters: the attributes needed
    to send read cleanly, the send then fails, and only ``id`` — read inside
    the ``except`` — is unreachable.
    """

    channel_type = "google_chat"


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


# ── Fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_buffer():
    """The buffer is process-global class state — isolate every test."""
    ChannelDebugBuffer.reset()
    yield
    ChannelDebugBuffer.reset()


def _reply(channel, adapter: _Adapter, monkeypatch, *, text: str = "notice") -> None:
    """Drive ``_reply`` to completion with ``adapter`` installed."""
    monkeypatch.setattr(_MODULE + ".get_adapter", lambda _type: adapter)
    asyncio.run(ChannelInboundService._reply(None, channel, "thread-1", text))


def _spy_log_detail(monkeypatch) -> list[tuple[BaseException, str]]:
    """Capture every ``(exc, result)`` pair the ``except`` branch hands to
    ``_log_detail``, while still running the real implementation.

    This replaces a ``caplog.text`` substring check. ``_log_detail`` is the
    one place in ``_reply`` that decides what an exception looks like once it
    reaches the log — total by construction, per its own docstring — so
    spying on it directly proves *the surviving exception itself* reached
    that decision point, and what it decided, without going anywhere near
    ``logging``. That distinction matters here specifically: the
    session-scoped ``setup_db`` fixture runs Alembic before any test, and
    ``alembic.config.Config`` calls ``logging.config.fileConfig`` with its
    default ``disable_existing_loggers=True``, which leaves every
    application logger — this module's included — permanently ``disabled``
    for the rest of the session (see ``tests/README.md``, "caplog assertions
    are vacuous for the rest of the session"). Any assertion routed through
    ``caplog.text`` is comparing against ``''`` in that scope and passes
    whether or not the call ever happened — which is exactly how the three
    assertions this file used to make went unnoticed until a full-suite run.
    """
    calls: list[tuple[BaseException, str]] = []
    original = _cis._log_detail

    def _spy(exc: BaseException) -> str:
        result = original(exc)
        calls.append((exc, result))
        return result

    monkeypatch.setattr(_cis, "_log_detail", _spy)
    return calls


@contextmanager
def _unswallowed_module_warnings():
    """Real ``LogRecord``s from this module's own logger.

    Used only for the one property here that genuinely IS a log line — "the
    drop of the unfileable event is itself observable rather than silent"
    (Rule 1's half of the bargain) is a claim about whether a warning was
    emitted at all, not about which exception it carried. ``caplog`` cannot
    answer that in this scope for the reason given in ``_spy_log_detail``
    above, so this attaches a handler straight to the module logger and
    force-enables it for the duration instead — the same manoeuvre as
    ``_swallowed_failures`` in
    ``tests/api/routing/routing_persist_session_ownership_test.py``.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger(_MODULE)
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


# ── The regression this file exists for ───────────────────────────────────────


def test_expired_channel_does_not_replace_the_delivery_failure(monkeypatch) -> None:
    """The original send failure survives an unreadable ``channel.id``.

    Before the fix, ``channel_id=channel.id`` in the ``except`` branch raised
    on the lazy reload. That raise propagated out of ``_reply`` *in place of*
    the send failure: the caller saw a database error, and the diagnosis the
    record was capturing was gone. ``_reply`` is best-effort by contract, so
    "intact" means the caller is handed nothing at all and the failure reaches
    the log — not that a database error arrives instead.
    """
    original = RuntimeError("google chat rejected the post")
    adapter = _Adapter(error=original)
    detail_calls = _spy_log_detail(monkeypatch)

    with _unswallowed_module_warnings() as records:
        # No ``pytest.raises``: this returning at all is the assertion. Before
        # the fix it raised ``_Vanished`` out of the handler.
        _reply(_ChannelLostMidSend(), adapter, monkeypatch)

    assert adapter.attempts == 1, "the send must actually have been attempted"
    # The failure handed to the log is the ORIGINAL send failure, not a
    # reload error that displaced it — proved directly on what reached
    # ``_log_detail`` rather than by matching a substring in ``caplog.text``
    # (see ``_spy_log_detail`` for why that check is unconditionally vacuous
    # in this scope).
    assert detail_calls, "the except-branch never reached _log_detail"
    assert detail_calls[-1][0] is original
    # And the drop of the unfileable event is itself observable rather than
    # silent — Rule 1's half of the bargain. That property genuinely is a log
    # line, so real ``LogRecord``s are used for it instead.
    assert any(
        "Could not identify a channel for the debug buffer" in r.getMessage()
        for r in records
    )
    # Nowhere to file it, so it was not filed anywhere.
    assert ChannelDebugBuffer._buffers == {}


def test_fully_vanished_channel_fails_before_the_send_but_still_quietly(
    monkeypatch,
) -> None:
    """The other half: an instance unreadable from the first touch.

    ``channel.channel_type`` at the top of the ``try`` raises before the
    adapter is reached, so there is no delivery failure to preserve — but the
    ``except`` must still cope, because its own recorder would otherwise raise
    a *second* time on ``channel.id``.
    """
    adapter = _Adapter()
    detail_calls = _spy_log_detail(monkeypatch)

    _reply(_ExpiredChannel(), adapter, monkeypatch)

    assert adapter.attempts == 0
    # The channel_type read failure is what reached the log — proof the
    # except-branch coped rather than blowing up trying to re-read
    # channel.id a second time. See ``_spy_log_detail`` for why this is not
    # a ``caplog.text`` check.
    assert detail_calls, "the except-branch never reached _log_detail"
    assert "could not refresh channel_type" in str(detail_calls[-1][0])


def test_exception_with_a_raising_str_does_not_break_the_handler(
    monkeypatch, caplog
) -> None:
    """A poison ``__str__`` is described, not detonated — and still recorded.

    The second exposed expression. ``f"Notice delivery failed: {exc}"`` called
    ``__str__`` before ``record`` was entered, so the buffer's guard never got
    the chance to swallow it.
    """
    channel = _LiveChannel()
    adapter = _Adapter(error=_PoisonStr())

    with caplog.at_level("WARNING"):
        _reply(channel, adapter, monkeypatch)

    # The record was still *attempted*, and it landed: a debug aid that drops
    # the hardest failures is the inversion this feature keeps rediscovering.
    events = ChannelDebugBuffer.list_events(channel.id)
    assert [e.kind for e in events] == [DEBUG_SEND_FAILED]
    summary = events[0].summary
    assert summary.startswith("Notice delivery failed: ")
    # Better than merely surviving: ``describe_exception`` never calls
    # ``str(exc)`` in the first place, so the poison is not just contained —
    # the admin still gets a usable line out of it.
    assert "_PoisonStr" in summary
    assert "HTTP 503" in summary


def test_both_poisons_at_once_still_returns_quietly(monkeypatch) -> None:
    """Neither expression can raise, so both failing at once is survivable."""
    adapter = _Adapter(error=_PoisonStr())
    detail_calls = _spy_log_detail(monkeypatch)

    # Again: completing is the assertion.
    _reply(_ChannelLostMidSend(), adapter, monkeypatch)

    assert adapter.attempts == 1
    # Nothing was filed under a key no panel reads.
    assert ChannelDebugBuffer._buffers == {}
    # The log survives an unprintable exception rather than being the thing
    # that raises — proved directly on ``_log_detail``'s own return value
    # (total by construction), not via ``caplog.text``. ``logging``'s own
    # interpolation is not covered by the handler, and pytest's capture
    # handler re-raises what production swallows — but that risk is why
    # ``_log_detail`` pre-formats in the first place, and this pins its
    # actual output rather than a log substring (see ``_spy_log_detail``).
    assert detail_calls, "the except-branch never reached _log_detail"
    assert isinstance(detail_calls[-1][0], _PoisonStr)
    assert detail_calls[-1][1] == "<unprintable _PoisonStr>"


def test_send_failure_summary_carries_the_diagnosis_not_the_message(
    monkeypatch,
) -> None:
    """Type and HTTP status survive; the exception's message body does not.

    Pins the choice of ``describe_exception`` over a plain f-string. An
    adapter's HTTP error echoes the request it just made — a request carrying
    the channel's service-account credentials — and this buffer is a read
    surface. A future refactor back to ``f"{exc}"`` fails here.
    """

    class _Rejected(Exception):
        status_code = 403

    channel = _LiveChannel()
    adapter = _Adapter(error=_Rejected("service account key AKIA-SECRET denied"))

    _reply(channel, adapter, monkeypatch)

    summary = ChannelDebugBuffer.list_events(channel.id)[0].summary
    assert "_Rejected" in summary
    assert "HTTP 403" in summary
    assert "AKIA-SECRET" not in summary


def test_successful_delivery_still_records_under_the_channel_key(
    monkeypatch,
) -> None:
    """The hoist must not have cost the happy path its event."""
    channel = _LiveChannel()
    adapter = _Adapter()

    _reply(channel, adapter, monkeypatch, text="all good")

    events = ChannelDebugBuffer.list_events(channel.id)
    assert [e.kind for e in events] == [DEBUG_REPLIED]
    assert events[0].text == "all good"
    assert adapter.calls == [("thread-1", "all good")]


def test_adapter_lookup_failure_is_still_recorded(monkeypatch, caplog) -> None:
    """The ``except`` also covers ``get_adapter``/``channel_type`` raising.

    Those reads are deliberately left inline (a separate, deferred sweep), so
    this pins that the handler they land in copes with them today.
    """
    channel = _LiveChannel()

    def _boom(_type):
        raise LookupError("no adapter registered")

    monkeypatch.setattr(_MODULE + ".get_adapter", _boom)

    with caplog.at_level("WARNING"):
        asyncio.run(ChannelInboundService._reply(None, channel, "thread-1", "x"))

    events = ChannelDebugBuffer.list_events(channel.id)
    assert [e.kind for e in events] == [DEBUG_SEND_FAILED]
    assert "LookupError" in events[0].summary
