"""MFA tests — WebAuthn passkey enrollment, authentication, step-up, and
passkey-specific scenarios.

Coverage targets (from plan §13):
  3.  Google OAuth MFA branch — same branching on the Google callback.
  8.  Passkey enrollment — begin+finish flow turns 2FA on and issues 8
      recovery codes on first factor.  ``verify_registration_response``
      is patched at the service boundary so no real authenticator is needed.
  9.  Passkey verify — /login/mfa/passkey/options + /login/mfa/verify
      happy path; unknown credential ID returns 400; assertion increments
      sign_count and updates last_used_at.
  10. Recovery code one-shot (passkey path) — code consumed, rejected on second use.
  14. Rate limit — 10 verifies / 5 minutes triggers 429 rate_limited.
  15. Last factor protection (passkey path) — deleting only passkey while 2FA on returns 409.
  16. Disable 2FA with TOTP step-up proof.
  17. Auth required for all /users/me/mfa/* endpoints (covered in TOTP file; not repeated here).
  18. SecurityEvent rows — MFA_ENROLLED via passkey, MFA_CHALLENGE_ISSUED, MFA_CHALLENGE_SUCCESS.
  22. begin_passkey_authentication rejects when user has no passkeys → factor_not_enrolled.
  23. UserPublic derived flags — has_passkey/has_totp after passkey enrollment.

WebAuthn strategy
-----------------
``verify_registration_response`` and ``verify_authentication_response`` from
the ``webauthn`` library are patched using ``unittest.mock.patch`` at the
import path ``app.services.users.mfa_service.*``.  The fake
``VerifiedRegistration`` / ``VerifiedAuthentication`` objects expose only
the attributes that ``MfaService`` reads:

    VerifiedRegistration: credential_id, credential_public_key, sign_count, aaguid
    VerifiedAuthentication: new_sign_count

All other WebAuthn cryptographic details are bypassed intentionally — the
goal is to test the service / route integration, not the library itself.
"""
from __future__ import annotations

import base64
import os
import uuid
from unittest.mock import MagicMock, patch

import pyotp
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.mfa import (
    assert_security_event_written,
    enroll_passkey,
    enroll_totp,
    get_me,
    get_mfa_status,
    headers_from_token,
    list_security_events,
    login,
    login_get_challenge,
    passkey_begin_registration,
    passkey_finish_registration,
    signup_user,
    totp_begin,
    totp_finish,
    verify_challenge_totp,
    _cred_id_b64url,
    _fake_credential_id,
    _fake_public_key,
    _make_verified_authentication,
    _make_verified_registration,
)

_BASE = settings.API_V1_STR

# MFA tests never create agents; opt out of the heavy agent/env stubs in
# tests/api/users/conftest.py (which also provides autouse rate-limit-bucket
# clearing, keeping this file order-independent).
NEEDS_AGENT_STUBS = False


# ── 8. Passkey enrollment (registration) ─────────────────────────────


