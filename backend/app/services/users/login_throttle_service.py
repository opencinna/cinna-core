"""Login throttle (C4): per-source rate limits and per-account lockout.

Guards ``POST /login/access-token`` (password) and the native
``/desktop-auth/token`` / ``/app-auth/token`` endpoints. In-process, one
window per worker, like every limiter in the repo — a brute-force backstop,
not an exact control.

Password logins are checked against three conditions, all before the
password is verified (so a correct password is refused while locked):

* per source: every attempt counts against
  ``LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN`` (sliding minute);
* per account + source and per account: once ``f >= T`` failures sit inside
  ``LOGIN_FAILURE_WINDOW_SECONDS``, the key is locked for
  ``min(BASE * 2^(f - T), MAX)`` seconds from the last failure.

A success clears both account keys. Attempts refused with 429 never reach
the password check, so they are not counted as failures. Known and unknown
accounts are throttled identically (the key is the submitted address).
"""
import math
import threading
import time
from collections import deque

from fastapi import Request

from app.core.config import settings
from app.services.common.rate_limiter import RateLimiter, anonymous_caller_key

# Ceiling on tracked failure keys (attacker-chosen addresses). Past it, new
# keys are not recorded until the sweep frees room; the per-source limit still
# applies to every attempt, so this fails open only for the lockout of an
# address first seen during a flood.
_MAX_FAILURE_KEYS = 50_000
_SWEEP_INTERVAL_SECONDS = 30.0
_MAX_EMAIL_CHARS = 320


class LoginThrottled(Exception):
    """A throttled attempt. ``retry_after`` is whole seconds, at least 1."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("rate_limited")
        self.retry_after = max(1, math.ceil(retry_after))


class _FailureLog:
    """Lock-guarded failure timestamps per key (monotonic seconds)."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def record(self, key: str, now: float) -> None:
        window = settings.LOGIN_FAILURE_WINDOW_SECONDS
        with self._lock:
            self._sweep_locked(now, window)
            bucket = self._failures.get(key)
            if bucket is None:
                if len(self._failures) >= _MAX_FAILURE_KEYS:
                    return
                bucket = self._failures.setdefault(key, deque())
            self._prune(bucket, now, window)
            bucket.append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def lockout_remaining(self, key: str, threshold: int, now: float) -> float:
        """Seconds left on the key's lockout, ``0`` when not locked."""
        window = settings.LOGIN_FAILURE_WINDOW_SECONDS
        with self._lock:
            bucket = self._failures.get(key)
            if not bucket:
                return 0.0
            self._prune(bucket, now, window)
            failures = len(bucket)
            if failures < threshold:
                return 0.0
            exponent = min(failures - threshold, 32)
            lockout = min(
                settings.LOGIN_LOCKOUT_BASE_SECONDS * (2**exponent),
                settings.LOGIN_LOCKOUT_MAX_SECONDS,
            )
            return max(0.0, bucket[-1] + lockout - now)

    @staticmethod
    def _prune(bucket: deque[float], now: float, window: float) -> None:
        while bucket and bucket[0] < now - window:
            bucket.popleft()

    def _sweep_locked(self, now: float, window: float) -> None:
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        stale = [k for k, b in self._failures.items() if not b or b[-1] < now - window]
        for k in stale:
            del self._failures[k]


_login_source_limiter = RateLimiter()
_native_source_limiter = RateLimiter()
_failure_log = _FailureLog()


def _account_key(email: str) -> str:
    return "acct:" + (email or "").strip().lower()[:_MAX_EMAIL_CHARS]


def _account_source_key(email: str, source: str) -> str:
    return _account_key(email) + "|src:" + source


class LoginThrottle:
    @staticmethod
    def reset() -> None:
        """Fresh limiters and an empty failure log (tests re-enabling C4)."""
        global _login_source_limiter, _native_source_limiter, _failure_log
        _login_source_limiter = RateLimiter()
        _native_source_limiter = RateLimiter()
        _failure_log = _FailureLog()

    @staticmethod
    def check_password_attempt(request: Request, email: str) -> None:
        """Raise :class:`LoginThrottled` when this attempt must be refused."""
        if not settings.LOGIN_RATE_LIMIT_ENABLED:
            return
        now = time.monotonic()
        source = anonymous_caller_key(request)
        locked = max(
            _failure_log.lockout_remaining(
                _account_source_key(email, source),
                settings.LOGIN_FAILURES_PER_ACCOUNT_SOURCE,
                now,
            ),
            _failure_log.lockout_remaining(
                _account_key(email), settings.LOGIN_FAILURES_PER_ACCOUNT, now
            ),
        )
        if locked > 0:
            raise LoginThrottled(locked)
        retry_after = _login_source_limiter.check(
            source, settings.LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN
        )
        if retry_after is not None:
            raise LoginThrottled(retry_after)

    @staticmethod
    def record_failure(request: Request, email: str) -> None:
        if not settings.LOGIN_RATE_LIMIT_ENABLED:
            return
        now = time.monotonic()
        source = anonymous_caller_key(request)
        _failure_log.record(_account_source_key(email, source), now)
        _failure_log.record(_account_key(email), now)

    @staticmethod
    def record_success(request: Request, email: str) -> None:
        if not settings.LOGIN_RATE_LIMIT_ENABLED:
            return
        source = anonymous_caller_key(request)
        _failure_log.clear(_account_source_key(email, source))
        _failure_log.clear(_account_key(email))

    @staticmethod
    def check_native_token(request: Request) -> None:
        """Per-source limit on the native token endpoints."""
        if not settings.LOGIN_RATE_LIMIT_ENABLED:
            return
        retry_after = _native_source_limiter.check(
            anonymous_caller_key(request),
            settings.NATIVE_TOKEN_RATE_LIMIT_PER_SOURCE_PER_MIN,
        )
        if retry_after is not None:
            raise LoginThrottled(retry_after)
