"""
Shared in-memory sliding-window rate limiter for the CLI service surface.

Lifted out of ``account_api_proxy_service`` so it can be reused by both the
escape-hatch proxy (keyed by account-token id) and the unauthenticated device-
login endpoints (keyed by source IP). Process-local (one window per worker) — a
backstop against a runaway local loop, not a billing control.
"""
import threading
import time
from collections import deque


class RateLimiter:
    """In-memory sliding-window throttle keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit_per_min: int) -> float | None:
        """Record a hit. Return ``None`` if allowed, else seconds until retry."""
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= limit_per_min:
                retry_after = max(1.0, 60.0 - (now - bucket[0]))
                return retry_after
            bucket.append(now)
            return None
