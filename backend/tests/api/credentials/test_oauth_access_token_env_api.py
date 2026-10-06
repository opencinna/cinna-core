"""``POST /agent/credentials/{id}/access-token`` — on-demand OAuth token for an env (Phase 2).

Plan: docs/plans/oauth_credential_refresh_plan.md §2, §6.3.

Auth is a real scoped env token minted with ``create_env_with_token`` (the real
``AgentEnvContextDep`` path). Google is never contacted: the exchange seam is an
``AsyncMock``. The credentials conftest's refresh no-op fixture is overridden and
Google is forced OFF by default (the container has real client credentials) —
tests opt in with ``google_enabled()``.
"""
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.credentials.oauth_credentials_service import OAuthRefreshError
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    link_credential_to_agent,
    real_credentials_json,
    set_credential_sharing,
    share_credential_via_api,
)
from tests.utils.user import create_random_user_with_headers

EXCHANGE = (
    "app.services.credentials.oauth_credentials_service."
    "OAuthCredentialsService._exchange_refresh_token"
)
OLD_ACCESS = "ya29.old-access-secret"
NEW_ACCESS = "ya29.new-access-secret"
REFRESH_TOKEN = "1//refresh-secret-value"
_BASE = settings.API_V1_STR


@pytest.fixture(autouse=True)
def patch_external_services():
    """Override the conftest fixture: keep the REAL credential refresh, Google OFF."""
    with (
        patch("app.services.events.event_service.socketio_connector", StubSocketIOConnector()),
        patch.object(settings, "GOOGLE_CLIENT_ID", None),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", None),
    ):
        yield


@contextmanager
def google_enabled():
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "x"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "x"),
    ):
        yield


@pytest.fixture
def exchange():
    mock = AsyncMock(return_value={"access_token": NEW_ACCESS, "expires_in": 3600})
    with patch(EXCHANGE, new=mock), google_enabled():
        yield mock


def _oauth_credential(client, headers, *, expires_in: int, type_: str = "gmail_oauth") -> dict:
    return create_random_credential(
        client,
        headers,
        credential_type=type_,
        credential_data={
            "access_token": OLD_ACCESS,
            "refresh_token": REFRESH_TOKEN,
            "token_type": "Bearer",
            "expires_at": int(time.time()) + expires_in,
            "scope": "https://www.googleapis.com/auth/gmail.modify",
        },
    )


class Ctx:
    def __init__(self, agent, adapter, env_headers, user_headers):
        self.agent = agent
        self.adapter = adapter
        self.env_headers = env_headers
        self.user_headers = user_headers


@pytest.fixture
def ctx(client: TestClient, superuser_token_headers, patch_environment_adapter, db: Session) -> Ctx:
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: adapter
    me = client.get(f"{_BASE}/users/me", headers=superuser_token_headers).json()
    _env, env_headers = create_env_with_token(db, agent["id"], me["id"])
    return Ctx(agent, adapter, env_headers, superuser_token_headers)


def _call(client, c: Ctx, credential_id, min_ttl=None, headers=None, known_expires_at=None):
    body = {} if min_ttl is None else {"min_ttl": min_ttl}
    if known_expires_at is not None:
        body["known_expires_at"] = known_expires_at
    return client.post(
        f"{_BASE}/agent/credentials/{credential_id}/access-token",
        headers=headers or c.env_headers,
        json=body,
    )


def _link(client, c: Ctx, credential_id) -> None:
    link_credential_to_agent(client, c.user_headers, c.agent["id"], credential_id)


def _pushed(c: Ctx, credential_id) -> dict | None:
    for e in real_credentials_json(c.adapter.credentials_set):
        if e["id"] == credential_id:
            return e["credential_data"]
    return None


