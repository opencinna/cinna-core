"""Session revocation by session version (C3) and the 401/403 split.

Every non-desktop user JWT carries ``sv``; password change, first
set-password, forgot-password reset and the admin password set bump the
user's session version, revoke web sessions, native (desktop/mobile) clients
and trusted devices. A password change or set keeps only the caller's own
session: its web session survives (its *access* token goes stale and one
refresh recovers it) and, when made with a desktop token, that desktop client.

401 means "refresh and retry" (expired, stale ``sv``, legacy token without
``sv``) and carries ``WWW-Authenticate: Bearer``; a forged or aud-bearing
token stays 403.

API-token effects (CLI / A2A / MCP) live in
``tests/api/cli/session_revocation_api_tokens_test.py`` (they need the agent
stubs of that directory's conftest).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.desktop_auth import generate_pkce_pair, obtain_desktop_tokens
from tests.utils.google_oauth import google_oauth_patched, random_google_id
from tests.utils.platform_token import mint_platform_token
from tests.utils.security_event import events_of_type
from tests.utils.user import create_random_user
from tests.utils.utils import random_email, random_lower_string
from tests.utils.web_session import (
    OPT_IN_HEADERS,
    admin_set_password,
    bearer,
    bearer_login,
    change_password,
    claims_of,
    me_status,
    post_refresh,
    refresh_session,
    reset_password,
    session_from_login,
    set_own_password,
    web_login,
)

API = settings.API_V1_STR
DESKTOP = f"{API}/desktop-auth"
APP = f"{API}/app-auth"
MOBILE_REDIRECT = "cinna-mobile://oauth/callback"


def _revocation_reasons(client: TestClient, headers: dict[str, str]) -> list[dict]:
    rows = events_of_type(client, headers, "SESSIONS_REVOKED")
    out = []
    for row in rows:
        details = row["details"]
        out.append(json.loads(details) if isinstance(details, str) else details)
    return out


def _native_refresh(client: TestClient, base: str, client_id: str, refresh: str):
    return client.post(
        f"{base}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
        },
    )


def _mobile_tokens(client: TestClient, headers: dict[str, str]) -> dict:
    """``/app-auth`` authorize → consent → token (the mobile mirror)."""
    verifier, challenge = generate_pkce_pair()
    r = client.get(
        f"{APP}/authorize",
        params={
            "redirect_uri": MOBILE_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
            "device_name": "Phone",
            "platform": "ios",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    nonce = parse_qs(urlparse(r.headers["location"]).query)["request"][0]
    r = client.post(
        f"{APP}/consent",
        headers=headers,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 200, r.text
    params = parse_qs(urlparse(r.json()["redirect_to"]).query)
    r = client.post(
        f"{APP}/token",
        json={
            "grant_type": "authorization_code",
            "client_id": params["client_id"][0],
            "code": params["code"][0],
            "redirect_uri": MOBILE_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── C3: a password change ends every other session ───────────────────────


def test_c3_password_change_ends_other_sessions(client: TestClient) -> None:
    """
      1. Cookie session A, cookie session C and bearer-only login B.
      2. Change the password with A's token.
      3. B → 401 (+ WWW-Authenticate); C cannot refresh.
      4. A's own access token went stale too, but A's refresh → 200 and the new
         token works (the caller's session is kept).
      5. Old password refused, new one accepted; a SESSIONS_REVOKED event with
         reason ``password_changed`` and no API-token revocation.
    """
    user = create_random_user(client)
    old_pw, new_pw = user["_password"], random_lower_string()

    # ── Phase 1: three sessions ────────────────────────────────────────
    a = web_login(client, user["email"], old_pw)
    c = web_login(client, user["email"], old_pw)
    b = bearer_login(client, user["email"], old_pw)
    assert me_status(client, b) == 200

    # ── Phase 2: change with A ─────────────────────────────────────────
    change_password(client, a.headers, old_pw, new_pw)

    # ── Phase 3: B and C are dead ──────────────────────────────────────
    r = client.get(f"{API}/users/me", headers=b)
    assert r.status_code == 401, r.text
    assert r.headers.get("www-authenticate") == "Bearer"
    r = post_refresh(client, c.refresh, c.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}

    # ── Phase 4: A recovers with one refresh ───────────────────────────
    assert me_status(client, a.headers) == 401
    a = refresh_session(client, a)
    assert claims_of(a.access_token)["sv"] == 2
    assert me_status(client, a.headers) == 200

    # ── Phase 5: credentials and audit ─────────────────────────────────
    r = client.post(
        f"{API}/login/access-token",
        data={"username": user["email"], "password": old_pw},
    )
    assert r.status_code == 400, r.text
    fresh = bearer_login(client, user["email"], new_pw)
    assert {"reason": "password_changed", "api_tokens": False} in _revocation_reasons(
        client, fresh
    )


# ── Reset and admin set end everything ───────────────────────────────────


def test_reset_and_admin_password_set_end_every_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. User 1 holds a cookie session and a bearer token; forgot-password
         reset → both are dead (the cookie session cannot refresh).
      2. User 2 likewise; admin ``PATCH /users/{id}`` password → both dead.
      3. Each feed records SESSIONS_REVOKED with the right reason; only the
         reset revokes API tokens.
    """
    # ── Phase 1: reset ─────────────────────────────────────────────────
    u1 = create_random_user(client)
    ws1 = web_login(client, u1["email"], u1["_password"])
    b1 = bearer_login(client, u1["email"], u1["_password"])
    new1 = random_lower_string()
    reset_password(client, u1["email"], new1)
    assert me_status(client, b1) == 401
    assert me_status(client, ws1.headers) == 401
    r = post_refresh(client, ws1.refresh, ws1.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}
    h1 = bearer_login(client, u1["email"], new1)
    assert {"reason": "password_reset", "api_tokens": True} in _revocation_reasons(
        client, h1
    )

    # ── Phase 2: admin set ─────────────────────────────────────────────
    u2 = create_random_user(client)
    ws2 = web_login(client, u2["email"], u2["_password"])
    b2 = bearer_login(client, u2["email"], u2["_password"])
    new2 = random_lower_string()
    admin_set_password(client, superuser_token_headers, u2["id"], new2)
    assert me_status(client, b2) == 401
    assert me_status(client, ws2.headers) == 401
    r = post_refresh(client, ws2.refresh, ws2.csrf_token)
    assert r.status_code == 401, r.text
    h2 = bearer_login(client, u2["email"], new2)
    assert {
        "reason": "admin_password_set",
        "api_tokens": False,
    } in _revocation_reasons(client, h2)

    # The admin's own session is untouched.
    assert me_status(client, superuser_token_headers) == 200


# ── First set-password keeps the caller's session ────────────────────────


def test_self_set_password_keeps_current_session(client: TestClient) -> None:
    """
      1. A Google-only user signs in twice: a cookie session and a bearer token.
      2. Sets a first password with the cookie session's token.
      3. The bearer token is dead; the cookie session refreshes and works.
      4. The new password logs in; reason ``password_set`` without API tokens.
    """
    email, google_id = random_email(), random_google_id()

    # ── Phase 1: two Google sign-ins ───────────────────────────────────
    with google_oauth_patched(email=email, google_id=google_id):
        r = client.post(
            f"{API}/auth/google/callback",
            json={"code": "c", "state": "s"},
            headers=OPT_IN_HEADERS,
        )
        ws = session_from_login(client, r)
        r = client.post(f"{API}/auth/google/callback", json={"code": "c", "state": "s"})
        assert r.status_code == 200, r.text
        other = bearer(r.json()["access_token"])
    client.cookies.clear()
    assert client.get(f"{API}/users/me", headers=ws.headers).json()["has_password"] is False

    # ── Phase 2: set a first password ──────────────────────────────────
    pw = random_lower_string()
    set_own_password(client, ws.headers, pw)

    # ── Phase 3: other session dead, own session recovers ──────────────
    assert me_status(client, other) == 401
    ws = refresh_session(client, ws)
    assert me_status(client, ws.headers) == 200

    # ── Phase 4: password works; audit ─────────────────────────────────
    fresh = bearer_login(client, email, pw)
    assert {"reason": "password_set", "api_tokens": False} in _revocation_reasons(
        client, fresh
    )


# ── Legacy, stale, expired and forged tokens ─────────────────────────────


def test_legacy_stale_expired_and_forged_tokens(client: TestClient) -> None:
    """
      1. A token with the current ``sv`` works (control).
      2. A legacy token without ``sv`` → 401 + WWW-Authenticate.
      3. A token with a stale ``sv`` → 401.
      4. An expired token → 401 + WWW-Authenticate.
      5. A token signed with another key → 403; an ``aud``-bearing token → 403.
    """
    user = create_random_user(client)
    me = client.get(
        f"{API}/users/me", headers=bearer_login(client, user["email"], user["_password"])
    ).json()
    uid = me["id"]

    def get_me(token: str):
        return client.get(f"{API}/users/me", headers=bearer(token))

    # ── Phase 1: control ───────────────────────────────────────────────
    assert get_me(mint_platform_token(uid)).status_code == 200

    # ── Phase 2: legacy (no sv) ────────────────────────────────────────
    r = get_me(mint_platform_token(uid, extra_claims={"sv": None}))
    assert r.status_code == 401, r.text
    assert r.headers.get("www-authenticate") == "Bearer"

    # ── Phase 3: stale sv ──────────────────────────────────────────────
    r = get_me(mint_platform_token(uid, extra_claims={"sv": 0}))
    assert r.status_code == 401, r.text
    r = get_me(mint_platform_token(uid, extra_claims={"sv": 2}))
    assert r.status_code == 401, r.text

    # ── Phase 4: expired ───────────────────────────────────────────────
    r = get_me(mint_platform_token(uid, expires_delta=timedelta(minutes=-1)))
    assert r.status_code == 401, r.text
    assert r.headers.get("www-authenticate") == "Bearer"

    # ── Phase 5: forged / aud ──────────────────────────────────────────
    forged = jwt.encode(
        {
            "sub": uid,
            "sv": 1,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "not-the-server-secret-" + random_lower_string(),
        algorithm="HS256",
    )
    assert get_me(forged).status_code == 403
    aud = mint_platform_token(uid, extra_claims={"aud": "agent_env"})
    assert get_me(aud).status_code == 403


# ── Native clients: desktop and mobile ───────────────────────────────────


def test_password_change_revokes_other_desktop_clients_but_keeps_the_caller(
    client: TestClient,
) -> None:
    """
      1. Two desktop clients (code exchange) for one user.
      2. Change the password with desktop client A's access token.
      3. B's refresh → 400 ``invalid_grant`` and B's access token → 401.
      4. A keeps working: its access token and its refresh.
    """
    user = create_random_user(client)
    web = bearer_login(client, user["email"], user["_password"])
    dev_a = obtain_desktop_tokens(client, web, device_name="Laptop A")
    dev_b = obtain_desktop_tokens(client, web, device_name="Laptop B")

    # ── Phase 2: change from desktop A ─────────────────────────────────
    change_password(
        client, bearer(dev_a["access_token"]), user["_password"], random_lower_string()
    )

    # ── Phase 3: B revoked ─────────────────────────────────────────────
    r = _native_refresh(client, DESKTOP, dev_b["client_id"], dev_b["refresh_token"])
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": "invalid_grant"}
    assert me_status(client, bearer(dev_b["access_token"])) == 401

    # ── Phase 4: A kept ────────────────────────────────────────────────
    assert me_status(client, bearer(dev_a["access_token"])) == 200
    r = _native_refresh(client, DESKTOP, dev_a["client_id"], dev_a["refresh_token"])
    assert r.status_code == 200, r.text
    assert me_status(client, bearer(r.json()["access_token"])) == 200
    # The web bearer token that set up the clients is gone.
    assert me_status(client, web) == 401


def test_reset_revokes_desktop_and_mobile_clients(client: TestClient) -> None:
    """
      1. A desktop client and a mobile (app-auth) client.
      2. Forgot-password reset.
      3. Both refreshes → 400 ``invalid_grant``; both access tokens → 401.
    """
    user = create_random_user(client)
    web = bearer_login(client, user["email"], user["_password"])
    desktop = obtain_desktop_tokens(client, web, device_name="Desk")
    mobile = _mobile_tokens(client, web)
    assert me_status(client, bearer(mobile["access_token"])) == 200

    reset_password(client, user["email"], random_lower_string())

    for base, tokens in ((DESKTOP, desktop), (APP, mobile)):
        r = _native_refresh(client, base, tokens["client_id"], tokens["refresh_token"])
        assert r.status_code == 400, (base, r.text)
        assert r.json() == {"detail": "invalid_grant"}
        assert me_status(client, bearer(tokens["access_token"])) == 401, base
