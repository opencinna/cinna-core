"""MFA trusted-device tests — "Do not ask on this device" sub-feature.

Coverage:
  1.  Mint via TOTP + ``remember_device_days=7`` → ``trusted_device_token`` non-null.
  2.  No duration (``remember_device_days=null``) → ``trusted_device_token`` null/absent.
  3.  Skip on password login: minted token in ``X-Trusted-Device`` → ``kind="token"``.
  4.  No skip without the header → ``kind="mfa_challenge"``.
  5.  Forged/garbage ``X-Trusted-Device`` value → ``kind="mfa_challenge"`` (graceful).
  6.  Token minted for user A does NOT skip for user B → user B gets challenge.
  7.  Expired token → ``kind="mfa_challenge"`` (monkeypatched clock, same pattern as
      existing challenge-expiry tests).
  8.  ``remember_device_days`` outside {1,7,30} (0, 5, 365) → 422 at the route edge.
  9.  Trust applies via recovery-code method too.
  10. Wipe-on-disable: ``POST /mfa/disable`` wipes trusted devices; subsequent login
      with the old token gets a challenge.
  11. Wipe on last-factor TOTP removal: ``DELETE /totp`` (last factor) wipes trusted
      devices; subsequent login with the old token does NOT skip (2FA is now off, so
      login already returns a token regardless, but the device row is gone).
  12. Wipe on last-factor passkey removal: ``DELETE /passkeys/{id}`` (last factor)
      wipes trusted devices; device row gone.
  13. Security events: ``MFA_TRUSTED_DEVICE_REGISTERED`` on mint,
      ``MFA_TRUSTED_DEVICE_USED`` on skip.
  14. Mint via passkey (mocked WebAuthn) returns a non-null ``trusted_device_token``.

Clock-manipulation strategy:
  Patch ``app.services.users.mfa_service.datetime`` with ``mock_dt`` whose
  ``.now()`` returns a far-future timestamp — identical to the pattern used
  in ``test_challenge_expiry_returns_410`` and ``test_totp_secret_token_expiry``
  in this same test suite.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import pyotp
from fastapi.testclient import TestClient

import pytest
import app.services.users.mfa_service as _mfa_svc
from app.core.config import settings
from tests.utils.mfa import (
    assert_security_event_written,
    delete_with_body,
    enroll_passkey,
    enroll_totp,
    enroll_totp_and_get_secret,
    find_security_events,
    get_mfa_status,
    headers_from_token,
    login,
    login_get_challenge,
    login_with_trusted_device,
    passkey_begin_registration,
    passkey_finish_registration,
    signup_user,
    totp_begin,
    totp_finish,
    verify_challenge_recovery_remember,
    verify_challenge_totp_remember,
    _cred_id_b64url,
    _fake_credential_id,
    _fake_public_key,
    _make_verified_authentication,
)

_BASE = settings.API_V1_STR


@pytest.fixture(autouse=True)
def _clear_rate_limit_buckets():
    """Clear the in-memory rate-limit logs before each test.

    ``/login/mfa/verify`` has two in-memory guards:
    - per-user bucket  (``_verify_rate_limit_log``, keyed by ``user.id``)
    - per-source bucket (``_anonymous_verify_rate_limit_log``, keyed by
      client IP — always ``"testclient"`` in TestClient runs)

    These are module-level dicts that accumulate across tests in the same
    process.  The existing rate-limit test (``test_rate_limit_per_user_triggers_429``)
    already clears the per-user bucket manually.  We clear both here so that
    tests in this file do not trip the anonymous limit after the Nth verify
    call across the session (anonymous cap = 20 / 5 min).  We also clean up
    after the test to avoid contaminating subsequent tests.
    """
    _mfa_svc._verify_rate_limit_log.clear()
    _mfa_svc._anonymous_verify_rate_limit_log.clear()
    yield
    _mfa_svc._verify_rate_limit_log.clear()
    _mfa_svc._anonymous_verify_rate_limit_log.clear()


# ── 1. Mint on verify — with duration ──────────────────────────────────


def test_trusted_device_minted_when_remember_device_days_set(client: TestClient) -> None:
    """
    Mint scenario:
      1. Enroll TOTP.
      2. Obtain a login challenge.
      3. Verify with ``remember_device_days=7`` → ``LoginToken`` with non-null
         ``trusted_device_token``.
      4. The token is a non-empty string.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    # ── Phase 2: Obtain login challenge
    challenge = login_get_challenge(client, user["email"], user["_password"])

    # ── Phase 3: Verify with remember_device_days=7
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=7
    )

    # ── Phase 4: Assert token present
    assert token_body["kind"] == "token"
    assert token_body["access_token"]
    assert token_body.get("trusted_device_token") is not None
    assert isinstance(token_body["trusted_device_token"], str)
    assert len(token_body["trusted_device_token"]) > 0


