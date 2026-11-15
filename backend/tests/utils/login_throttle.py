"""Turning the C4 login throttle on for one test, and controlling its clock.

``backend/tests/conftest.py`` sets ``settings.LOGIN_RATE_LIMIT_ENABLED = False``
for the whole suite: many tests log in from the one TestClient source in a
burst. The throttle tests re-enable it here.

EXEMPTION — ``app.services`` import. The limiters are module-level state with
no HTTP surface that can reset them, so ``LoginThrottle.reset()`` is imported
inside the function, in this one named place (same pattern as
``tests/utils/invitation.py::fresh_invitation_limiter``).

All TestClient requests share one source key (``testclient``; a forwarded
header from a non-private peer is ignored), so per-source limits count every
request a test makes against the throttled endpoints.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.core.config import settings

_THROTTLE_MODULE = "app.services.users.login_throttle_service"


def enable_login_throttle(monkeypatch: pytest.MonkeyPatch, **overrides: int) -> None:
    """Enable C4 with fresh limiters; ``overrides`` set ``settings`` fields
    (e.g. ``LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN=3``) for this test only."""
    from app.services.users.login_throttle_service import LoginThrottle

    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_ENABLED", True)
    for name, value in overrides.items():
        assert hasattr(settings, name), name
        monkeypatch.setattr(settings, name, value)
    # Leftover state from an earlier test is harmless once the flag is back
    # off (every check returns early), and every enabling test starts here.
    LoginThrottle.reset()


def reset_login_throttle() -> None:
    """Fresh limiters and failure log, keeping the current settings."""
    from app.services.users.login_throttle_service import LoginThrottle

    LoginThrottle.reset()


class ThrottleClock:
    """A controllable ``time.monotonic`` for the throttle's failure log.

    Only the lockout bookkeeping reads it; the per-source sliding windows keep
    real time, which is fine for tests that make a handful of requests.
    """

    def __init__(self) -> None:
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def patch_throttle_clock(monkeypatch: pytest.MonkeyPatch) -> ThrottleClock:
    clock = ThrottleClock()
    monkeypatch.setattr(f"{_THROTTLE_MODULE}.time", SimpleNamespace(monotonic=clock.monotonic))
    return clock