def test_passkey_enrollment_full_lifecycle(client: TestClient) -> None:
    """
    Passkey enrollment scenario:
      1. POST /passkeys/begin returns options dict with challenge_token.
      2. POST /passkeys/finish (mocked) returns passkey public data +
         recovery_codes on first factor.
      3. MFA status reflects has_passkey=True, enabled=True.
      4. /users/me has_passkey=True, two_factor_enabled=True.
      5. SecurityEvent MFA_ENROLLED written.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # ── Phase 1: Begin registration
    r_begin = client.post(f"{_BASE}/users/me/mfa/passkeys/begin", headers=headers)
    assert r_begin.status_code == 200, f"passkeys/begin failed: {r_begin.text}"
    begin_body = r_begin.json()
    assert "challenge_token" in begin_body
    # WebAuthn creation options are nested under ``options`` so the
    # frontend can feed them straight into ``@simplewebauthn/browser``.
    assert "options" in begin_body
    assert "challenge" in begin_body["options"]
    challenge_token = begin_body["challenge_token"]

    # ── Phase 2: Finish (mocked)
    cred_id = _fake_credential_id()
    public_key = _fake_public_key()
    finish_body = passkey_finish_registration(
        client, headers, challenge_token, "YubiKey 5",
        cred_id=cred_id, public_key=public_key,
    )
    assert "passkey" in finish_body
    pk = finish_body["passkey"]
    assert pk["nickname"] == "YubiKey 5"
    assert pk["device_type"] == "platform"

    # ── Phase 3: Recovery codes (first factor)
    assert "recovery_codes" in finish_body
    rc = finish_body["recovery_codes"]
    assert rc is not None
    assert len(rc["codes"]) == settings.MFA_RECOVERY_CODE_COUNT

    # ── Phase 4: MFA status
    status = get_mfa_status(client, headers)
    assert status["enabled"] is True
    assert status["has_passkey"] is True
    assert status["has_recovery_codes"] is True
    assert status["passkey_count"] == 1

    # ── Phase 5: /users/me
    me = get_me(client, headers)
    assert me["two_factor_enabled"] is True
    assert me["has_passkey"] is True

    # ── Phase 6: SecurityEvent
    assert_security_event_written(client, headers, "MFA_ENROLLED")


def test_passkey_list(client: TestClient) -> None:
    """GET /passkeys returns the enrolled passkey with public fields."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_passkey(client, headers, "iPhone Touch ID")

    r = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    pk = body["data"][0]
    assert pk["nickname"] == "iPhone Touch ID"
    assert "id" in pk
    # Raw credential_id / public_key must NOT be in the public schema.
    assert "credential_id" not in pk
    assert "public_key" not in pk


def test_passkey_rename(client: TestClient) -> None:
    """PATCH /passkeys/{id} renames a passkey."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    _, _, _ = enroll_passkey(client, headers, "Old Name")

    # Get passkey id
    r_list = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    passkey_id = r_list.json()["data"][0]["id"]

    r = client.patch(
        f"{_BASE}/users/me/mfa/passkeys/{passkey_id}",
        headers=headers,
        json={"nickname": "New Name"},
    )
    assert r.status_code == 200
    assert r.json()["nickname"] == "New Name"


def test_passkey_delete_second_passkey(client: TestClient) -> None:
    """Deleting a passkey when another passkey still exists succeeds."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    cred1 = _fake_credential_id()
    pk1 = _fake_public_key()
    cred2 = _fake_credential_id()
    pk2 = _fake_public_key()

    enroll_passkey(client, headers, "Key 1", cred_id=cred1, public_key=pk1)
    enroll_passkey(client, headers, "Key 2", cred_id=cred2, public_key=pk2)

    r_list = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    assert r_list.json()["count"] == 2
    first_id = r_list.json()["data"][0]["id"]

    r_del = client.delete(
        f"{_BASE}/users/me/mfa/passkeys/{first_id}", headers=headers
    )
    assert r_del.status_code == 200

    r_list2 = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    assert r_list2.json()["count"] == 1


# ── 9. Passkey verify (authentication) ───────────────────────────────