# ── 2. No duration → no token ─────────────────────────────────────────


def test_trusted_device_not_minted_when_no_duration(client: TestClient) -> None:
    """
    No-duration scenario:
      1. Enroll TOTP.
      2. Verify with ``remember_device_days=null`` (omitted).
      3. ``LoginToken.trusted_device_token`` must be null/absent.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=None
    )

    assert token_body["kind"] == "token"
    assert token_body["access_token"]
    # null or absent both satisfy "no device token"
    assert not token_body.get("trusted_device_token")


# ── 3 & 4. Skip vs challenge — with/without header ────────────────────


def test_trusted_device_skip_and_no_skip_scenarios(client: TestClient) -> None:
    """
    Combined skip / no-skip scenario:
      1. Enroll TOTP.
      2. Verify with ``remember_device_days=30`` → mint device token.
      3. Login WITH ``X-Trusted-Device`` header → ``kind="token"`` (skip).
      4. Login WITHOUT the header → ``kind="mfa_challenge"`` (challenge issued).
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    # ── Phase 2: Mint a trusted device token
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=30
    )
    device_token = token_body["trusted_device_token"]
    assert device_token

    # ── Phase 3: Skip — with header → LoginToken
    skip_body = login_with_trusted_device(
        client, user["email"], user["_password"], device_token
    )
    assert skip_body["kind"] == "token", (
        f"Expected kind=token on skip, got: {skip_body}"
    )
    assert skip_body["access_token"]
    # A skip login does NOT mint a new device token
    assert not skip_body.get("trusted_device_token")

    # ── Phase 4: No skip — without header → MfaChallenge
    r_no_header = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
    )
    assert r_no_header.status_code == 200
    no_skip_body = r_no_header.json()
    assert no_skip_body["kind"] == "mfa_challenge", (
        f"Expected mfa_challenge without header, got: {no_skip_body}"
    )
    assert "challenge_token" in no_skip_body
    assert "access_token" not in no_skip_body


# ── 5. Forged token → graceful challenge fallthrough ──────────────────


