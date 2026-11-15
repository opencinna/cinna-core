"""Browser cookie sessions (C2): short access tokens, the ``cinna_refresh``
cookie, and the ``/login/session/{refresh,logout,revoke-all}`` endpoints.

Contract (plan ``security_hardening_slice1_plan.md`` §2):
  * every user access token lives 15 minutes and carries ``sv`` + ``iat``;
    cookie-session tokens also carry ``sid``;
  * ``X-Cinna-Web-Session: 1`` on a login opts into a cookie session: the body
    gets ``csrf_token`` and the response sets ``cinna_refresh`` (HttpOnly,
    SameSite=Strict, Path=/api/v1/login/session, Max-Age=14 days, host-only,
    not Secure on the local stack); without it the token arm is unchanged and
    ``csrf_token`` is null;
  * refresh needs the cookie, ``X-CSRF-Token`` and (when sent) an allowed
    ``Origin``; it rotates, answers 200 without ``Set-Cookie`` inside the
    reuse-grace window, and treats a replay outside grace as theft.

The cookie jar is never relied on: ``tests/utils/web_session.py`` captures the
raw cookie per login and sends it explicitly.
"""
from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.google_oauth import google_oauth_patched, random_google_id
from tests.utils.invitation import ACCEPT_URL, fresh_invitation_limiter, invite_and_token
from tests.utils.mfa import enroll_totp_and_get_secret, login_get_challenge
from tests.utils.user import create_random_user
from tests.utils.utils import random_email, random_lower_string
from tests.utils.web_session import (
    LOGIN_URL,
    OPT_IN_HEADERS,
    REFRESH_COOKIE,
    bearer,
    bearer_login,
    claims_of,
    me_status,
    post_logout,
    post_refresh,
    refresh_session,
    refresh_set_cookie,
    refresh_value,
    revoke_all,
    session_from_login,
    web_login,
)

API = settings.API_V1_STR
ALLOWED_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example.com"


def _assert_cookie_cleared(response) -> None:
    line = refresh_set_cookie(response)
    assert line is not None, "expected the refresh cookie to be cleared"
    lowered = line.lower()
    assert refresh_value(response) == ""
    assert "max-age=0" in lowered or "expires=thu, 01 jan 1970" in lowered, line
    assert f"path={API}/login/session".lower() in lowered, line


# ── C2: access tokens are short-lived ────────────────────────────────────