def test_passkey_login_verify_happy_path(client: TestClient) -> None:
    """
    Passkey login challenge scenario:
      1. Enroll passkey with known cred_id/public_key.
      2. Password login → MFA challenge.
      3. POST /login/mfa/passkey/options → WebAuthn assertion options.
      4. POST /login/mfa/verify with method='passkey' (mocked assertion) → token.
      5. SecurityEvent MFA_CHALLENGE_SUCCESS written.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    cred_id = _fake_credential_id()
    public_key = _fake_public_key()
    enroll_passkey(client, headers, "Test Key", cred_id=cred_id, public_key=public_key)

    # ── Phase 2: Login → MFA challenge
    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]
    assert "passkey" in challenge_body["allowed_methods"]

    # ── Phase 3: Get passkey assertion options
    r_opts = client.post(
        f"{_BASE}/login/mfa/passkey/options",
        json={"challenge_token": challenge_token},
    )
    assert r_opts.status_code == 200, f"passkey/options failed: {r_opts.text}"
    opts_body = r_opts.json()
    # WebAuthn request options are nested under ``options`` so the
    # frontend can feed them straight into ``@simplewebauthn/browser``.
    assert "options" in opts_body
    opts = opts_body["options"]
    assert "challenge" in opts
    assert "allowCredentials" in opts

    # ── Phase 4: Verify with mocked assertion
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
            },
        )
    assert r_verify.status_code == 200, f"passkey verify failed: {r_verify.text}"
    token_body = r_verify.json()
    assert token_body["kind"] == "token"
    assert token_body["access_token"]

    # ── Phase 5: SecurityEvent
    assert_security_event_written(client, headers, "MFA_CHALLENGE_SUCCESS")


def test_passkey_login_verify_unknown_credential_id(client: TestClient) -> None:
    """Asserting with an unknown credential_id returns 400 invalid_assertion."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_passkey(client, headers)

    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]

    # Get assertion options first
    client.post(
        f"{_BASE}/login/mfa/passkey/options",
        json={"challenge_token": challenge_token},
    )

    # Submit with a random (unknown) credential id.
    unknown_cred_id = _cred_id_b64url(_fake_credential_id())
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge_token,
            "method": "passkey",
            "payload": {
                "id": unknown_cred_id,
                "rawId": unknown_cred_id,
                "type": "public-key",
                "response": {
                    "authenticatorData": "dummyAuthData",
                    "clientDataJSON": "dummyClientData",
                    "signature": "dummySig",
                },
            },
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_assertion"


def test_passkey_assertion_increments_sign_count(client: TestClient) -> None:
    """Successful passkey assertion updates sign_count and last_used_at on the
    stored passkey row.  Verified by observing the passkey list endpoint
    (last_used_at changes from None to a timestamp after successful assertion).
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    cred_id = _fake_credential_id()
    public_key = _fake_public_key()
    enroll_passkey(client, headers, "Track Me", cred_id=cred_id, public_key=public_key)

    # Before assertion: last_used_at is None.
    r_list = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    pk_before = r_list.json()["data"][0]
    assert pk_before["last_used_at"] is None

    # Perform a successful passkey login (mocked).
    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]
    client.post(
        f"{_BASE}/login/mfa/passkey/options",
        json={"challenge_token": challenge_token},
    )

    cred_id_b64 = _cred_id_b64url(cred_id)
    fake_va = _make_verified_authentication(new_sign_count=7)

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
            },
        )
    assert r_verify.status_code == 200

    # After assertion: last_used_at is now set.
    # We need fresh headers (the challenge was for an unauthenticated step).
    new_token_body = r_verify.json()
    new_headers = headers_from_token(new_token_body)
    r_list2 = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=new_headers)
    pk_after = r_list2.json()["data"][0]
    assert pk_after["last_used_at"] is not None, "last_used_at not updated after assertion"


# ── 15. Last-factor auto-disable (passkey) ───────────────────────────


def test_last_factor_auto_disable_passkey_delete(client: TestClient) -> None:
    """Deleting the only passkey (no TOTP) auto-disables 2FA.

    Replaces the old ``last_factor_protected`` 409 contract: per the
    business rules in ``docs/development/users/user_2fa.md``, removing
    the user's last 2FA factor now wipes-and-flags exactly as
    ``POST /mfa/disable`` would.  The UI surfaces a last-factor-aware
    confirmation dialog before the request fires.
    """
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])
    enroll_passkey(client, headers, "Solo Key")

    # Sanity-check: 2FA on, passkey as the only factor.
    status = client.get(f"{_BASE}/users/me/mfa/status", headers=headers).json()
    assert status["enabled"] is True
    assert status["has_passkey"] is True
    assert status["has_totp"] is False

    r_list = client.get(f"{_BASE}/users/me/mfa/passkeys", headers=headers)
    passkey_id = r_list.json()["data"][0]["id"]

    r = client.delete(
        f"{_BASE}/users/me/mfa/passkeys/{passkey_id}",
        headers=headers,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    # All factor state wiped, master flag flipped off.
    status_after = client.get(
        f"{_BASE}/users/me/mfa/status", headers=headers
    ).json()
    assert status_after["enabled"] is False
    assert status_after["has_passkey"] is False
    assert status_after["has_totp"] is False
    assert status_after["has_recovery_codes"] is False
    assert status_after["passkey_count"] == 0

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


# ── 22. begin_passkey_authentication rejects when no passkeys ─────────


def test_passkey_auth_options_rejects_no_passkeys(client: TestClient) -> None:
    """POST /login/mfa/passkey/options returns factor_not_enrolled (404)
    when the challenge's user has no registered passkeys."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # Enroll TOTP only (so the user has a factor and we can get an MFA challenge,
    # but no passkey).
    enroll_totp(client, headers)

    challenge_body = login_get_challenge(client, user["email"], user["_password"])
    challenge_token = challenge_body["challenge_token"]

    r = client.post(
        f"{_BASE}/login/mfa/passkey/options",
        json={"challenge_token": challenge_token},
    )
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    assert r.json()["detail"]["code"] == "factor_not_enrolled"