def test_forged_trusted_device_token_returns_mfa_challenge(client: TestClient) -> None:
    """
    Forged-token scenario:
      1. Enroll TOTP.
      2. Send a random garbage token in ``X-Trusted-Device``.
      3. Response is ``kind="mfa_challenge"`` — no error, no oracle.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_totp(client, headers)

    garbage_token = secrets.token_urlsafe(32)  # random — not in DB
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
        headers={"X-Trusted-Device": garbage_token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "mfa_challenge", (
        f"Expected mfa_challenge for garbage token, got: {body}"
    )
    # Response shape must be indistinguishable from "no token presented"
    assert "challenge_token" in body
    assert "access_token" not in body


# ── 6. Cross-user isolation ────────────────────────────────────────────


def test_trusted_device_token_does_not_skip_for_different_user(
    client: TestClient,
) -> None:
    """
    Cross-user isolation scenario:
      1. Enroll TOTP for user A; mint a device token.
      2. Enroll TOTP for user B (independent).
      3. Present user A's token on user B's login → challenge, not skip.
      4. User A's own token still skips for user A.
    """
    # ── Phase 1: user A — enroll and mint
    user_a = signup_user(client)
    headers_a = login(client, user_a["email"], user_a["_password"])
    secret_a = enroll_totp_and_get_secret(client, headers_a)

    challenge_a = login_get_challenge(client, user_a["email"], user_a["_password"])
    token_body_a = verify_challenge_totp_remember(
        client, challenge_a["challenge_token"], secret_a, remember_device_days=7
    )
    device_token_a = token_body_a["trusted_device_token"]
    assert device_token_a

    # ── Phase 2: user B — enroll TOTP (needs its own 2FA so the branch fires)
    user_b = signup_user(client)
    headers_b = login(client, user_b["email"], user_b["_password"])
    enroll_totp(client, headers_b)

    # ── Phase 3: user A's token on user B's login → challenge
    r_b = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user_b["email"], "password": user_b["_password"]},
        headers={"X-Trusted-Device": device_token_a},
    )
    assert r_b.status_code == 200
    body_b = r_b.json()
    assert body_b["kind"] == "mfa_challenge", (
        f"user A's token should NOT skip user B's challenge; got: {body_b}"
    )

    # ── Phase 4: user A's token still works for user A
    skip_a = login_with_trusted_device(
        client, user_a["email"], user_a["_password"], device_token_a
    )
    assert skip_a["kind"] == "token", (
        f"user A's own token should skip; got: {skip_a}"
    )


# ── 7. Expired token → challenge (monkeypatched clock) ────────────────


def test_expired_trusted_device_token_does_not_skip(client: TestClient) -> None:
    """
    Expiry scenario:
      1. Enroll TOTP and mint a device token normally.
      2. Monkeypatch ``datetime.now`` in ``mfa_service`` to return a
         far-future timestamp (same pattern as ``test_challenge_expiry_returns_410``).
      3. While the clock is in the future, the ``consume_trusted_device`` call
         must find no live rows → falls through → ``kind="mfa_challenge"``.

    We only monkeypatch during the login call (where ``consume_trusted_device``
    reads the clock); the mint call runs with the real clock.
    """
    import app.services.users.mfa_service as mfa_svc_module

    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    # Mint with the real clock so ``expires_at = now + 1 day`` (valid).
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=1
    )
    device_token = token_body["trusted_device_token"]
    assert device_token

    # Advance the clock past expiry: +2 days puts us beyond the 1-day window.
    far_future = datetime.now(UTC) + timedelta(days=2)
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now = MagicMock(return_value=far_future)

    with patch.object(mfa_svc_module, "datetime", mock_dt):
        r = client.post(
            f"{_BASE}/login/access-token",
            data={"username": user["email"], "password": user["_password"]},
            headers={"X-Trusted-Device": device_token},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "mfa_challenge", (
        f"Expired token should NOT skip; got: {body}"
    )


# ── 8. Allowlist enforcement — 422 at route edge ──────────────────────


def test_remember_device_days_outside_allowlist_returns_422(
    client: TestClient,
) -> None:
    """
    Allowlist scenario:
      ``remember_device_days`` values outside {1, 7, 30} are rejected at
      the Pydantic edge (``Literal[1,7,30]|None``) with HTTP 422.

      We need an active challenge to call the endpoint.  We create a user,
      enroll TOTP, and get a challenge; then we try invalid durations.
      The 422 fires before any business logic runs.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    for bad_days in [0, 5, 365, -1, 100]:
        challenge = login_get_challenge(client, user["email"], user["_password"])
        code = pyotp.TOTP(secret).now()
        r = client.post(
            f"{_BASE}/login/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "method": "totp",
                "payload": {"code": code},
                "remember_device_days": bad_days,
            },
        )
        assert r.status_code == 422, (
            f"remember_device_days={bad_days} expected 422, got {r.status_code}: {r.text}"
        )


# ── 9. Trust via recovery-code method ─────────────────────────────────


