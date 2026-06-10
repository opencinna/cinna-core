"""MFA tests — TOTP enrollment, login challenge lifecycle, recovery codes,
challenge expiry/locking, rate limiting, and security events.

Coverage targets (from plan §13):
  1.  Login regression — password login without 2FA returns LoginToken (kind="token").
  2.  Login MFA branch — password login with 2FA returns MfaChallenge (kind="mfa_challenge").
  4.  TOTP enrollment success — begin returns secret/QR/secret_token; finish with valid
      code persists secret and flips two_factor_enabled=True on first factor.
  5.  TOTP enrollment wrong code — finish with wrong code does NOT persist UserTotpSecret.
  6.  TOTP verify accepts current ± 1 step within validity window.
  7.  TOTP replay rejection via last_used_step.
  10. Recovery code one-shot — consumed on first use, rejected on second.
  11. Recovery code regenerate — invalidates previous batch.
  12. Challenge expiry — after MFA_CHALLENGE_TTL_SECONDS, verify returns 410.
  13. Attempt limit — 5 failed verifications return 429 and lock the challenge.
  14. Rate limit per-user — 10 verifies / 5 minutes triggers 429 and writes MFA_RATE_LIMITED.
  15. Last factor protection (TOTP path) — deleting only TOTP while 2FA on returns 409.
  16. Disable 2FA requires step-up — without proof returns 401; with valid proof wipes all.
  17. Auth required — all /users/me/mfa/* endpoints reject anonymous requests.
  18. SecurityEvent rows written for key events.
  20. TOTP secret_token user-binding & expiry.
  21. Recovery codes only after enrollment (factor_not_enrolled when 2FA off).
  23. UserPublic derived flags — /users/me, signup response return has_passkey/has_totp.

WebAuthn passkey coverage is in test_mfa_passkeys.py.

TOTP codes are computed using ``pyotp.TOTP(secret).now()`` — no sleeps.

For challenge-expiry tests we monkeypatch ``app.services.users.mfa_service.datetime``
so ``datetime.now(UTC)`` returns a far-future timestamp; this avoids real clock
manipulation or sleeps.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import pyotp
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.mfa import (
    assert_security_event_written,
    delete_with_body,
    enroll_totp,
    enroll_totp_and_get_secret,
    get_me,
    get_mfa_status,
    headers_from_token,
    list_security_events,
    login,
    login_get_challenge,
    signup_user,
    totp_begin,
    totp_finish,
    verify_challenge_recovery,
)

_BASE = settings.API_V1_STR

# MFA tests never create agents; opt out of the heavy agent/env stubs in
# tests/api/users/conftest.py (which also provides autouse rate-limit-bucket
# clearing, keeping this file order-independent).
NEEDS_AGENT_STUBS = False


# ── 1. Login regression — no 2FA ──────────────────────────────────────


def test_login_without_2fa_returns_token(client: TestClient) -> None:
    """Password login with two_factor_enabled=False returns LoginToken (kind='token')
    with a non-empty access_token and never an mfa_challenge."""
    user = signup_user(client)
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "token", f"Expected kind=token, got {body}"
    assert "access_token" in body
    assert body["access_token"]
    assert "challenge_token" not in body


# ── 2. Login MFA branch ───────────────────────────────────────────────


def test_login_with_2fa_returns_mfa_challenge(client: TestClient) -> None:
    """Password login with two_factor_enabled=True returns MfaChallenge
    (kind='mfa_challenge') — never an access token at step 1 — and a valid TOTP
    code at step 2 yields a full access token."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # ── Phase 1: Enroll TOTP → flips two_factor_enabled (keep the secret) ──
    secret = enroll_totp_and_get_secret(client, headers)

    # ── Phase 2: Login → expect MFA challenge ─────────────────────────────
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "mfa_challenge"
    assert "challenge_token" in body
    assert body["challenge_token"]
    assert "access_token" not in body
    assert "expires_at" in body
    assert "allowed_methods" in body
    assert "totp" in body["allowed_methods"]

    # ── Phase 3: Verify with a valid TOTP → full access token ─────────────
    # Enrollment already consumed the current step's code (replay protection
    # records last_used_step), so verify with the NEXT step's code — still
    # within the ±1 acceptance window but past last_used_step.
    next_step_code = pyotp.TOTP(secret).at(
        datetime.now(UTC) + timedelta(seconds=30)
    )
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": body["challenge_token"],
            "method": "totp",
            "payload": {"code": next_step_code},
        },
    )
    assert r.status_code == 200, f"mfa/verify failed: {r.text}"
    verified = r.json()
    assert verified["kind"] == "token", f"Expected kind=token, got {verified}"
    assert verified["access_token"]

    # The issued token authenticates as the enrolled user.
    me = get_me(client, headers_from_token(verified))
    assert me["email"] == user["email"]


