"""Login throttle (C4): per-account lockout with backoff, per-source caps,
the native token endpoints, and the 429 contract.

429 body ``{"detail":{"code":"rate_limited","message":"rate_limited"}}`` plus
``Retry-After`` (integer seconds ≥ 1), identical for known and unknown
accounts, readable cross-origin (listed in CORS ``expose_headers``).

The suite runs with the throttle off (``tests/conftest.py``); each test here
re-enables it with fresh limiters via ``tests/utils/login_throttle.py``.
The backoff arithmetic is unit-tested in
``tests/unit/login_throttle_backoff_test.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.login_throttle import (
    enable_login_throttle,
    patch_throttle_clock,
    reset_login_throttle,
)
from tests.utils.user import create_random_user
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR
LOGIN = f"{API}/login/access-token"
RATE_LIMITED = {"detail": {"code": "rate_limited", "message": "rate_limited"}}


def _login(client: TestClient, email: str, password: str, headers: dict | None = None):
    return client.post(
        LOGIN, data={"username": email, "password": password}, headers=headers or {}
    )


def _assert_429(r, *, max_retry_after: int | None = None) -> int:
    assert r.status_code == 429, f"{r.status_code} {r.text}"
    assert r.json() == RATE_LIMITED
    retry_after = r.headers.get("retry-after")
    assert retry_after is not None and retry_after.isdigit(), retry_after
    value = int(retry_after)
    assert value >= 1
    if max_retry_after is not None:
        assert value <= max_retry_after, value
    return value


def _native_token(client: TestClient, base: str):
    return client.post(
        f"{API}/{base}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": "no-such-client",
            "refresh_token": "no-such-token",
        },
    )


# ── C4: per-account lockout ──────────────────────────────────────────────


def test_c4_repeated_bad_logins_get_429_with_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
      1. Known account: 5 wrong passwords → 400 each; the 6th → 429 with
         ``Retry-After`` ≤ the 30 s base lockout.
      2. Unknown address: the same sequence → the identical 429 body.
      3. Another account from the same source is not locked.
    """
    enable_login_throttle(monkeypatch)
    user = create_random_user(client)

    # ── Phase 1: known account ─────────────────────────────────────────
    for _ in range(settings.LOGIN_FAILURES_PER_ACCOUNT_SOURCE):
        r = _login(client, user["email"], "wrong-" + random_lower_string())
        assert r.status_code == 400, r.text
        assert r.json() == {"detail": "Incorrect email or password"}
    known = _login(client, user["email"], "wrong-" + random_lower_string())
    _assert_429(known, max_retry_after=settings.LOGIN_LOCKOUT_BASE_SECONDS)

    # ── Phase 2: unknown address, same shape ───────────────────────────
    ghost = random_email()
    for _ in range(settings.LOGIN_FAILURES_PER_ACCOUNT_SOURCE):
        assert _login(client, ghost, "x" * 10).status_code == 400
    unknown = _login(client, ghost, "x" * 10)
    _assert_429(unknown, max_retry_after=settings.LOGIN_LOCKOUT_BASE_SECONDS)
    assert unknown.content == known.content

    # ── Phase 3: other accounts unaffected ─────────────────────────────
    other = create_random_user(client)
    r = _login(client, other["email"], other["_password"])
    assert r.status_code == 200, r.text


def test_right_password_refused_while_locked_and_accepted_after_lockout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
      1. 5 failures lock the account.
      2. The right password → 429 (checked before the password).
      3. After the lockout (clock +31 s) the right password → 200.
      4. Success cleared the failures: one more wrong password → 400, not 429.
      5. Backoff: 5 failures again, then one more after the 30 s lockout →
         the next lockout is 60 s.
    """
    enable_login_throttle(monkeypatch)
    clock = patch_throttle_clock(monkeypatch)
    user = create_random_user(client)

    # ── Phase 1–2: locked, right password refused ──────────────────────
    for _ in range(5):
        assert _login(client, user["email"], "wrong-password").status_code == 400
    _assert_429(_login(client, user["email"], user["_password"]))

    # ── Phase 3: lockout over ──────────────────────────────────────────
    clock.advance(settings.LOGIN_LOCKOUT_BASE_SECONDS + 1)
    r = _login(client, user["email"], user["_password"])
    assert r.status_code == 200, r.text

    # ── Phase 4: failures were cleared ─────────────────────────────────
    assert _login(client, user["email"], "wrong-password").status_code == 400

    # ── Phase 5: doubled lockout after the 6th failure ─────────────────
    for _ in range(4):
        assert _login(client, user["email"], "wrong-password").status_code == 400
    _assert_429(_login(client, user["email"], user["_password"]), max_retry_after=30)
    clock.advance(31)
    assert _login(client, user["email"], "wrong-password").status_code == 400
    retry = _assert_429(_login(client, user["email"], user["_password"]))
    assert 30 < retry <= 60, retry


# ── Per-source caps and the native token endpoints ───────────────────────


def test_per_source_cap_and_native_token_endpoints(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
      1. Per-source cap 3/min: three logins for different addresses pass the
         throttle; the 4th → 429, even with a fresh ``X-Forwarded-For``
         (forwarded headers from a non-proxy peer are ignored).
      2. Native cap 2/min: ``/desktop-auth/token`` twice → not 429; 3rd → 429.
      3. ``/app-auth/token`` likewise (fresh limiters).
    """
    enable_login_throttle(
        monkeypatch,
        LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN=3,
        NATIVE_TOKEN_RATE_LIMIT_PER_SOURCE_PER_MIN=2,
    )

    # ── Phase 1: password logins ───────────────────────────────────────
    for _ in range(3):
        assert _login(client, random_email(), "x" * 10).status_code == 400
    _assert_429(
        _login(
            client, random_email(), "x" * 10, headers={"X-Forwarded-For": "203.0.113.9"}
        ),
        max_retry_after=60,
    )

    # ── Phase 2–3: native token endpoints ──────────────────────────────
    for base in ("desktop-auth", "app-auth"):
        reset_login_throttle()
        for _ in range(2):
            r = _native_token(client, base)
            assert r.status_code != 429, (base, r.text)
            assert r.status_code == 400, (base, r.text)
        _assert_429(_native_token(client, base), max_retry_after=60)


def test_disabled_flag_never_throttles(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``LOGIN_RATE_LIMIT_ENABLED`` off (the suite default) and tiny
    caps, no endpoint ever answers 429."""
    reset_login_throttle()
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN", 1)
    monkeypatch.setattr(settings, "NATIVE_TOKEN_RATE_LIMIT_PER_SOURCE_PER_MIN", 1)
    user = create_random_user(client)
    for _ in range(8):
        assert _login(client, user["email"], "wrong-password").status_code == 400
    assert _login(client, user["email"], user["_password"]).status_code == 200
    for base in ("desktop-auth", "app-auth"):
        for _ in range(3):
            assert _native_token(client, base).status_code == 400


def test_c4_429_exposes_retry_after_cross_origin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A login 429 sent from the SPA origin carries CORS headers that let the
    browser read ``Retry-After``."""
    enable_login_throttle(monkeypatch, LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN=1)
    origin = settings.FRONTEND_HOST.rstrip("/")
    assert origin in settings.all_cors_origins

    assert _login(client, random_email(), "x" * 10).status_code == 400
    r = _login(client, random_email(), "x" * 10, headers={"Origin": origin})
    _assert_429(r)
    assert r.headers.get("access-control-allow-origin") == origin
    exposed = [
        h.strip().lower()
        for h in r.headers.get("access-control-expose-headers", "").split(",")
    ]
    assert "retry-after" in exposed, r.headers
