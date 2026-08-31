"""Shared in-memory sliding-window rate limiter.

Originally lived in ``app.services.cli.rate_limiter``, lifted out of
``account_api_proxy_service``. Promoted here once a third, non-CLI consumer
appeared (the server-channels webhook), the same path ``egress_guard`` took —
a shared chokepoint should not live inside one domain's package.

Consumers key it differently by design: the escape-hatch proxy by account-token
id, the unauthenticated device-login endpoints by source IP, the channel webhook
by webhook token. Process-local (one window per worker) — a backstop against a
runaway loop or a token-guessing burst, not a billing control.

**Memory bound.** The channel webhook is the first consumer whose key is both
attacker-chosen and unbounded in cardinality: anyone can POST to
``/channels/<anything>/inbound``, and the limiter runs before the token is
resolved. Without eviction that dict is a memory-exhaustion vector, so the
limiter prunes expired buckets and, past a hard ceiling, stops minting new ones
and folds further keys into a shared overflow bucket. Overflow degrades
fairness but never fails open: distinct keys share one window, so during a
flood a legitimate low-traffic caller whose bucket was swept can land in the
overflow bucket and be throttled by the attacker's volume. Availability of the
process is the property being protected; per-key fairness is what is traded.
"""
import threading
import time
from collections import deque

# Window length. Callers express limits per minute.
_WINDOW_SECONDS = 60.0

# Prune expired buckets at most this often — the sweep is O(keys), so it must
# not run on every call.
_SWEEP_INTERVAL_SECONDS = 30.0

# Ceiling on distinct tracked keys. Past this, new keys share one bucket rather
# than growing the dict without limit.
_MAX_TRACKED_KEYS = 10_000

# Keys longer than this are truncated before use — a URL path segment can be
# kilobytes, and the raw value is never needed, only its identity.
_MAX_KEY_CHARS = 200

_OVERFLOW_KEY = "\x00overflow"


class RateLimiter:
    """In-memory sliding-window throttle keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def check(self, key: str, limit_per_min: int) -> float | None:
        """Record a hit. Return ``None`` if allowed, else seconds until retry."""
        now = time.monotonic()
        window_start = now - _WINDOW_SECONDS
        key = (key or "")[:_MAX_KEY_CHARS]

        with self._lock:
            self._sweep_locked(now, window_start)

            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= _MAX_TRACKED_KEYS:
                    # Ceiling reached: share one bucket instead of growing.
                    key = _OVERFLOW_KEY
                    bucket = self._hits.setdefault(key, deque())
                else:
                    bucket = self._hits.setdefault(key, deque())

            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= limit_per_min:
                retry_after = max(1.0, _WINDOW_SECONDS - (now - bucket[0]))
                return retry_after
            bucket.append(now)
            return None

    def _sweep_locked(self, now: float, window_start: float) -> None:
        """Drop buckets with no hits left in the window. Caller holds the lock."""
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        stale = [
            k
            for k, bucket in self._hits.items()
            if not bucket or bucket[-1] < window_start
        ]
        for k in stale:
            del self._hits[k]