# ── 4. TOTP enrollment success ────────────────────────────────────────


def test_totp_enrollment_full_lifecycle(client: TestClient) -> None:
    """
    TOTP enrollment full scenario:
      1. /begin returns secret_base32, otpauth_uri, qr_svg_data_uri, secret_token.
      2. /finish with valid 6-digit code returns 200 with 'message'.
      3. MFA status reflects has_totp=True, enabled=True.
      4. /users/me has_totp=True, two_factor_enabled=True.
      5. Recovery codes returned on first factor enrollment.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # ── Phase 1: Begin enrollment
    r = client.post(f"{_BASE}/users/me/mfa/totp/begin", headers=headers)
    assert r.status_code == 200
    begin_body = r.json()
    assert "secret_base32" in begin_body
    assert "otpauth_uri" in begin_body
    assert "qr_svg_data_uri" in begin_body
    assert "secret_token" in begin_body
    assert begin_body["secret_base32"]
    assert begin_body["otpauth_uri"].startswith("otpauth://totp/")
    assert begin_body["qr_svg_data_uri"].startswith("data:image/svg+xml;base64,")

    # ── Phase 2: Finish with valid code
    secret = begin_body["secret_base32"]
    secret_token = begin_body["secret_token"]
    code = pyotp.TOTP(secret).now()
    r2 = client.post(
        f"{_BASE}/users/me/mfa/totp/finish",
        headers=headers,
        json={"secret_token": secret_token, "code": code},
    )
    assert r2.status_code == 200
    finish_body = r2.json()
    assert finish_body.get("message") == "TOTP enrolled"

    # ── Phase 3: Recovery codes returned on first factor
    assert "recovery_codes" in finish_body
    rc = finish_body["recovery_codes"]
    assert rc is not None
    assert len(rc["codes"]) == settings.MFA_RECOVERY_CODE_COUNT

    # ── Phase 4: MFA status reflects enrollment
    status = get_mfa_status(client, headers)
    assert status["enabled"] is True
    assert status["has_totp"] is True
    assert status["has_recovery_codes"] is True

    # ── Phase 5: /users/me reflects enrollment
    me = get_me(client, headers)
    assert me["two_factor_enabled"] is True
    assert me["has_totp"] is True


# ── 5. TOTP enrollment wrong code ────────────────────────────────────


def test_totp_enrollment_wrong_code_does_not_persist(client: TestClient) -> None:
    """Finish with wrong code returns 400 invalid_code and does NOT persist
    a UserTotpSecret row (has_totp stays False, 2FA stays off)."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    begin = totp_begin(client, headers)
    secret_token = begin["secret_token"]

    # Wrong code (inverted digits or all-zeros is unlikely to match).
    wrong_code = "000000"
    if pyotp.TOTP(begin["secret_base32"]).now() == wrong_code:
        wrong_code = "111111"

    r = client.post(
        f"{_BASE}/users/me/mfa/totp/finish",
        headers=headers,
        json={"secret_token": secret_token, "code": wrong_code},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_code"

    # Status unchanged
    status = get_mfa_status(client, headers)
    assert status["has_totp"] is False
    assert status["enabled"] is False


# ── 6. TOTP verify accepts ± 1 step ──────────────────────────────────


def test_totp_verify_accepts_adjacent_steps(client: TestClient) -> None:
    """After enrollment, the login TOTP verify path accepts the previous
    (or next) step code, simulating mild clock skew.  We test this by
    enrolling, then patching datetime.now inside the service to advance
    by exactly 30 s so the 'previous step' of the new window matches the
    code we computed at enrollment time.

    We cannot reuse the exact enrollment code because last_used_step
    tracks it and would reject a replay — instead we compute
    ``pyotp.TOTP(secret).at(t, counter_offset=-1)`` for the next window.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    begin = totp_begin(client, headers)
    secret = begin["secret_base32"]
    totp = pyotp.TOTP(secret)

    # Anchor both the enrollment code and the verification code to a single
    # fixed timestamp so a 30 s step boundary crossing mid-test cannot shift
    # the relative offset (wall-clock flake).
    import time
    anchor_ts = int(time.time())
    enroll_code = totp.at(anchor_ts)
    totp_finish(client, headers, begin["secret_token"], enroll_code)

    # Build a code for 'one step in the future' (offset +1 from the anchor step).
    code_plus1 = totp.at(anchor_ts, counter_offset=1)

    # Obtain a fresh login challenge (2FA is now on).
    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]

    # Submit the +1-step code — should be accepted (valid_window=1).
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge_token,
            "method": "totp",
            "payload": {"code": code_plus1},
        },
    )
    assert r.status_code == 200, f"Adjacent-step TOTP rejected: {r.text}"
    body = r.json()
    assert body["kind"] == "token"
    assert body["access_token"]


# ── 7. TOTP replay rejection ──────────────────────────────────────────


def test_totp_replay_rejection(client: TestClient) -> None:
    """The same 6-digit TOTP code is accepted once then rejected on a
    second attempt within the same 30-second window (last_used_step guard)."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    begin = totp_begin(client, headers)
    secret = begin["secret_base32"]
    totp = pyotp.TOTP(secret)

    # Anchor enrollment and the verification code to a single fixed timestamp
    # so a 30 s step boundary crossing mid-test cannot shift the relative
    # offset (wall-clock flake).
    import time
    anchor_ts = int(time.time())
    enroll_code = totp.at(anchor_ts)
    totp_finish(client, headers, begin["secret_token"], enroll_code)

    # Get two separate login challenges.
    challenge1 = login_get_challenge(client, user["email"], user["_password"])
    challenge2 = login_get_challenge(client, user["email"], user["_password"])

    # Compute a fresh code (offset +1 from the anchor step) so it differs from
    # the enrollment code and last_used_step does not auto-block it.
    fresh_code = totp.at(anchor_ts, counter_offset=1)

    # ── First verify should succeed.
    r1 = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge1["challenge_token"],
            "method": "totp",
            "payload": {"code": fresh_code},
        },
    )
    assert r1.status_code == 200, f"First verify failed: {r1.text}"

    # ── Second verify with the same code — last_used_step must reject it.
    r2 = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge2["challenge_token"],
            "method": "totp",
            "payload": {"code": fresh_code},
        },
    )
    assert r2.status_code == 400, f"Replay was not rejected: {r2.text}"
    assert r2.json()["detail"]["code"] == "invalid_code"