def test_trusted_device_minted_via_recovery_code_method(client: TestClient) -> None:
    """
    Recovery-code mint scenario:
      1. Enroll TOTP → receive recovery codes.
      2. Verify login challenge via recovery code with ``remember_device_days=1``.
      3. ``LoginToken.trusted_device_token`` is non-null.
      4. The minted token skips the challenge on the next login.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    finish = enroll_totp(client, headers)
    recovery_codes = finish["recovery_codes"]["codes"]
    first_code = recovery_codes[0]

    # ── Phase 2: Verify via recovery code + remember
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_recovery_remember(
        client, challenge["challenge_token"], first_code, remember_device_days=1
    )

    # ── Phase 3: Token non-null
    assert token_body["kind"] == "token"
    assert token_body.get("trusted_device_token"), (
        f"Expected trusted_device_token on recovery-code verify: {token_body}"
    )
    device_token = token_body["trusted_device_token"]

    # ── Phase 4: Skip works on next login
    skip_body = login_with_trusted_device(
        client, user["email"], user["_password"], device_token
    )
    assert skip_body["kind"] == "token", (
        f"Token minted via recovery code should skip: {skip_body}"
    )


# ── 10. Wipe on disable 2FA ───────────────────────────────────────────


def test_trusted_device_wiped_on_mfa_disable(client: TestClient) -> None:
    """
    Wipe-on-disable scenario:
      1. Enroll TOTP; mint a trusted-device token.
      2. Verify the token skips the challenge.
      3. POST /mfa/disable with password proof.
      4. Login with the old token: 2FA is now off, so login returns a
         ``LoginToken`` directly via the no-2FA branch.  The important
         assertion is that the token is NOT what gave us the login — we
         verify that by re-enabling 2FA (re-enrolling TOTP) and confirming
         the old token no longer skips (i.e. the device row was wiped).
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    # ── Phase 1: Mint token
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=7
    )
    device_token = token_body["trusted_device_token"]
    assert device_token

    # ── Phase 2: Confirm skip works before disable
    pre_disable = login_with_trusted_device(
        client, user["email"], user["_password"], device_token
    )
    assert pre_disable["kind"] == "token"

    # ── Phase 3: Disable 2FA
    r_disable = client.post(
        f"{_BASE}/users/me/mfa/disable",
        headers=headers,
        json={"password": user["_password"]},
    )
    assert r_disable.status_code == 200, f"disable failed: {r_disable.text}"

    # 2FA is now off; MFA status confirms.
    status = get_mfa_status(client, headers)
    assert status["enabled"] is False

    # ── Phase 4: Re-enroll TOTP so 2FA is on again
    headers_post_disable = login(client, user["email"], user["_password"])
    new_secret = enroll_totp_and_get_secret(client, headers_post_disable)

    # ── Phase 5: The OLD device token must NOT skip the challenge
    r_old_token = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
        headers={"X-Trusted-Device": device_token},
    )
    assert r_old_token.status_code == 200
    old_body = r_old_token.json()
    assert old_body["kind"] == "mfa_challenge", (
        "Old device token should NOT skip after 2FA was disabled and re-enabled; "
        f"got: {old_body}"
    )


# ── 11. Wipe on last-factor TOTP removal ─────────────────────────────


def test_trusted_device_wiped_on_last_totp_factor_removal(
    client: TestClient,
) -> None:
    """
    Last-TOTP-removal wipe scenario:
      1. Enroll TOTP; mint a trusted-device token.
      2. DELETE /users/me/mfa/totp (last factor → auto-disables 2FA + wipes devices).
      3. Re-enroll TOTP so 2FA is on again.
      4. The OLD device token must NOT skip.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    # ── Phase 1: Mint device token
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=7
    )
    device_token = token_body["trusted_device_token"]
    assert device_token

    # ── Phase 2: Delete TOTP (last factor — auto-disables 2FA)
    r_del = delete_with_body(
        client,
        f"{_BASE}/users/me/mfa/totp",
        headers=headers,
        json={"password": user["_password"]},
    )
    assert r_del.status_code == 200, f"totp delete failed: {r_del.text}"

    # Confirm 2FA is off
    status = get_mfa_status(client, headers)
    assert status["enabled"] is False

    # ── Phase 3: Re-enroll TOTP
    headers_fresh = login(client, user["email"], user["_password"])
    enroll_totp_and_get_secret(client, headers_fresh)

    # ── Phase 4: Old token must NOT skip
    r_old = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
        headers={"X-Trusted-Device": device_token},
    )
    assert r_old.status_code == 200
    old_body = r_old.json()
    assert old_body["kind"] == "mfa_challenge", (
        "Old device token should NOT skip after last-factor TOTP removal; "
        f"got: {old_body}"
    )


# ── 12. Wipe on last-factor passkey removal ───────────────────────────


def test_trusted_device_wiped_on_last_passkey_factor_removal(
    client: TestClient,
) -> None:
    """
    Last-passkey-removal wipe scenario:
      1. Enroll passkey (mocked); mint a trusted-device token.
      2. DELETE /users/me/mfa/passkeys/{id} (last factor → auto-disables 2FA + wipes).
      3. Re-enroll TOTP so 2FA is on again.
      4. The OLD device token must NOT skip.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # ── Phase 1: Enroll passkey
    cred_id = _fake_credential_id()
    public_key = _fake_public_key()
    enroll_passkey(client, headers, "Test Passkey", cred_id=cred_id, public_key=public_key)

    # Recover the passkey id
    r_list = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    assert r_list.status_code == 200
    passkey_id = r_list.json()["data"][0]["id"]

    # Mint a trusted-device token via TOTP ... wait, only passkey is enrolled.
    # Use passkey assertion to get a token.  We mock verify_authentication_response.
    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]

    client.post(
        f"{_BASE}/login/mfa/passkey/options",
        json={"challenge_token": challenge_token},
    )

    cred_id_b64 = _cred_id_b64url(cred_id)
    fake_va = _make_verified_authentication(new_sign_count=1)

    with patch(
        "app.services.users.mfa_service.verify_authentication_response",
        return_value=fake_va,
    ):
        r_verify = client.post(
            f"{_BASE}/login/mfa/verify",
            json={
                "challenge_token": challenge_token,
                "method": "passkey",
                "payload": {
                    "id": cred_id_b64,
                    "rawId": cred_id_b64,
                    "type": "public-key",
                    "response": {
                        "authenticatorData": "dummyAuthData",
                        "clientDataJSON": "dummyClientData",
                        "signature": "dummySig",
                    },
                },
                "remember_device_days": 7,
            },
        )
    assert r_verify.status_code == 200, f"passkey verify failed: {r_verify.text}"
    token_body = r_verify.json()
    device_token = token_body.get("trusted_device_token")
    assert device_token, f"Expected trusted_device_token from passkey verify: {token_body}"

    # ── Phase 2: Delete last passkey (auto-disables 2FA + wipes devices)
    r_del = client.delete(
        f"{_BASE}/users/me/mfa/passkeys/{passkey_id}", headers=headers
    )
    assert r_del.status_code == 200, f"passkey delete failed: {r_del.text}"

    # Confirm 2FA is off
    status = get_mfa_status(client, headers)
    assert status["enabled"] is False

    # ── Phase 3: Re-enroll TOTP to get 2FA back on
    headers_fresh = login(client, user["email"], user["_password"])
    enroll_totp_and_get_secret(client, headers_fresh)

    # ── Phase 4: Old token must NOT skip
    r_old = client.post(
        f"{_BASE}/login/access-token",
        data={"username": user["email"], "password": user["_password"]},
        headers={"X-Trusted-Device": device_token},
    )
    assert r_old.status_code == 200
    old_body = r_old.json()
    assert old_body["kind"] == "mfa_challenge", (
        "Old device token should NOT skip after last-factor passkey removal; "
        f"got: {old_body}"
    )


