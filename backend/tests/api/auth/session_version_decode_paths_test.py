"""A stale session version is refused on every user-JWT decode path, not just
``CurrentUser``.

Covered here (no agent needed):
  * the guest-aware dependency ``CurrentUserOrGuest`` (``GET /sessions/``);
  * the webapp owner resolver (``/agents/{id}/webapp/owner-status?token=``), which
    answers 401 for a stale token before it ever looks the agent up.

The other decode paths each gained a stale-``sv`` case in their own file,
next to the fixtures they need:
  * Socket.IO connect — ``tests/api/events/socket_connection_auth_test.py``;
  * env-console WebSockets — ``tests/api/agent_environments/test_env_console.py``;
  * speech WebSocket — ``tests/api/voice_transcription/test_speech_ws_auth.py``;
  * git smart-HTTP — ``tests/api/catalog/catalog_desktop_git_test.py``.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.platform_token import mint_platform_token
from tests.utils.user import create_random_user
from tests.utils.web_session import bearer, bearer_login, change_password

API = settings.API_V1_STR


def _user_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{API}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_guest_aware_dependency_refuses_stale_session_version(
    client: TestClient,
) -> None:
    """
      1. Current token → ``GET /sessions/`` 200.
      2. Minted stale / legacy tokens → 401.
      3. A real token made stale by a password change → 401.
    """
    user = create_random_user(client)
    headers = bearer_login(client, user["email"], user["_password"])
    uid = _user_id(client, headers)

    assert client.get(f"{API}/sessions/", headers=headers).status_code == 200
    for claims in ({"sv": 2}, {"sv": None}):
        token = mint_platform_token(uid, extra_claims=claims)
        r = client.get(f"{API}/sessions/", headers=bearer(token))
        assert r.status_code == 401, (claims, r.text)

    other = bearer_login(client, user["email"], user["_password"])
    change_password(client, other, user["_password"], "new-" + user["_password"])
    assert client.get(f"{API}/sessions/", headers=headers).status_code == 401


def test_webapp_owner_resolver_refuses_stale_session_version(
    client: TestClient,
) -> None:
    """
      1. Current token on an unknown agent → 404 (the token was accepted).
      2. Stale ``sv`` (query token and bearer) → 401.
    """
    user = create_random_user(client)
    uid = _user_id(client, bearer_login(client, user["email"], user["_password"]))
    url = f"{API}/agents/{uuid.uuid4()}/webapp/owner-status"

    good = mint_platform_token(uid)
    assert client.get(url, params={"token": good}).status_code == 404

    stale = mint_platform_token(uid, extra_claims={"sv": 2})
    r = client.get(url, params={"token": stale})
    assert r.status_code == 401, r.text
    r = client.get(url, headers=bearer(stale))
    assert r.status_code == 401, r.text