# ── 10. Recovery code one-shot ────────────────────────────────────────


def test_recovery_code_one_shot(client: TestClient) -> None:
    """Recovery code is consumed on first use and rejected on second attempt."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # Enroll TOTP (first factor) — recovery codes returned.
    finish = enroll_totp(client, headers)
    assert finish["recovery_codes"] is not None
    codes = finish["recovery_codes"]["codes"]
    assert len(codes) == settings.MFA_RECOVERY_CODE_COUNT
    first_code = codes[0]

    # ── Attempt 1: Consume the code via login challenge.
    challenge1 = login_get_challenge(client, user["email"], user["_password"])
    r1 = verify_challenge_recovery(client, challenge1["challenge_token"], first_code)
    assert r1["kind"] == "token"
    assert r1["access_token"]

    # ── Attempt 2: Same code must be rejected.
    challenge2 = login_get_challenge(client, user["email"], user["_password"])
    r2 = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge2["challenge_token"],
            "method": "recovery",
            "payload": {"code": first_code},
        },
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "invalid_code"


# ── 11. Recovery code regenerate ──────────────────────────────────────


def test_recovery_code_regenerate_invalidates_prior_batch(client: TestClient) -> None:
    """POST /recovery-codes/regenerate wipes the old batch and issues fresh codes;
    a code from the old batch is no longer usable."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # Enroll TOTP → receive first batch.
    finish = enroll_totp(client, headers)
    old_codes = finish["recovery_codes"]["codes"]

    # Regenerate — proof = password.
    r_regen = client.post(
        f"{_BASE}/users/me/mfa/recovery-codes/regenerate",
        headers=headers,
        json={"password": user["_password"]},
    )
    assert r_regen.status_code == 200, r_regen.text
    new_batch = r_regen.json()
    assert "codes" in new_batch
    new_codes = new_batch["codes"]
    assert len(new_codes) == settings.MFA_RECOVERY_CODE_COUNT

    # Old code is no longer usable.
    challenge = login_get_challenge(client, user["email"], user["_password"])
    r_old = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge["challenge_token"],
            "method": "recovery",
            "payload": {"code": old_codes[0]},
        },
    )
    assert r_old.status_code == 400
    assert r_old.json()["detail"]["code"] == "invalid_code"

    # New code works.
    challenge2 = login_get_challenge(client, user["email"], user["_password"])
    r_new = verify_challenge_recovery(client, challenge2["challenge_token"], new_codes[0])
    assert r_new["kind"] == "token"

    # Security event written for regeneration.
    assert_security_event_written(client, headers, "MFA_RECOVERY_CODES_REGENERATED")