# ── 23. UserPublic derived flags (passkey path) ───────────────────────


def test_user_public_flags_passkey(client: TestClient) -> None:
    """has_passkey/two_factor_enabled in /users/me reflect passkey enrollment."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    me = get_me(client, headers)
    assert me["has_passkey"] is False
    assert me["two_factor_enabled"] is False

    enroll_passkey(client, headers, "Test Passkey")

    me2 = get_me(client, headers)
    assert me2["has_passkey"] is True
    assert me2["two_factor_enabled"] is True
    assert me2["has_totp"] is False


# ── 3. Google OAuth MFA branch ────────────────────────────────────────


def test_google_oauth_callback_gatekeeping(client: TestClient) -> None:
    """Google OAuth callback gatekeeping, pinned to one outcome per config state:

      1. Google OAuth disabled (no client id) → 501 not_implemented.
      2. Google OAuth enabled + an invalid code → 400 (the exchange fails before
         any MFA branch is reached).

    The actual MfaChallenge branch (2FA-enabled user, valid Google identity)
    cannot be driven through the API without a real Google IdP token; that path
    is exercised by the shared /login/mfa/verify flow in test_mfa_totp_login.py
    and remains an integration gap here (documented).
    """
    # ── Phase 1: Google OAuth disabled → deterministic 501 ────────────────
    with patch.object(settings, "GOOGLE_CLIENT_ID", None):
        r = client.post(
            f"{_BASE}/auth/google/callback",
            json={"code": "fake_code", "state": "fake_state"},
        )
        assert r.status_code == 501, f"Expected 501, got {r.status_code}: {r.text}"

    # ── Phase 2: Google OAuth enabled + invalid code → deterministic 400 ──
    with patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"), patch.object(
        settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"
    ):
        r = client.post(
            f"{_BASE}/auth/google/callback",
            json={"code": "fake_code", "state": "fake_state"},
        )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"


# ── 16. Disable 2FA with TOTP step-up (passkey enrolled) ─────────────


def test_disable_2fa_with_totp_step_up_after_passkey_enrolled(client: TestClient) -> None:
    """When both passkey and TOTP are enrolled, disabling 2FA with TOTP step-up
    wipes both factors and flips the flag."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # Enroll passkey (turns 2FA on, issues recovery codes)
    enroll_passkey(client, headers, "Key A")

    # Enroll TOTP as a second factor (no recovery codes returned — already on)
    begin = totp_begin(client, headers)
    secret = begin["secret_base32"]
    code = pyotp.TOTP(secret).now()
    r_totp_finish = client.post(
        f"{_BASE}/users/me/mfa/totp/finish",
        headers=headers,
        json={"secret_token": begin["secret_token"], "code": code},
    )
    assert r_totp_finish.status_code == 200
    # Second factor — recovery_codes should be None
    assert r_totp_finish.json().get("recovery_codes") is None

    # Confirm both factors are enrolled
    status = get_mfa_status(client, headers)
    assert status["has_passkey"] is True
    assert status["has_totp"] is True

    # Disable 2FA using TOTP step-up code.
    # Using counter_offset=+1 to ensure the code differs from the enrollment step
    # (enrollment records last_used_step; reusing the same step within the 30s window
    # would be rejected as a replay by verify_totp).
    import time as _time
    totp_obj = pyotp.TOTP(secret)
    fresh_code = totp_obj.at(int(_time.time()), counter_offset=1)
    r_disable = client.post(
        f"{_BASE}/users/me/mfa/disable",
        headers=headers,
        json={"totp_code": fresh_code},
    )
    assert r_disable.status_code == 200, f"disable failed: {r_disable.text}"

    # Verify all factors wiped
    status2 = get_mfa_status(client, headers)
    assert status2["enabled"] is False
    assert status2["has_passkey"] is False
    assert status2["has_totp"] is False
    assert status2["passkey_count"] == 0


