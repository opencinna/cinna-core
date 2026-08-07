"""MFA test utilities.

Provides helpers for the 2FA / passkey / TOTP test suite.  All state
changes go through the API — no direct DB access per project convention.

WebAuthn strategy
-----------------
The webauthn library verifies attestation and assertion objects
cryptographically; we cannot construct valid COSE-encoded payloads
without a real or software authenticator.  Therefore, tests that require
passkey *registration* or *assertion verification* patch the
``verify_registration_response`` / ``verify_authentication_response``
functions at the service boundary using ``unittest.mock.patch``.  Tests
that exercise the *non-WebAuthn* paths (TOTP, recovery codes, challenge
lifecycle, rate-limit) use the API directly without any patching.

The fake ``VerifiedRegistration`` / ``VerifiedAuthentication`` objects
returned by the mocks satisfy exactly the interface attributes that
``MfaService`` reads — no more, no less.
"""
from __future__ import annotations

import base64
import os
import uuid
from unittest.mock import MagicMock, patch

import pyotp
from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = settings.API_V1_STR


# ── Low-level API helpers ──────────────────────────────────────────────


def delete_with_body(
    client: TestClient, url: str, *, headers: dict[str, str], json: dict
):
    """``DELETE`` with a JSON body.

    ``httpx.Client.delete()`` deliberately omits the ``json`` parameter
    (RFC 7231 makes DELETE bodies legal but advisory), so we drop to the
    generic ``request()`` API.  Centralised here so callers don't have to
    repeat the same workaround comment in every test.
    """
    return client.request("DELETE", url, headers=headers, json=json)


def signup_user(client: TestClient, email: str | None = None, password: str | None = None) -> dict:
    """Create a fresh user via the signup API and return the response + '_password'."""
    import secrets
    email = email or f"mfa-test-{secrets.token_hex(6)}@example.com"
    password = password or f"passw0rd-{secrets.token_hex(8)}"
    r = client.post(f"{_BASE}/users/signup", json={"email": email, "password": password})
    assert r.status_code == 200, f"signup failed: {r.text}"
    data = r.json()
    data["_password"] = password
    return data


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    """Log in and return auth headers.  Only works when 2FA is off."""
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.text}"
    body = r.json()
    assert body.get("kind") == "token", f"Expected token, got: {body}"
    return {"Authorization": f"Bearer {body['access_token']}"}


def get_me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{_BASE}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()