# ── 12. Challenge expiry ──────────────────────────────────────────────


def test_challenge_expiry_returns_410(client: TestClient) -> None:
    """After the challenge TTL has elapsed, /login/mfa/verify returns 410 Gone."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_totp(client, headers)

    challenge = login_get_challenge(client, user["email"], user["_password"])
    token = challenge["challenge_token"]

    # Patch datetime.now inside the service so "now" is far in the future.
    far_future = datetime.now(UTC) + timedelta(seconds=settings.MFA_CHALLENGE_TTL_SECONDS + 60)

    import app.services.users.mfa_service as mfa_svc_module

    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=far_future)

    with patch.object(mfa_svc_module, "datetime", mock_dt):
        r = client.post(
            f"{_BASE}/login/mfa/verify",
            json={
                "challenge_token": token,
                "method": "totp",
                "payload": {"code": "123456"},
            },
        )
    assert r.status_code == 410, f"Expected 410, got {r.status_code}: {r.text}"
    assert r.json()["detail"]["code"] in ("challenge_expired", "challenge_not_found")


# ── 13. Attempt limit — 5 failed verifications lock challenge ─────────


def test_challenge_attempt_limit_returns_429(client: TestClient) -> None:
    """5 failed TOTP verifications lock the challenge; the 6th returns 429."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    finish = enroll_totp(client, headers)
    # Use a recovery code to get a fresh access token so we can obtain a challenge.
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token = challenge["challenge_token"]

    # Send 5 wrong codes.
    for i in range(settings.MFA_MAX_ATTEMPTS_PER_CHALLENGE):
        r = client.post(
            f"{_BASE}/login/mfa/verify",
            json={
                "challenge_token": token,
                "method": "totp",
                "payload": {"code": "000000"},
            },
        )
        assert r.status_code == 400, (
            f"Attempt {i+1} should return 400, got {r.status_code}"
        )

    # The 6th attempt must be blocked with 429.
    r6 = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": token,
            "method": "totp",
            "payload": {"code": "000000"},
        },
    )
    assert r6.status_code == 429, f"Expected 429, got {r6.status_code}: {r6.text}"
    assert r6.json()["detail"]["code"] == "attempt_limit_exceeded"


# ── 14. Rate limit per-user ───────────────────────────────────────────


def test_rate_limit_per_user_triggers_429(client: TestClient) -> None:
    """10 verify attempts within 5 minutes triggers 429 rate_limited and
    writes MFA_RATE_LIMITED SecurityEvent.  We patch the in-memory
    rate-limit bucket to pre-seed it near the cap."""
    import app.services.users.mfa_service as mfa_svc_module

    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_totp(client, headers)

    # Clear any existing bucket (test isolation — module-level dict).
    mfa_svc_module._verify_rate_limit_log.clear()

    # Pre-seed the bucket with exactly max entries so the next call is blocked.
    # check_verify_rate_limit tests `len(bucket) >= max` BEFORE appending,
    # so seeding with `max` entries means the very next verify attempt hits the cap.
    max_attempts = mfa_svc_module._VERIFY_RATE_LIMIT_MAX
    # We need the user's uuid from /users/me.
    me = get_me(client, headers)
    user_uuid = uuid.UUID(me["id"])
    import time
    now_ts = time.time()
    mfa_svc_module._verify_rate_limit_log[user_uuid] = [now_ts] * max_attempts

    # One more challenge: this verify attempt tips the bucket → 429.
    challenge = login_get_challenge(client, user["email"], user["_password"])
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge["challenge_token"],
            "method": "totp",
            "payload": {"code": "000000"},
        },
    )
    assert r.status_code == 429, f"Expected 429 from rate limit, got {r.status_code}: {r.text}"
    assert r.json()["detail"]["code"] == "rate_limited"

    # Cleanup.
    mfa_svc_module._verify_rate_limit_log.pop(user_uuid, None)

    # MFA_RATE_LIMITED SecurityEvent written — verify by logging in fresh and listing.
    # Use a recovery code to log in (or regenerate a new login since challenge was consumed).
    assert_security_event_written(client, headers, "MFA_RATE_LIMITED")