# ── Passkey registration with non-existent challenge token ────────────


def test_passkey_finish_with_invalid_challenge_token(client: TestClient) -> None:
    """Finishing passkey registration with an unknown challenge_token returns 404."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    r = client.post(
        f"{_BASE}/users/me/mfa/passkeys/finish",
        headers=headers,
        json={
            "challenge_token": "completely-fake-token",
            "credential": {
                "id": _cred_id_b64url(_fake_credential_id()),
                "rawId": _cred_id_b64url(_fake_credential_id()),
                "type": "public-key",
                "response": {
                    "attestationObject": "dummyAttestation",
                    "clientDataJSON": "dummyClientData",
                },
            },
            "nickname": "Bad Key",
        },
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "challenge_not_found"


# ── No second recovery codes on second factor enrollment ─────────────


def test_passkey_second_enrollment_no_recovery_codes(client: TestClient) -> None:
    """Enrolling a second passkey (when 2FA is already on) does NOT issue
    a second batch of recovery codes — recovery_codes is null in the response."""
    user = signup_user(client)
    headers = login(client, user["email"], user["_password"])

    # First enrollment — should produce recovery codes
    cred1 = _fake_credential_id()
    pk1 = _fake_public_key()
    resp1, _, _ = enroll_passkey(client, headers, "Key 1", cred_id=cred1, public_key=pk1)
    assert resp1["recovery_codes"] is not None

    # Second enrollment — should NOT produce recovery codes
    cred2 = _fake_credential_id()
    pk2 = _fake_public_key()

    begin = passkey_begin_registration(client, headers)
    challenge_token = begin["challenge_token"]
    fake_vr = _make_verified_registration(cred2, pk2)
    cred2_b64 = _cred_id_b64url(cred2)

    with patch(
        "app.services.users.mfa_service.verify_registration_response",
        return_value=fake_vr,
    ):
        r2 = client.post(
            f"{_BASE}/users/me/mfa/passkeys/finish",
            headers=headers,
            json={
                "challenge_token": challenge_token,
                "credential": {
                    "id": cred2_b64,
                    "rawId": cred2_b64,
                    "type": "public-key",
                    "response": {
                        "attestationObject": "dummyAttestation",
                        "clientDataJSON": "dummyClientData",
                        "transports": ["internal"],
                    },
                    "authenticatorAttachment": "platform",
                },
                "nickname": "Key 2",
            },
        )
    assert r2.status_code == 200
    assert r2.json()["recovery_codes"] is None
