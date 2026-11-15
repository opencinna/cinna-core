"""Google auto-link wipe (C1): a Google sign-in that auto-links to an
*unconfirmed* password account clears the pre-registered password, MFA
enrollment, every session, device, trusted device and API token before it
links and confirms. A confirmed account links as before.

Threat: an attacker registers the victim's address with their own password
(open registration, never confirmed). The victim later signs in with Google;
before the fix the attacker's password and sessions kept working.

The existing ``test_google_oauth_auto_confirm.py`` covers MFA enrollment and
the account CLI token; A2A and MCP tokens are covered in
``tests/api/cli/session_revocation_api_tokens_test.py``.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core.config import settings
from app.utils import generate_email_confirmation_token
from tests.utils.google_oauth import google_oauth_patched, random_google_id
from tests.utils.mfa import (
    enroll_totp_and_get_secret,
    login_get_challenge,
    login_with_trusted_device,
    verify_challenge_totp_remember,
)
from tests.utils.security_event import events_of_type
from tests.utils.user import create_random_user
from tests.utils.utils import random_email, random_lower_string
from tests.utils.web_session import (
    bearer,
    bearer_login,
    me_status,
    post_refresh,
    set_own_password,
    web_login,
)

API = settings.API_V1_STR


def _google_sign_in(client: TestClient, email: str) -> dict[str, str]:
    with google_oauth_patched(email=email, google_id=random_google_id()):
        r = client.post(
            f"{API}/auth/google/callback", json={"code": "c", "state": "s"}
        )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "token", r.json()
    client.cookies.clear()
    return bearer(r.json()["access_token"])


def test_c1_unconfirmed_password_account_prehijack(client: TestClient) -> None:
    """
      1. Attacker signs up with the victim's address (unconfirmed) and holds a
         bearer token and a cookie session.
      2. Victim signs in with Google for the same address.
      3. Attacker's password → 400 "Incorrect email or password"; attacker's
         bearer → 401; attacker's cookie session cannot refresh.
      4. Victim's account is confirmed and has no password; the feed records a
         ``google_link_wipe`` revocation including API tokens.
    """
    # ── Phase 1: attacker pre-registers ────────────────────────────────
    email = random_email()
    attacker_pw = random_lower_string()
    r = client.post(
        f"{API}/users/signup", json={"email": email, "password": attacker_pw}
    )
    assert r.status_code == 200, r.text
    assert r.json()["email_confirmed"] is False
    attacker = bearer_login(client, email, attacker_pw)
    attacker_web = web_login(client, email, attacker_pw)
    assert me_status(client, attacker) == 200

    # ── Phase 2: victim signs in with Google ───────────────────────────
    victim = _google_sign_in(client, email)

    # ── Phase 3: attacker locked out ───────────────────────────────────
    r = client.post(
        f"{API}/login/access-token",
        data={"username": email, "password": attacker_pw},
    )
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": "Incorrect email or password"}
    assert me_status(client, attacker) == 401
    r = post_refresh(client, attacker_web.refresh, attacker_web.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}

    # ── Phase 4: victim state ──────────────────────────────────────────
    me = client.get(f"{API}/users/me", headers=victim)
    assert me.status_code == 200, me.text
    assert me.json()["email_confirmed"] is True
    assert me.json()["has_password"] is False
    reasons = [
        json.loads(e["details"]) if isinstance(e["details"], str) else e["details"]
        for e in events_of_type(client, victim, "SESSIONS_REVOKED")
    ]
    assert {"reason": "google_link_wipe", "api_tokens": True} in reasons


def test_confirmed_account_keeps_password_on_google_link(client: TestClient) -> None:
    """
      1. A password user confirms their address.
      2. They sign in with Google (auto-link).
      3. The password still logs in, the pre-link token still works, and no
         sessions were revoked.
    """
    user = create_random_user(client)
    token = generate_email_confirmation_token(email=user["email"])
    r = client.post(f"{API}/confirm-email/", json={"token": token})
    assert r.status_code == 200, r.text
    before = bearer_login(client, user["email"], user["_password"])

    google = _google_sign_in(client, user["email"])

    after = bearer_login(client, user["email"], user["_password"])
    assert me_status(client, before) == 200
    assert me_status(client, after) == 200
    me = client.get(f"{API}/users/me", headers=google).json()
    assert me["has_password"] is True
    assert events_of_type(client, google, "SESSIONS_REVOKED") == []


def test_c1_trusted_device_no_longer_skips_mfa(client: TestClient) -> None:
    """
      1. Attacker pre-registers, enrolls TOTP and gets a trusted-device token.
      2. Victim signs in with Google (wipe-and-link).
      3. Victim sets a password and enrolls TOTP.
      4. A login presenting the attacker's trusted-device token still gets an
         MFA challenge, not a token.
    """
    # ── Phase 1: attacker's trusted device ─────────────────────────────
    email = random_email()
    attacker_pw = random_lower_string()
    r = client.post(
        f"{API}/users/signup", json={"email": email, "password": attacker_pw}
    )
    assert r.status_code == 200, r.text
    attacker = bearer_login(client, email, attacker_pw)
    secret = enroll_totp_and_get_secret(client, attacker)
    challenge = login_get_challenge(client, email, attacker_pw)
    verified = verify_challenge_totp_remember(
        client, challenge["challenge_token"], secret, 30
    )
    trusted = verified["trusted_device_token"]
    assert trusted
    assert login_with_trusted_device(client, email, attacker_pw, trusted)["kind"] == "token"

    # ── Phase 2: victim links Google ───────────────────────────────────
    victim = _google_sign_in(client, email)

    # ── Phase 3: victim sets a password and enrolls TOTP ───────────────
    victim_pw = random_lower_string()
    set_own_password(client, victim, victim_pw)
    victim = bearer_login(client, email, victim_pw)
    enroll_totp_and_get_secret(client, victim)

    # ── Phase 4: the old trusted device no longer skips MFA ────────────
    body = login_with_trusted_device(client, email, victim_pw, trusted)
    assert body["kind"] == "mfa_challenge", body