# ── 15. Last-factor auto-disable (TOTP) ──────────────────────────────


def test_last_factor_auto_disable_totp_delete(client: TestClient) -> None:
    """Deleting TOTP while it's the only factor auto-disables 2FA.

    Replaces the old ``last_factor_protected`` 409 contract: per the
    business rules in ``docs/development/users/user_2fa.md``, removing
    the last 2FA factor now turns 2FA off automatically (same wipe-and-
    flag flow as ``POST /mfa/disable``).  The UI surfaces a last-factor-
    aware confirmation dialog before the request fires.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_totp(client, headers)

    # Sanity-check: 2FA is on with TOTP as the only factor.
    status = client.get(f"{_BASE}/users/me/mfa/status", headers=headers).json()
    assert status["enabled"] is True
    assert status["has_totp"] is True
    assert status["has_passkey"] is False

    # DELETE TOTP with a valid step-up password proof.
    r = delete_with_body(
        client,
        f"{_BASE}/users/me/mfa/totp",
        headers=headers,
        json={"password": user["_password"]},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    # 2FA fully off; both factor flags clear; recovery codes wiped.
    status_after = client.get(
        f"{_BASE}/users/me/mfa/status", headers=headers
    ).json()
    assert status_after["enabled"] is False
    assert status_after["has_totp"] is False
    assert status_after["has_passkey"] is False
    assert status_after["has_recovery_codes"] is False

    # Audit trail records the auto-disable cause.
    events = list_security_events(client, headers, limit=20)
    auto_disable = [
        e
        for e in events
        if e.get("event_type") == "MFA_DISABLED"
        and (e.get("details") or {}).get("reason") == "last_factor_removed"
    ]
    assert auto_disable, (
        "Expected an MFA_DISABLED event with reason='last_factor_removed' "
        f"but found: {[(e.get('event_type'), e.get('details')) for e in events]}"
    )


# ── 16. Disable 2FA requires step-up ─────────────────────────────────


def test_disable_2fa_lifecycle(client: TestClient) -> None:
    """
    Disable 2FA scenario:
      1. Enroll TOTP → 2FA on.
      2. POST /mfa/disable without proof → 401 step_up_required.
      3. POST /mfa/disable with password proof → 200, 2FA off.
      4. /users/me reflects two_factor_enabled=False, has_totp=False.
      5. MFA_DISABLED SecurityEvent written.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_totp(client, headers)

    # ── Phase 2: No proof → 401
    r_no_proof = client.post(
        f"{_BASE}/users/me/mfa/disable",
        headers=headers,
        json={},
    )
    assert r_no_proof.status_code == 401
    assert r_no_proof.json()["detail"]["code"] == "step_up_required"

    # ── Phase 3: With password proof → 200
    r_ok = client.post(
        f"{_BASE}/users/me/mfa/disable",
        headers=headers,
        json={"password": user["_password"]},
    )
    assert r_ok.status_code == 200, f"disable failed: {r_ok.text}"
    assert r_ok.json()["message"] == "Two-factor authentication disabled"

    # ── Phase 4: /users/me reflects disabled state
    me = get_me(client, headers)
    assert me["two_factor_enabled"] is False
    assert me["has_totp"] is False

    # ── Phase 5: MFA status
    status = get_mfa_status(client, headers)
    assert status["enabled"] is False
    assert status["has_totp"] is False

    # ── SecurityEvent
    assert_security_event_written(client, headers, "MFA_DISABLED")


# ── 17. Auth required for all /users/me/mfa/* endpoints ──────────────