def test_valid_token_returned_without_exchange(client, ctx, exchange):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=10_000)
    _link(client, ctx, cred["id"])
    r = _call(client, ctx, cred["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == OLD_ACCESS
    assert body["refreshed"] is False
    assert body["token_type"] == "Bearer"
    assert body["expires_at"] > time.time() + 5000
    exchange.assert_not_called()
    assert "refresh_token" not in r.text and REFRESH_TOKEN not in r.text


def test_near_expiry_refreshes_and_pushes_before_responding(client, ctx, exchange):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=60)
    with patch.object(settings, "GOOGLE_CLIENT_ID", None):  # link without refreshing
        _link(client, ctx, cred["id"])
    assert _pushed(ctx, cred["id"])["access_token"] == OLD_ACCESS

    r = _call(client, ctx, cred["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == NEW_ACCESS
    assert body["refreshed"] is True
    assert body["expires_at"] > time.time() + 1800
    exchange.assert_awaited_once_with(REFRESH_TOKEN)
    # the env push happened before the response returned
    pushed = _pushed(ctx, cred["id"])
    assert pushed["access_token"] == NEW_ACCESS
    assert "refresh_token" not in pushed
    assert "refresh_token" not in r.text and REFRESH_TOKEN not in r.text

    again = _call(client, ctx, cred["id"])
    assert again.json()["refreshed"] is False
    assert exchange.await_count == 1


def test_unlinked_foreign_and_random_credentials_are_404(client, ctx, exchange):
    own_unlinked = _oauth_credential(client, ctx.user_headers, expires_in=60)
    _other, other_headers = create_random_user_with_headers(client)
    foreign = _oauth_credential(client, other_headers, expires_in=60)
    import uuid

    for cid in (own_unlinked["id"], foreign["id"], str(uuid.uuid4())):
        r = _call(client, ctx, cid)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "credential_not_linked"
    exchange.assert_not_called()


def test_shared_credential_linked_to_agent_is_served(client, ctx, exchange):
    owner, owner_headers = create_random_user_with_headers(client)
    cred = _oauth_credential(client, owner_headers, expires_in=10_000)
    set_credential_sharing(client, owner_headers, cred["id"], True)
    me = client.get(f"{_BASE}/users/me", headers=ctx.user_headers).json()
    share_credential_via_api(client, owner_headers, cred["id"], me["email"])
    _link(client, ctx, cred["id"])

    r = _call(client, ctx, cred["id"])
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == OLD_ACCESS


def test_non_oauth_credential_is_not_refreshable(client, ctx, exchange):
    cred = create_random_credential(client, ctx.user_headers, credential_type="api_token")
    _link(client, ctx, cred["id"])
    r = _call(client, ctx, cred["id"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "not_refreshable"
    exchange.assert_not_called()


def test_mcp_provider_credential_is_not_refreshable(client, ctx, exchange):
    fake_meta = {
        "issuer": "https://external.example.com",
        "authorization_endpoint": "https://external.example.com/oauth/authorize",
        "token_endpoint": "https://external.example.com/oauth/token",
        "registration_endpoint": "https://external.example.com/oauth/register",
    }
    base = "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
    with (
        patch(f"{base}.discover_authorization_server", new=AsyncMock(return_value=fake_meta)),
        patch(f"{base}.register_client", new=AsyncMock(return_value={"client_id": "c", "client_secret": "s"})),
    ):
        r = client.post(
            f"{_BASE}/mcp-providers/connect/external",
            headers=ctx.user_headers,
            json={
                "endpoint_url": "https://external.example.com/mcp",
                "auth_mode": "oauth_dcr",
                "transport": "streamable-http",
                "label": "dcr",
                "mcp_mode_conversation": True,
                "mcp_mode_building": True,
            },
        )
    assert r.status_code == 200, r.text
    cid = str(r.json()["credential_id"])
    _link(client, ctx, cid)
    r = _call(client, ctx, cid)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "not_refreshable"


def test_invalid_grant_is_409_and_second_call_skips_google(client, ctx):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=60)
    _link(client, ctx, cred["id"])
    boom = AsyncMock(side_effect=OAuthRefreshError("reauth_required", "rejected"))
    with patch(EXCHANGE, new=boom), google_enabled():
        r1 = _call(client, ctx, cred["id"])
        r2 = _call(client, ctx, cred["id"])
    assert r1.status_code == 409 and r2.status_code == 409
    assert r1.json()["detail"]["code"] == "reauthorization_required"
    assert r2.json()["detail"]["code"] == "reauthorization_required"
    assert boom.await_count == 1
    assert REFRESH_TOKEN not in r1.text


def test_provider_error_with_expired_token_is_502_with_retry_after(client, ctx):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=-30)
    _link(client, ctx, cred["id"])
    boom = AsyncMock(side_effect=OAuthRefreshError("provider_error", "503"))
    with patch(EXCHANGE, new=boom), google_enabled():
        r1 = _call(client, ctx, cred["id"])
        r2 = _call(client, ctx, cred["id"])
    assert r1.status_code == 502, r1.text
    assert r1.json()["detail"]["code"] == "provider_error"
    assert int(r1.headers["Retry-After"]) >= 1
    assert r2.status_code == 502
    assert r2.headers.get("Retry-After")
    assert boom.await_count == 1  # cooldown: no second Google call


def test_min_ttl_is_clamped(client, ctx, exchange):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=60)
    with patch.object(settings, "GOOGLE_CLIENT_ID", None):
        _link(client, ctx, cred["id"])
    r1 = _call(client, ctx, cred["id"], min_ttl=99999)
    r2 = _call(client, ctx, cred["id"], min_ttl=99999)
    assert r1.status_code == 200 and r1.json()["refreshed"] is True
    assert r2.json()["refreshed"] is False  # 3600 s left > clamp (1800)
    assert exchange.await_count == 1


def test_user_jwt_and_wrong_env_id_are_rejected(client, ctx, exchange):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=10_000)
    _link(client, ctx, cred["id"])
    r = _call(client, ctx, cred["id"], headers=ctx.user_headers)
    assert r.status_code in (401, 403), r.text
    import uuid

    bad = {**ctx.env_headers, "X-Agent-Env-Id": str(uuid.uuid4())}
    r = _call(client, ctx, cred["id"], headers=bad)
    assert r.status_code in (401, 403, 404), r.text
    exchange.assert_not_called()


def test_push_happens_when_known_expiry_differs_or_is_missing_but_not_when_equal(
    client, ctx, exchange
):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=10_000)
    _link(client, ctx, cred["id"])
    expires_at = _call(client, ctx, cred["id"]).json()["expires_at"]

    ctx.adapter.credentials_set = []
    r = _call(client, ctx, cred["id"], known_expires_at=expires_at)
    assert r.status_code == 200 and r.json()["refreshed"] is False
    assert ctx.adapter.credentials_set == []  # env already holds this token

    r = _call(client, ctx, cred["id"], known_expires_at=expires_at - 500)
    assert r.status_code == 200
    assert _pushed(ctx, cred["id"])["access_token"] == OLD_ACCESS

    ctx.adapter.credentials_set = []
    assert _call(client, ctx, cred["id"]).status_code == 200  # field missing
    assert _pushed(ctx, cred["id"]) is not None