def get_mfa_status(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{_BASE}/users/me/mfa/status", headers=headers)
    assert r.status_code == 200
    return r.json()


# ── TOTP helpers ───────────────────────────────────────────────────────


def totp_begin(client: TestClient, headers: dict[str, str]) -> dict:
    """Call POST /users/me/mfa/totp/begin and assert 200."""
    r = client.post(f"{_BASE}/users/me/mfa/totp/begin", headers=headers)
    assert r.status_code == 200, f"totp/begin failed: {r.text}"
    body = r.json()
    assert "secret_base32" in body
    assert "secret_token" in body
    return body


def totp_finish(
    client: TestClient,
    headers: dict[str, str],
    secret_token: str,
    code: str,
    *,
    expected_status: int = 200,
) -> dict:
    """Call POST /users/me/mfa/totp/finish and return the response body."""
    r = client.post(
        f"{_BASE}/users/me/mfa/totp/finish",
        headers=headers,
        json={"secret_token": secret_token, "code": code},
    )
    assert r.status_code == expected_status, (
        f"totp/finish expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def enroll_totp(client: TestClient, headers: dict[str, str]) -> dict:
    """Full TOTP enrollment: begin → compute valid code → finish.

    Returns the finish response which may include ``recovery_codes``.
    """
    begin = totp_begin(client, headers)
    secret = begin["secret_base32"]
    token = begin["secret_token"]
    code = pyotp.TOTP(secret).now()
    return totp_finish(client, headers, token, code)


# ── Passkey helpers (mock-based) ───────────────────────────────────────


def _fake_credential_id() -> bytes:
    """Generate a random 32-byte credential ID."""
    return os.urandom(32)


def _fake_public_key() -> bytes:
    """Generate a plausible COSE-encoded public key blob (random bytes)."""
    return os.urandom(77)


def _cred_id_b64url(cred_id: bytes) -> str:
    """Encode bytes as base64url without padding."""
    return base64.urlsafe_b64encode(cred_id).rstrip(b"=").decode()


def _make_verified_registration(cred_id: bytes, public_key: bytes, sign_count: int = 0) -> MagicMock:
    """Build a fake VerifiedRegistration compatible with MfaService's attribute reads."""
    vr = MagicMock()
    vr.credential_id = cred_id
    vr.credential_public_key = public_key
    vr.sign_count = sign_count
    vr.aaguid = None
    return vr


def _make_verified_authentication(new_sign_count: int = 1) -> MagicMock:
    """Build a fake VerifiedAuthentication compatible with MfaService's attribute reads."""
    va = MagicMock()
    va.new_sign_count = new_sign_count
    return va


def passkey_begin_registration(client: TestClient, headers: dict[str, str]) -> dict:
    """Call POST /users/me/mfa/passkeys/begin and return options."""
    r = client.post(f"{_BASE}/users/me/mfa/passkeys/begin", headers=headers)
    assert r.status_code == 200, f"passkeys/begin failed: {r.text}"
    return r.json()


def passkey_finish_registration(
    client: TestClient,
    headers: dict[str, str],
    challenge_token: str,
    nickname: str = "Test Key",
    *,
    cred_id: bytes | None = None,
    public_key: bytes | None = None,
    expected_status: int = 200,
) -> dict:
    """Call POST /users/me/mfa/passkeys/finish with mocked verification.

    Patches ``webauthn.verify_registration_response`` in the service layer
    so no real authenticator is needed.
    """
    cred_id = cred_id or _fake_credential_id()
    public_key = public_key or _fake_public_key()
    fake_vr = _make_verified_registration(cred_id, public_key)
    cred_id_b64 = _cred_id_b64url(cred_id)

    with patch(
        "app.services.users.mfa_service.verify_registration_response",
        return_value=fake_vr,
    ):
        r = client.post(
            f"{_BASE}/users/me/mfa/passkeys/finish",
            headers=headers,
            json={
                "challenge_token": challenge_token,
                "credential": {
                    "id": cred_id_b64,
                    "rawId": cred_id_b64,
                    "type": "public-key",
                    "response": {
                        "attestationObject": "dummyAttestation",
                        "clientDataJSON": "dummyClientData",
                        "transports": ["internal"],
                    },
                    "authenticatorAttachment": "platform",
                },
                "nickname": nickname,
            },
        )
    assert r.status_code == expected_status, (
        f"passkeys/finish expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def enroll_passkey(
    client: TestClient,
    headers: dict[str, str],
    nickname: str = "Test Key",
    *,
    cred_id: bytes | None = None,
    public_key: bytes | None = None,
) -> tuple[dict, bytes, bytes]:
    """Full passkey enrollment: begin → finish (mocked).

    Returns ``(finish_response, cred_id, public_key)`` so callers can
    reuse the credential for assertion mocking.
    """
    cred_id = cred_id or _fake_credential_id()
    public_key = public_key or _fake_public_key()
    begin = passkey_begin_registration(client, headers)
    challenge_token = begin["challenge_token"]
    resp = passkey_finish_registration(
        client,
        headers,
        challenge_token,
        nickname,
        cred_id=cred_id,
        public_key=public_key,
    )
    return resp, cred_id, public_key


# ── Challenge / login helpers ──────────────────────────────────────────


def login_get_challenge(client: TestClient, email: str, password: str) -> dict:
    """Login with a 2FA-enabled account and return the MFA challenge body."""
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.text}"
    body = r.json()
    assert body.get("kind") == "mfa_challenge", f"Expected mfa_challenge, got: {body}"
    return body


def verify_challenge_totp(
    client: TestClient,
    challenge_token: str,
    totp_secret: str,
    *,
    expected_status: int = 200,
) -> dict:
    """Verify a login MFA challenge using a TOTP code."""
    code = pyotp.TOTP(totp_secret).now()
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge_token,
            "method": "totp",
            "payload": {"code": code},
        },
    )
    assert r.status_code == expected_status, (
        f"mfa/verify expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def verify_challenge_recovery(
    client: TestClient,
    challenge_token: str,
    recovery_code: str,
    *,
    expected_status: int = 200,
) -> dict:
    """Verify a login MFA challenge using a recovery code."""
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge_token,
            "method": "recovery",
            "payload": {"code": recovery_code},
        },
    )
    assert r.status_code == expected_status, (
        f"mfa/verify (recovery) expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def headers_from_token(token_body: dict) -> dict[str, str]:
    """Build auth headers from a ``LoginToken`` body."""
    assert token_body.get("kind") == "token", f"Not a token response: {token_body}"
    return {"Authorization": f"Bearer {token_body['access_token']}"}


# ── Security event helpers ─────────────────────────────────────────────


def list_security_events(
    client: TestClient,
    headers: dict[str, str],
    *,
    event_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Fetch security events visible to the current user."""
    params: dict = {"limit": limit}
    if event_type:
        params["event_type"] = event_type
    r = client.get(f"{_BASE}/security-events/", headers=headers, params=params)
    assert r.status_code == 200, f"security-events list failed: {r.text}"
    data = r.json()
    # NOTE: must be `is not None`, not a truthy `or` — an empty-but-present
    # "data" list (no events yet) is falsy and would otherwise fall through to
    # returning the whole response dict instead of `[]`.
    return data["data"] if "data" in data else data


def assert_security_event_written(
    client: TestClient,
    headers: dict[str, str],
    event_type: str,
    *,
    limit: int = 50,
) -> None:
    """Assert that at least one ``SecurityEvent`` row with ``event_type`` exists."""
    events = list_security_events(client, headers, limit=limit)
    matching = [e for e in events if e.get("event_type") == event_type]
    assert matching, (
        f"Expected at least one '{event_type}' security event but found none. "
        f"All events: {[e.get('event_type') for e in events]}"
    )


def find_security_events(
    client: TestClient,
    headers: dict[str, str],
    event_type: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return all ``SecurityEvent`` rows matching ``event_type``."""
    events = list_security_events(client, headers, limit=limit)
    return [e for e in events if e.get("event_type") == event_type]


# ── Trusted-device helpers ──────────────────────────────────────────────


def verify_challenge_totp_remember(
    client: TestClient,
    challenge_token: str,
    totp_secret: str,
    remember_device_days: int | None,
    *,
    expected_status: int = 200,
) -> dict:
    """Verify an MFA challenge via TOTP with an optional trusted-device duration.

    Passes ``remember_device_days`` in the request body.  When set to a
    valid value (1/7/30), the returned ``LoginToken`` carries a non-null
    ``trusted_device_token``.  When ``None`` (default), the field is absent
    or null.

    Uses ``counter_offset=+1`` to generate a code from the adjacent TOTP
    step.  This avoids the ``last_used_step`` replay guard when this helper
    is called in the same 30-second window as ``enroll_totp_and_get_secret``
    (which records the current step during enrollment).  Using the +1 step
    is accepted by the service (``valid_window=1``) and mirrors the pattern
    used by ``test_totp_verify_accepts_adjacent_steps``.
    """
    import time as _time
    ts = int(_time.time())
    code = pyotp.TOTP(totp_secret).at(ts, counter_offset=1)
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge_token,
            "method": "totp",
            "payload": {"code": code},
            "remember_device_days": remember_device_days,
        },
    )
    assert r.status_code == expected_status, (
        f"mfa/verify (remember) expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def verify_challenge_recovery_remember(
    client: TestClient,
    challenge_token: str,
    recovery_code: str,
    remember_device_days: int | None,
    *,
    expected_status: int = 200,
) -> dict:
    """Verify an MFA challenge via recovery code with an optional trusted-device duration."""
    r = client.post(
        f"{_BASE}/login/mfa/verify",
        json={
            "challenge_token": challenge_token,
            "method": "recovery",
            "payload": {"code": recovery_code},
            "remember_device_days": remember_device_days,
        },
    )
    assert r.status_code == expected_status, (
        f"mfa/verify (recovery remember) expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def login_with_trusted_device(
    client: TestClient,
    email: str,
    password: str,
    token: str,
) -> dict:
    """POST /login/access-token with the ``X-Trusted-Device`` header set.

    Returns the raw response body.  Does NOT assert on the kind so the
    caller can branch on ``kind=="token"`` (skip) vs ``kind=="mfa_challenge"``
    (no skip) vs check error responses.
    """
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": email, "password": password},
        headers={"X-Trusted-Device": token},
    )
    assert r.status_code == 200, f"login_with_trusted_device: {r.status_code} {r.text}"
    return r.json()


def enroll_totp_and_get_secret(client: TestClient, headers: dict[str, str]) -> str:
    """Enroll TOTP and return the TOTP secret (``secret_base32``).

    Needed for tests that must call ``pyotp.TOTP(secret).now()`` after enrollment
    (e.g. to obtain a fresh challenge verify code) without going through the
    begin/finish ceremony a second time.
    """
    begin = totp_begin(client, headers)
    secret = begin["secret_base32"]
    code = pyotp.TOTP(secret).now()
    totp_finish(client, headers, begin["secret_token"], code)
    return secret