def test_mfa_endpoints_require_auth(client: TestClient) -> None:
    """Anonymous requests to all /users/me/mfa/* endpoints must be rejected
    with 401 or 403."""
    import json as _json

    # Endpoints that accept a body — use content= because TestClient.get/delete
    # do not accept json= but POST/PUT/PATCH do.
    post_endpoints = [
        f"{_BASE}/users/me/mfa/passkeys/begin",
        f"{_BASE}/users/me/mfa/passkeys/finish",
        f"{_BASE}/users/me/mfa/totp/begin",
        f"{_BASE}/users/me/mfa/totp/finish",
        f"{_BASE}/users/me/mfa/recovery-codes/regenerate",
        f"{_BASE}/users/me/mfa/disable",
    ]
    get_endpoints = [
        f"{_BASE}/users/me/mfa/status",
        f"{_BASE}/users/me/mfa/passkeys",
        f"{_BASE}/users/me/mfa/recovery-codes",
    ]
    delete_endpoints = [
        f"{_BASE}/users/me/mfa/totp",
    ]

    for url in post_endpoints:
        r = client.post(url, json={})
        assert r.status_code in (401, 403), (
            f"POST {url} returned {r.status_code} instead of 401/403"
        )
    for url in get_endpoints:
        r = client.get(url)
        assert r.status_code in (401, 403), (
            f"GET {url} returned {r.status_code} instead of 401/403"
        )
    for url in delete_endpoints:
        r = delete_with_body(client, url, headers={}, json={})
        assert r.status_code in (401, 403), (
            f"DELETE {url} returned {r.status_code} instead of 401/403"
        )


# ── 18. SecurityEvent rows written ───────────────────────────────────


def test_security_events_written_for_mfa_lifecycle(client: TestClient) -> None:
    """Verify SecurityEvent rows are written for: MFA_ENROLLED, MFA_CHALLENGE_ISSUED,
    MFA_CHALLENGE_SUCCESS, MFA_CHALLENGE_FAILED, MFA_RECOVERY_CODE_USED."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # Enrollment triggers MFA_ENROLLED + MFA_RECOVERY_CODES_REGENERATED
    finish = enroll_totp(client, headers)
    assert_security_event_written(client, headers, "MFA_ENROLLED")

    # Login issues MFA_CHALLENGE_ISSUED
    challenge = login_get_challenge(client, user["email"], user["_password"])
    assert_security_event_written(client, headers, "MFA_CHALLENGE_ISSUED")

    # Wrong code → MFA_CHALLENGE_FAILED
    r_fail = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge["challenge_token"],
            "method": "totp",
            "payload": {"code": "000000"},
        },
    )
    assert r_fail.status_code == 400
    assert_security_event_written(client, headers, "MFA_CHALLENGE_FAILED")

    # Consume a recovery code → MFA_CHALLENGE_SUCCESS + MFA_RECOVERY_CODE_USED
    codes = finish["recovery_codes"]["codes"]
    challenge2 = login_get_challenge(client, user["email"], user["_password"])
    verify_challenge_recovery(client, challenge2["challenge_token"], codes[0])
    assert_security_event_written(client, headers, "MFA_CHALLENGE_SUCCESS")
    assert_security_event_written(client, headers, "MFA_RECOVERY_CODE_USED")


# ── 20. TOTP secret_token user-binding & expiry ───────────────────────


def test_totp_secret_token_user_binding(client: TestClient) -> None:
    """secret_token minted for user A cannot be used by user B."""
    user_a = signup_user(client)
    user_b = signup_user(client)
    headers_a = login(client, user_a["email"], user_a["_password"])
    headers_b = login(client, user_b["email"], user_b["_password"])

    # User A begins enrollment → obtains secret_token.
    begin_a = totp_begin(client, headers_a)
    token_a = begin_a["secret_token"]
    code_a = pyotp.TOTP(begin_a["secret_base32"]).now()

    # User B tries to finish with A's token.
    r = client.post(
        f"{_BASE}/users/me/mfa/totp/finish",
        headers=headers_b,
        json={"secret_token": token_a, "code": code_a},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_secret_token"

    # User A can still finish successfully.
    r_ok = client.post(
        f"{_BASE}/users/me/mfa/totp/finish",
        headers=headers_a,
        json={"secret_token": token_a, "code": pyotp.TOTP(begin_a["secret_base32"]).now()},
    )
    assert r_ok.status_code == 200


def test_totp_secret_token_expiry(client: TestClient) -> None:
    """Expired secret_token is rejected (invalid_secret_token) even with a valid code."""
    import app.services.users.mfa_service as mfa_svc_module

    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    begin = totp_begin(client, headers)
    secret_token = begin["secret_token"]
    code = pyotp.TOTP(begin["secret_base32"]).now()

    # Patch datetime.now to return a time past the enrollment TTL.
    far_future = datetime.now(UTC) + timedelta(seconds=mfa_svc_module._TOTP_ENROLLMENT_TTL_SECONDS + 60)
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=far_future)

    with patch.object(mfa_svc_module, "datetime", mock_dt):
        r = client.post(
            f"{_BASE}/users/me/mfa/totp/finish",
            headers=headers,
            json={"secret_token": secret_token, "code": code},
        )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_secret_token"


# ── 21. Recovery codes only after enrollment ──────────────────────────


def test_recovery_codes_regenerate_requires_enrollment(client: TestClient) -> None:
    """POST /mfa/recovery-codes/regenerate returns 404 factor_not_enrolled
    when 2FA has not been set up."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    r = client.post(
        f"{_BASE}/users/me/mfa/recovery-codes/regenerate",
        headers=headers,
        json={"password": user["_password"]},
    )
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    assert r.json()["detail"]["code"] == "factor_not_enrolled"