# ── 13. Security events ────────────────────────────────────────────────


def test_security_events_for_trusted_device_lifecycle(client: TestClient) -> None:
    """
    Security-event scenario:
      1. Enroll TOTP; verify with ``remember_device_days=7`` →
         ``MFA_TRUSTED_DEVICE_REGISTERED`` event written with ``days``
         and ``device_id`` in details.
      2. Login with the device token (skip) →
         ``MFA_TRUSTED_DEVICE_USED`` event written with ``device_id``.
      3. Both ``device_id`` values match (same device row).
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)

    # ── Phase 1: Mint → REGISTERED event
    challenge = login_get_challenge(client, user["email"], user["_password"])
    token_body = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, remember_device_days=7
    )
    device_token = token_body["trusted_device_token"]
    assert device_token

    # Use the new access token for event inspection
    fresh_headers = headers_from_token(token_body)
    registered_events = find_security_events(
        client, fresh_headers, "MFA_TRUSTED_DEVICE_REGISTERED"
    )
    assert registered_events, "Expected MFA_TRUSTED_DEVICE_REGISTERED event after mint"
    reg_event = registered_events[-1]  # most recent
    reg_details = reg_event.get("details") or {}
    assert reg_details.get("days") == 7, f"Expected days=7 in event details: {reg_details}"
    assert "device_id" in reg_details, f"Expected device_id in event details: {reg_details}"
    device_id = reg_details["device_id"]

    # ── Phase 2: Skip → USED event
    skip_body = login_with_trusted_device(
        client, user["email"], user["_password"], device_token
    )
    assert skip_body["kind"] == "token"

    # Obtain fresh headers from the skip login to query events
    skip_headers = headers_from_token(skip_body)
    used_events = find_security_events(
        client, skip_headers, "MFA_TRUSTED_DEVICE_USED"
    )
    assert used_events, "Expected MFA_TRUSTED_DEVICE_USED event after skip"
    used_event = used_events[-1]
    used_details = used_event.get("details") or {}
    assert used_details.get("device_id") == device_id, (
        f"device_id in USED event ({used_details.get('device_id')!r}) "
        f"must match REGISTERED event ({device_id!r})"
    )


# ── 14. Mint via passkey method ───────────────────────────────────────


def test_trusted_device_minted_via_passkey_method(client: TestClient) -> None:
    """
    Passkey-path mint scenario:
      1. Enroll passkey (mocked WebAuthn).
      2. Verify login challenge via passkey with ``remember_device_days=7``.
      3. ``LoginToken.trusted_device_token`` is non-null.
      4. The minted token skips the challenge on the next login.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    cred_id = _fake_credential_id()
    public_key = _fake_public_key()
    enroll_passkey(client, headers, "Skip Key", cred_id=cred_id, public_key=public_key)

    # ── Phase 2: Login → challenge
    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]

    # Get passkey assertion options (required before verifying)
    client.post(
        f"{_BASE}/login/mfa/passkey/options",
        json={"challenge_token": challenge_token},
    )

    cred_id_b64 = _cred_id_b64url(cred_id)
    fake_va = _make_verified_authentication(new_sign_count=1)

    with patch(
        "app.services.users.mfa_service.verify_authentication_response",
        return_value=fake_va,
    ):
        r_verify = client.post(
            f"{_BASE}/login/mfa/verify",
            json={
                "challenge_token": challenge_token,
                "method": "passkey",
                "payload": {
                    "id": cred_id_b64,
                    "rawId": cred_id_b64,
                    "type": "public-key",
                    "response": {
                        "authenticatorData": "dummyAuthData",
                        "clientDataJSON": "dummyClientData",
                        "signature": "dummySig",
                    },
                },
                "remember_device_days": 7,
            },
        )
    assert r_verify.status_code == 200, f"passkey verify failed: {r_verify.text}"
    token_body = r_verify.json()

    # ── Phase 3: Token non-null
    assert token_body["kind"] == "token"
    device_token = token_body.get("trusted_device_token")
    assert device_token, (
        f"Expected trusted_device_token on passkey verify: {token_body}"
    )

    # ── Phase 4: Skip on next login
    skip_body = login_with_trusted_device(
        client, user["email"], user["_password"], device_token
    )
    assert skip_body["kind"] == "token", (
        f"Token minted via passkey should skip: {skip_body}"
    )


