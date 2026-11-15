"""Unit tests for the C4 lockout arithmetic in ``login_throttle_service``.

Lockout after ``f >= T`` failures inside the window lasts
``min(BASE * 2^(f - T), MAX)`` seconds from the last failure. With the
defaults (T=5, BASE=30, MAX=900): 30, 60, 120, 240, 480, then 900 from the
10th failure on.

The HTTP behaviour (429 body, ``Retry-After``, right password refused while
locked) is covered in ``tests/api/auth/login_throttle_test.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.users.login_throttle_service import LoginThrottled, _FailureLog


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LOGIN_LOCKOUT_BASE_SECONDS", 30)
    monkeypatch.setattr(settings, "LOGIN_LOCKOUT_MAX_SECONDS", 900)
    monkeypatch.setattr(settings, "LOGIN_FAILURE_WINDOW_SECONDS", 900)


def _log_with(failures: int, at: float = 1000.0) -> _FailureLog:
    log = _FailureLog()
    for _ in range(failures):
        log.record("k", at)
    return log


@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        (0, 0.0),
        (4, 0.0),
        (5, 30.0),
        (6, 60.0),
        (7, 120.0),
        (8, 240.0),
        (9, 480.0),
        (10, 900.0),
        (11, 900.0),
        (60, 900.0),
    ],
)
def test_backoff_sequence_and_cap(failures: int, expected: float) -> None:
    assert _log_with(failures).lockout_remaining("k", 5, 1000.0) == expected


def test_lockout_counts_from_the_last_failure_and_expires() -> None:
    log = _log_with(5, at=1000.0)
    assert log.lockout_remaining("k", 5, 1010.0) == 20.0
    assert log.lockout_remaining("k", 5, 1030.0) == 0.0
    assert log.lockout_remaining("k", 5, 1031.0) == 0.0


def test_failures_outside_the_window_do_not_count() -> None:
    log = _FailureLog()
    for _ in range(4):
        log.record("k", 0.0)
    log.record("k", 1000.0)  # the first four are older than 900 s now
    assert log.lockout_remaining("k", 5, 1000.0) == 0.0


def test_clear_and_per_key_isolation() -> None:
    log = _log_with(5)
    assert log.lockout_remaining("other", 5, 1000.0) == 0.0
    log.clear("k")
    assert log.lockout_remaining("k", 5, 1000.0) == 0.0


def test_account_threshold_is_independent() -> None:
    """The per-account key uses its own (higher) threshold."""
    log = _log_with(19)
    assert log.lockout_remaining("k", 20, 1000.0) == 0.0
    log.record("k", 1000.0)
    assert log.lockout_remaining("k", 20, 1000.0) == 30.0


@pytest.mark.parametrize(("raw", "expected"), [(0.0, 1), (0.2, 1), (1.0, 1), (29.1, 30)])
def test_retry_after_is_whole_seconds_at_least_one(raw: float, expected: int) -> None:
    assert LoginThrottled(raw).retry_after == expected