# ── 23. UserPublic derived flags ──────────────────────────────────────


def test_user_public_flags_totp(client: TestClient) -> None:
    """has_totp/has_passkey/two_factor_enabled in /users/me, /users/{id},
    /users list, PATCH /users/me, and signup response reflect TOTP enrollment."""
    # Superuser headers for admin endpoints.
    su_login = client.post(
        f"{_BASE}/login/access-token",
        data={"username": settings.FIRST_SUPERUSER, "password": settings.FIRST_SUPERUSER_PASSWORD},
    ).json()
    su_headers = {"Authorization": f"Bearer {su_login['access_token']}"}

    user = signup_user(client)
    uid = user["id"]
    headers = login(client, user["email"], user["_password"])

    # ── Before enrollment: all False
    me = get_me(client, headers)
    assert me["two_factor_enabled"] is False
    assert me["has_totp"] is False
    assert me["has_passkey"] is False

    # Signup response also carries the flags.
    assert user.get("two_factor_enabled") is False or "two_factor_enabled" not in user  # noqa

    # ── Enroll TOTP
    enroll_totp(client, headers)

    # /users/me reflects enrollment
    me2 = get_me(client, headers)
    assert me2["two_factor_enabled"] is True
    assert me2["has_totp"] is True
    assert me2["has_passkey"] is False

    # /users/{id} (superuser) also reflects it
    r_user = client.get(f"{_BASE}/users/{uid}", headers=su_headers)
    assert r_user.status_code == 200
    uu = r_user.json()
    assert uu["has_totp"] is True
    assert uu["two_factor_enabled"] is True

    # /users list
    r_list = client.get(f"{_BASE}/users/", headers=su_headers)
    assert r_list.status_code == 200
    found = next((u for u in r_list.json()["data"] if u["id"] == uid), None)
    assert found is not None
    assert found["has_totp"] is True

    # PATCH /users/me (non-2FA field) does not break the flags
    r_patch = client.patch(
        f"{_BASE}/users/me",
        headers=headers,
        json={"full_name": "New Name"},
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["has_totp"] is True
    assert r_patch.json()["two_factor_enabled"] is True


# ── 19. Account deletion cascades ─────────────────────────────────────


def test_account_deletion_cascades_mfa_data(client: TestClient) -> None:
    """Deleting the user account cascades to TOTP secret and recovery code rows.
    Verified indirectly: after DELETE /users/me, the auth token is invalidated
    and the user no longer appears in the system."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_totp(client, headers)

    # Confirm enrollment
    status = get_mfa_status(client, headers)
    assert status["has_totp"] is True

    # Delete the account
    r_del = client.delete(f"{_BASE}/users/me", headers=headers)
    assert r_del.status_code == 200

    # Token no longer works
    r_me = client.get(f"{_BASE}/users/me", headers=headers)
    assert r_me.status_code in (401, 403, 404)

    # Re-signup with the same email works (user is gone)
    r_signup2 = client.post(
        f"{_BASE}/users/signup",
        json={"email": user["email"], "password": "newpassword123"},
    )
    assert r_signup2.status_code == 200
    new_user = r_signup2.json()
    assert new_user["has_totp"] is False
    assert new_user["two_factor_enabled"] is False