# ── Mint with each allowed duration value ─────────────────────────────


def test_all_valid_remember_device_days_values_produce_token(
    client: TestClient,
) -> None:
    """
    Allowlist acceptance scenario:
      For each valid duration in {1, 7, 30}, verify that a ``LoginToken``
      with a non-null ``trusted_device_token`` is returned.
    """
    for days in [1, 7, 30]:
        user = signup_user(client)
        headers = login(client, user["email"], user["_password"])
        secret = enroll_totp_and_get_secret(client, headers)

        challenge = login_get_challenge(client, user["email"], user["_password"])
        token_body = verify_challenge_totp_remember(
            client, challenge["challenge_token"], secret, remember_device_days=days
        )
        assert token_body["kind"] == "token", (
            f"days={days}: expected kind=token, got: {token_body}"
        )
        assert token_body.get("trusted_device_token"), (
            f"days={days}: expected non-null trusted_device_token, got: {token_body}"
        )


# ── Security-event detail structure ───────────────────────────────────


def test_trusted_device_registered_event_details_structure(
    client: TestClient,
) -> None:
    """
    REGISTERED event detail-structure scenario:
      ``MFA_TRUSTED_DEVICE_REGISTERED`` must carry both ``days`` and
      ``device_id`` in its details, matching the requested duration.
      Exercises all three valid durations to confirm the ``days`` field
      reflects the actual requested window.
    """
    for days in [1, 7, 30]:
        user = signup_user(client)
        headers = login(client, user["email"], user["_password"])
        secret = enroll_totp_and_get_secret(client, headers)

        challenge = login_get_challenge(client, user["email"], user["_password"])
        token_body = verify_challenge_totp_remember(
            client, challenge["challenge_token"], secret, remember_device_days=days
        )
        fresh_headers = headers_from_token(token_body)

        events = find_security_events(
            client, fresh_headers, "MFA_TRUSTED_DEVICE_REGISTERED"
        )
        assert events, f"days={days}: no MFA_TRUSTED_DEVICE_REGISTERED event"
        latest = events[-1]
        details = latest.get("details") or {}
        assert details.get("days") == days, (
            f"days={days}: event details.days={details.get('days')!r}"
        )
        assert details.get("device_id"), (
            f"days={days}: event details.device_id missing or empty"
        )