def test_push_failure_still_returns_200(client, ctx, exchange):
    cred = _oauth_credential(client, ctx.user_headers, expires_in=10_000)
    _link(client, ctx, cred["id"])
    ctx.adapter.set_credentials = AsyncMock(side_effect=RuntimeError("env unreachable"))
    r = _call(client, ctx, cred["id"], known_expires_at=1)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == OLD_ACCESS


def test_busy_lock_with_expired_token_is_502_refresh_in_progress(client, ctx, exchange):
    from app.services.credentials import oauth_refresh_service as ors

    cred = _oauth_credential(client, ctx.user_headers, expires_in=-30)
    with patch.object(settings, "GOOGLE_CLIENT_ID", None):  # link without refreshing
        _link(client, ctx, cred["id"])
    with (
        patch.object(ors.OAuthRefreshService, "_try_advisory_lock", return_value=False),
        patch.object(ors, "LOCK_WAIT_TIMEOUT_SECONDS", 0.2),
        patch.object(ors, "LOCK_POLL_INTERVAL_SECONDS", 0.05),
    ):
        r = _call(client, ctx, cred["id"])
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "refresh_in_progress"
    assert r.headers["Retry-After"] == "1"
    exchange.assert_not_called()


@pytest.mark.parametrize("type_", ["gmeet_oauth", "gmeet_oauth_readonly"])
def test_gmeet_types_refresh_and_push_without_refresh_token(client, ctx, exchange, type_):
    """Meet credentials are refreshable; the env copy carries no refresh_token."""
    cred = _oauth_credential(client, ctx.user_headers, expires_in=60, type_=type_)
    with patch.object(settings, "GOOGLE_CLIENT_ID", None):  # link without refreshing
        _link(client, ctx, cred["id"])
    pushed = _pushed(ctx, cred["id"])
    assert pushed["access_token"] == OLD_ACCESS
    assert "refresh_token" not in pushed

    r = _call(client, ctx, cred["id"])
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == NEW_ACCESS
    assert r.json()["refreshed"] is True
    exchange.assert_awaited_once_with(REFRESH_TOKEN)
    assert _pushed(ctx, cred["id"])["access_token"] == NEW_ACCESS
    assert "refresh_token" not in _pushed(ctx, cred["id"])
    assert REFRESH_TOKEN not in r.text
