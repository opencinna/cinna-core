"""In-process bookkeeping for live voice sessions.

Valid because the backend runs a single uvicorn worker (as the env console
trackers assume). The rate limiter is a backstop against reconnect loops, not
a billing control; the monthly cap is enforced from the ledger.
"""
from __future__ import annotations

import uuid

from app.services.common.rate_limiter import RateLimiter

open_rate_limiter = RateLimiter()
test_rate_limiter = RateLimiter()

_live: dict[uuid.UUID, set[uuid.UUID]] = {}


def try_acquire(user_id: uuid.UUID, session_id: uuid.UUID, limit: int) -> bool:
    """Claim a live slot for the user. Synchronous, so atomic on the event loop."""
    sessions = _live.setdefault(user_id, set())
    if len(sessions) >= limit:
        return False
    sessions.add(session_id)
    return True


def release(user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    sessions = _live.get(user_id)
    if sessions is None:
        return
    sessions.discard(session_id)
    if not sessions:
        _live.pop(user_id, None)


def live_count(user_id: uuid.UUID) -> int:
    return len(_live.get(user_id, ()))


def reset_for_tests() -> None:
    """Forget every live session and rate-limit window."""
    global open_rate_limiter, test_rate_limiter
    _live.clear()
    open_rate_limiter = RateLimiter()
    test_rate_limiter = RateLimiter()