def test_c2_access_token_is_short_lived(client: TestClient) -> None:
    """
    A plain password login mints a 15-minute token that carries ``sv``/``iat``.
      1. Bearer-only login → token ``exp - iat`` ≤ 900 s, ``sv`` = 1, no ``sid``.
      2. Opt-in login → same lifetime, plus a ``sid``.
      3. Refresh → ``expires_in`` 900 and a 15-minute token with the same ``sid``.
    """
    user = create_random_user(client)

    # ── Phase 1: bearer-only login ─────────────────────────────────────
    headers = bearer_login(client, user["email"], user["_password"])
    claims = claims_of(headers["Authorization"].split(" ", 1)[1])
    assert "iat" in claims, claims
    assert claims["exp"] - claims["iat"] <= 900, claims
    assert claims["exp"] - claims["iat"] >= 890, claims
    assert claims["sv"] == 1
    assert "sid" not in claims

    # ── Phase 2: opt-in login carries sid ──────────────────────────────
    ws = web_login(client, user["email"], user["_password"])
    web_claims = claims_of(ws.access_token)
    assert web_claims["exp"] - web_claims["iat"] <= 900
    assert web_claims["sv"] == 1
    assert web_claims["sid"]

    # ── Phase 3: refreshed token keeps the sid, 15 minutes ─────────────
    r = post_refresh(client, ws.refresh, ws.csrf_token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    refreshed = claims_of(body["access_token"])
    assert refreshed["sid"] == web_claims["sid"]
    assert refreshed["exp"] - refreshed["iat"] <= 900
    assert me_status(client, bearer(body["access_token"])) == 200


# ── Opt-in vs bearer-only login shape ────────────────────────────────────


def test_opt_in_login_sets_refresh_cookie_and_plain_login_does_not(
    client: TestClient,
) -> None:
    """
      1. Opt-in login → ``cinna_refresh`` with the contract attributes and a
         ``csrf_token`` in the body; the access token works.
      2. Plain login → no refresh cookie, ``csrf_token`` null, and the token arm
         keeps its old shape (``kind``/``access_token``/``token_type``).
    """
    user = create_random_user(client)

    # ── Phase 1: opt-in login ──────────────────────────────────────────
    client.cookies.clear()
    r = client.post(
        LOGIN_URL,
        data={"username": user["email"], "password": user["_password"]},
        headers=OPT_IN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "token"
    assert body["token_type"] == "bearer"
    assert isinstance(body["csrf_token"], str) and len(body["csrf_token"]) >= 32
    line = refresh_set_cookie(r)
    assert line is not None, r.headers.get_list("set-cookie")
    attrs = [part.strip().lower() for part in line.split(";")]
    assert len(refresh_value(r)) >= 43  # opaque, high-entropy
    assert "httponly" in attrs
    assert "samesite=strict" in attrs
    assert "max-age=1209600" in attrs
    assert f"path={API}/login/session".lower() in attrs
    assert not any(a.startswith("domain=") for a in attrs), "cookie must be host-only"
    # ENVIRONMENT=local in the test container → no Secure flag.
    assert "secure" not in attrs
    client.cookies.clear()
    assert me_status(client, bearer(body["access_token"])) == 200

    # ── Phase 2: plain (script) login ──────────────────────────────────
    r = client.post(
        LOGIN_URL, data={"username": user["email"], "password": user["_password"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "token"
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["csrf_token"] is None
    assert refresh_set_cookie(r) is None
    assert me_status(client, bearer(body["access_token"])) == 200


# ── Refresh: rotation, CSRF, Origin, grace, replay ───────────────────────


def test_refresh_rotation_csrf_origin_grace_and_replay(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
      1. Missing cookie / unknown cookie → 401 ``session_expired``.
      2. Missing CSRF, wrong CSRF, foreign Origin → 403 ``csrf_failed``; none of
         them consumes the refresh token.
      3. Allowed Origin + CSRF → 200, rotated cookie.
      4. Replaying the superseded cookie inside the grace window → 200 with no
         ``Set-Cookie``.
      5. Grace window closed → replay → 401 + cookie cleared, and the session is
         dead: even the current cookie is refused.
    """
    user = create_random_user(client)
    ws = web_login(client, user["email"], user["_password"])

    # ── Phase 1: no / unknown cookie ───────────────────────────────────
    r = post_refresh(client, None, ws.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}
    r = post_refresh(client, "x" * 64, ws.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}
    _assert_cookie_cleared(r)

    # ── Phase 2: CSRF and Origin failures ──────────────────────────────
    r = post_refresh(client, ws.refresh, None)
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "csrf_failed"}
    r = post_refresh(client, ws.refresh, "not-the-csrf-token")
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "csrf_failed"}
    r = post_refresh(client, ws.refresh, ws.csrf_token, origin=FOREIGN_ORIGIN)
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "csrf_failed"}

    # ── Phase 3: a good refresh rotates ────────────────────────────────
    r = post_refresh(client, ws.refresh, ws.csrf_token, origin=ALLOWED_ORIGIN)
    assert r.status_code == 200, r.text
    rotated = refresh_value(r)
    assert rotated and rotated != ws.refresh
    assert me_status(client, bearer(r.json()["access_token"])) == 200

    # ── Phase 4: replay inside grace → 200, no cookie written ──────────
    r = post_refresh(client, ws.refresh, ws.csrf_token)
    assert r.status_code == 200, r.text
    assert "set-cookie" not in r.headers, r.headers.get_list("set-cookie")
    assert me_status(client, bearer(r.json()["access_token"])) == 200

    # ── Phase 5: replay outside grace → session revoked ────────────────
    monkeypatch.setattr(settings, "WEB_REFRESH_TOKEN_REUSE_GRACE_SECONDS", 0)
    r = post_refresh(client, ws.refresh, ws.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}
    _assert_cookie_cleared(r)
    r = post_refresh(client, rotated, ws.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}


# ── Logout and sign out everywhere ───────────────────────────────────────


def test_logout_and_sign_out_everywhere(client: TestClient) -> None:
    """
      1. Two cookie sessions A and B for one user, plus a bearer-only token.
      2. Logout A with a wrong CSRF → 403; with a foreign Origin → 403.
      3. Logout A properly → 204 and the cookie is cleared; A cannot refresh.
      4. Logout is idempotent: A's dead cookie again → 204; no cookie → 204.
      5. B is unaffected by A's logout.
      6. Revoke-all with B's token → 204, cookie cleared; B cannot refresh,
         B's and the bearer token are dead; a fresh login works.
    """
    user = create_random_user(client)
    a = web_login(client, user["email"], user["_password"])
    b = web_login(client, user["email"], user["_password"])
    plain = bearer_login(client, user["email"], user["_password"])
    assert claims_of(a.access_token)["sid"] != claims_of(b.access_token)["sid"]

    # ── Phase 2: CSRF / Origin guard on logout ─────────────────────────
    r = post_logout(client, a.refresh, "wrong")
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "csrf_failed"}
    r = post_logout(client, a.refresh, a.csrf_token, origin=FOREIGN_ORIGIN)
    assert r.status_code == 403, r.text
    a = refresh_session(client, a)  # still alive after the refused logouts

    # ── Phase 3: logout A ──────────────────────────────────────────────
    r = post_logout(client, a.refresh, a.csrf_token, origin=ALLOWED_ORIGIN)
    assert r.status_code == 204, r.text
    _assert_cookie_cleared(r)
    r = post_refresh(client, a.refresh, a.csrf_token)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "session_expired"}

    # ── Phase 4: idempotent ────────────────────────────────────────────
    assert post_logout(client, a.refresh, a.csrf_token).status_code == 204
    assert post_logout(client, a.refresh, None).status_code == 204
    assert post_logout(client, None, None).status_code == 204

    # ── Phase 5: B still refreshes ─────────────────────────────────────
    b = refresh_session(client, b)
    assert me_status(client, b.headers) == 200

    # ── Phase 6: sign out everywhere ───────────────────────────────────
    r = revoke_all(client, b.headers)
    _assert_cookie_cleared(r)
    r = post_refresh(client, b.refresh, b.csrf_token)
    assert r.status_code == 401, r.text
    assert me_status(client, b.headers) == 401
    assert me_status(client, plain) == 401
    assert client.post(f"{API}/login/session/revoke-all").status_code == 401
    fresh = bearer_login(client, user["email"], user["_password"])
    assert me_status(client, fresh) == 200


# ── Every web issuer can open a cookie session ───────────────────────────


def test_mfa_verify_google_callback_and_invitation_accept_create_sessions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
      1. MFA verify with the opt-in header → cookie session (refresh works).
      2. Google callback with the opt-in header → cookie session.
      3. Invitation accept with the opt-in header → cookie session.
      4. The same issuers without the header stay bearer-only (no cookie).
    """
    # ── Phase 1: MFA verify ────────────────────────────────────────────
    user = create_random_user(client)
    headers = bearer_login(client, user["email"], user["_password"])
    secret = enroll_totp_and_get_secret(client, headers)
    challenge = login_get_challenge(client, user["email"], user["_password"])
    import time as _time

    code = pyotp.TOTP(secret).at(int(_time.time()), counter_offset=1)
    r = client.post(
        f"{API}/login/mfa/verify",
        json={
            "challenge_token": challenge["challenge_token"],
            "method": "totp",
            "payload": {"code": code},
        },
        headers=OPT_IN_HEADERS,
    )
    ws = session_from_login(client, r)
    assert claims_of(ws.access_token)["sid"]
    ws = refresh_session(client, ws)
    assert me_status(client, ws.headers) == 200

    # ── Phase 2: Google callback ───────────────────────────────────────
    email = random_email()
    google_id = random_google_id()
    with google_oauth_patched(email=email, google_id=google_id):
        r = client.post(
            f"{API}/auth/google/callback",
            json={"code": "fake_code", "state": "fake_state"},
            headers=OPT_IN_HEADERS,
        )
    ws = session_from_login(client, r)
    ws = refresh_session(client, ws)
    assert me_status(client, ws.headers) == 200
    with google_oauth_patched(email=email, google_id=google_id):
        r = client.post(
            f"{API}/auth/google/callback",
            json={"code": "fake_code", "state": "fake_state"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["csrf_token"] is None
    assert refresh_set_cookie(r) is None
    client.cookies.clear()

    # ── Phase 3: invitation accept ─────────────────────────────────────
    fresh_invitation_limiter(monkeypatch)
    invited = random_email()
    _body, token = invite_and_token(client, superuser_token_headers, email=invited)
    r = client.post(
        ACCEPT_URL,
        json={"token": token, "password": random_lower_string()},
        headers=OPT_IN_HEADERS,
    )
    ws = session_from_login(client, r)
    ws = refresh_session(client, ws)
    me = client.get(f"{API}/users/me", headers=ws.headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == invited
    assert REFRESH_COOKIE not in client.cookies
