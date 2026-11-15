"""Helpers for browser cookie sessions (``/login/session/*``) and session-version
revocation tests.

A web login opts into a cookie session with ``X-Cinna-Web-Session: 1``; the
response carries ``csrf_token`` in the body and the ``cinna_refresh`` cookie
(HttpOnly, ``Path=/api/v1/login/session``). Tests often hold several sessions
of the same user at once, so these helpers never rely on the TestClient cookie
jar: every login captures the raw cookie value from ``Set-Cookie`` and clears
the jar, and every refresh/logout sends the cookie it is given explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR
SESSION_BASE = f"{API}/login/session"
REFRESH_URL = f"{SESSION_BASE}/refresh"
LOGOUT_URL = f"{SESSION_BASE}/logout"
REVOKE_ALL_URL = f"{SESSION_BASE}/revoke-all"
LOGIN_URL = f"{API}/login/access-token"

REFRESH_COOKIE = "cinna_refresh"
OPT_IN_HEADERS = {"X-Cinna-Web-Session": "1"}
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class WebSession:
    """One browser session: its access token, CSRF token and refresh cookie."""

    access_token: str
    csrf_token: str
    refresh: str

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.access_token)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def claims_of(token: str) -> dict[str, Any]:
    """A JWT's claims, unverified — reading what the server minted."""
    return jwt.decode(token, options={"verify_signature": False})


def set_cookie_headers(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def refresh_set_cookie(response: httpx.Response) -> str | None:
    """The raw ``Set-Cookie`` line for ``cinna_refresh``, if the response has one."""
    for line in set_cookie_headers(response):
        if line.startswith(f"{REFRESH_COOKIE}="):
            return line
    return None


def refresh_value(response: httpx.Response) -> str | None:
    """The ``cinna_refresh`` value a response sets (``""`` when it clears it)."""
    line = refresh_set_cookie(response)
    if line is None:
        return None
    return line.split(";", 1)[0].split("=", 1)[1].strip('"')


def session_from_login(client: TestClient, response: httpx.Response) -> WebSession:
    """Turn a 200 opt-in login response (any issuer) into a :class:`WebSession`."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("kind", "token") == "token", body
    assert body["csrf_token"], f"opt-in login returned no csrf_token: {body}"
    refresh = refresh_value(response)
    assert refresh, f"opt-in login set no {REFRESH_COOKIE} cookie"
    client.cookies.clear()
    return WebSession(
        access_token=body["access_token"],
        csrf_token=body["csrf_token"],
        refresh=refresh,
    )


def web_login(client: TestClient, email: str, password: str) -> WebSession:
    """Password login that opts into a cookie session."""
    r = client.post(
        LOGIN_URL,
        data={"username": email, "password": password},
        headers=OPT_IN_HEADERS,
    )
    return session_from_login(client, r)


def bearer_login(client: TestClient, email: str, password: str) -> dict[str, str]:
    """Plain (script-style) password login; returns bearer headers."""
    r = client.post(LOGIN_URL, data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "token", r.json()
    return bearer(r.json()["access_token"])


def _session_call(
    client: TestClient,
    url: str,
    *,
    refresh: str | None,
    csrf: str | None,
    origin: str | None = None,
) -> httpx.Response:
    client.cookies.clear()
    headers: dict[str, str] = {}
    if refresh is not None:
        headers["Cookie"] = f"{REFRESH_COOKIE}={refresh}"
    if csrf is not None:
        headers[CSRF_HEADER] = csrf
    if origin is not None:
        headers["Origin"] = origin
    r = client.post(url, headers=headers)
    client.cookies.clear()
    return r


def post_refresh(
    client: TestClient,
    refresh: str | None,
    csrf: str | None,
    *,
    origin: str | None = None,
) -> httpx.Response:
    """Raw ``POST /login/session/refresh`` with an explicit cookie and CSRF."""
    return _session_call(client, REFRESH_URL, refresh=refresh, csrf=csrf, origin=origin)


def post_logout(
    client: TestClient,
    refresh: str | None,
    csrf: str | None,
    *,
    origin: str | None = None,
) -> httpx.Response:
    """Raw ``POST /login/session/logout``."""
    return _session_call(client, LOGOUT_URL, refresh=refresh, csrf=csrf, origin=origin)


def refresh_session(client: TestClient, ws: WebSession) -> WebSession:
    """Refresh and return the session with the new access token (and cookie,
    if rotated). Asserts 200."""
    r = post_refresh(client, ws.refresh, ws.csrf_token)
    assert r.status_code == 200, r.text
    new_refresh = refresh_value(r) or ws.refresh
    return WebSession(
        access_token=r.json()["access_token"],
        csrf_token=ws.csrf_token,
        refresh=new_refresh,
    )


def revoke_all(client: TestClient, headers: dict[str, str]) -> httpx.Response:
    """``POST /login/session/revoke-all`` ("Sign out everywhere"); asserts 204."""
    client.cookies.clear()
    r = client.post(REVOKE_ALL_URL, headers=headers)
    assert r.status_code == 204, r.text
    return r


def change_password(
    client: TestClient,
    headers: dict[str, str],
    current_password: str,
    new_password: str,
) -> None:
    r = client.patch(
        f"{API}/users/me/password",
        headers=headers,
        json={"current_password": current_password, "new_password": new_password},
    )
    assert r.status_code == 200, r.text


def set_own_password(
    client: TestClient, headers: dict[str, str], new_password: str
) -> None:
    r = client.post(
        f"{API}/users/me/set-password",
        headers=headers,
        json={"new_password": new_password},
    )
    assert r.status_code == 200, r.text


def admin_set_password(
    client: TestClient,
    superuser_headers: dict[str, str],
    user_id: str,
    new_password: str,
) -> None:
    r = client.patch(
        f"{API}/users/{user_id}",
        headers=superuser_headers,
        json={"password": new_password},
    )
    assert r.status_code == 200, r.text


def reset_password(client: TestClient, email: str, new_password: str) -> None:
    """Complete a forgot-password reset with a server-format reset token."""
    from app.utils import generate_password_reset_token

    token = generate_password_reset_token(email=email)
    r = client.post(
        f"{API}/reset-password/",
        json={"token": token, "new_password": new_password},
    )
    assert r.status_code == 200, r.text


def me_status(client: TestClient, headers: dict[str, str]) -> int:
    client.cookies.clear()
    return client.get(f"{API}/users/me", headers=headers).status_code
