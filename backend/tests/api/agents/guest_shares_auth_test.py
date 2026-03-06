"""
Integration tests: guest share auth flow (Phase 3).

Tests the guest authentication and grant activation endpoints:
  1. Anonymous auth (POST /{token}/auth) — valid, expired, revoked, invalid
  2. Authenticated grant activation (POST /{token}/activate) — valid, idempotent, expired
  3. Public info (GET /{token}/info) — valid, invalid, expired

Only environment adapter is stubbed (via conftest autouse fixtures).
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.guest_share import (
    activate_guest_grant,
    create_guest_share,
    guest_auth,
    guest_share_info,
    mock_expired_guest_share,
    setup_guest_share_agent,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


# ── Tests: Anonymous Auth ────────────────────────────────────────────────


def test_guest_share_anonymous_auth_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full anonymous auth lifecycle:
      1. Create agent + guest share
      2. Anonymous auth with valid token → returns guest JWT
      3. Verify JWT response structure
      4. Anonymous auth with invalid token → 404
    """
    # ── Phase 1: Create agent + guest share ──────────────────────────────

    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Auth Lifecycle Agent",
        share_label="Auth Test Link",
        expires_in_hours=48,
    )
    token = share["token"]
    security_code = share["security_code"]

    # ── Phase 2: Anonymous auth with valid token ─────────────────────────

    body = guest_auth(client, token, security_code=security_code)

    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["guest_share_id"] == share["id"]
    assert body["agent_id"] == agent["id"]
    assert len(body["access_token"]) > 0

    # ── Phase 3: Anonymous auth with invalid token → 404 ─────────────────

    r = client.post(f"{API}/guest-share/totally-invalid-token-that-does-not-exist/auth")
    assert r.status_code == 404


def test_guest_share_anonymous_auth_expired_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous auth with an expired guest share token returns 410.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Expired Auth Agent",
        share_label="Expires Soon",
        expires_in_hours=1,
    )
    token = share["token"]
    security_code = share["security_code"]

    # Confirm it works now
    guest_auth(client, token, security_code=security_code)

    # Simulate expiration
    with mock_expired_guest_share():
        r = client.post(
            f"{API}/guest-share/{token}/auth",
            json={"security_code": security_code},
        )
    assert r.status_code == 410
    assert "expired" in r.json()["detail"].lower() or "revoked" in r.json()["detail"].lower()


def test_guest_share_anonymous_auth_revoked_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous auth with a deleted guest share token returns 404.
      1. Create share, verify auth works
      2. Delete the share
      3. Auth fails — share no longer exists
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Revoked Auth Agent",
        share_label="Will Be Deleted",
    )
    token = share["token"]
    security_code = share["security_code"]

    # Confirm auth works
    guest_auth(client, token, security_code=security_code)

    # Delete the share
    r = client.delete(
        f"{API}/agents/{agent['id']}/guest-shares/{share['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200

    # Auth should now fail
    r = client.post(
        f"{API}/guest-share/{token}/auth",
        json={"security_code": security_code},
    )
    assert r.status_code == 404


# ── Tests: Authenticated Grant Activation ────────────────────────────────


def test_guest_share_activate_grant(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Authenticated grant activation:
      1. Create agent + guest share
      2. Second user activates the grant → returns agent info
      3. Verify response structure
      4. Idempotent: calling activate again succeeds
    """
    # ── Phase 1: Create agent + guest share ──────────────────────────────

    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Grant Activation Agent",
        share_label="Activate Test",
    )
    token = share["token"]
    security_code = share["security_code"]

    # ── Phase 2: Create second user + activate grant ─────────────────────

    _, user_headers = create_random_user_with_headers(client)
    body = activate_guest_grant(client, user_headers, token, security_code=security_code)

    assert body["guest_share_id"] == share["id"]
    assert body["agent_id"] == agent["id"]
    assert body["agent_name"] == "Grant Activation Agent"

    # ── Phase 3: Idempotent — activate again ─────────────────────────────

    body2 = activate_guest_grant(client, user_headers, token, security_code=security_code)
    assert body2["guest_share_id"] == share["id"]
    assert body2["agent_id"] == agent["id"]


def test_guest_share_activate_expired_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Grant activation with an expired token returns 410.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Expired Grant Agent",
        share_label="Expires Soon Grant",
        expires_in_hours=1,
    )
    token = share["token"]
    security_code = share["security_code"]

    _, user_headers = create_random_user_with_headers(client)

    # Confirm it works now
    activate_guest_grant(client, user_headers, token, security_code=security_code)

    # Simulate expiration
    with mock_expired_guest_share():
        r = client.post(
            f"{API}/guest-share/{token}/activate",
            headers=user_headers,
            json={"security_code": security_code},
        )
    assert r.status_code == 410


def test_guest_share_activate_invalid_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Grant activation with a completely invalid token returns 404.
    """
    _, user_headers = create_random_user_with_headers(client)

    r = client.post(
        f"{API}/guest-share/nonexistent-invalid-token/activate",
        headers=user_headers,
    )
    assert r.status_code == 404


# ── Tests: Public Info ───────────────────────────────────────────────────


def test_guest_share_info_valid_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Public info endpoint returns agent name and is_valid=true for a valid token.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Info Agent",
        share_label="Info Test",
    )

    body = guest_share_info(client, share["token"])
    assert body["agent_name"] == "Info Agent"
    assert body["is_valid"] is True
    assert body["guest_share_id"] == share["id"]
    assert body["requires_code"] is True
    assert body["is_code_blocked"] is False


def test_guest_share_info_invalid_token(
    client: TestClient,
) -> None:
    """
    Public info endpoint returns is_valid=false for an unknown token.
    """
    body = guest_share_info(client, "completely-unknown-token")
    assert body["is_valid"] is False
    assert body["guest_share_id"] is None
    assert body["agent_name"] is None


def test_guest_share_info_expired_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Public info endpoint returns is_valid=false for an expired token.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Expired Info Agent",
        share_label="Expires Info",
        expires_in_hours=1,
    )

    with mock_expired_guest_share():
        body = guest_share_info(client, share["token"])

    assert body["is_valid"] is False
    assert body["guest_share_id"] == share["id"]


def test_guest_share_info_returns_allow_env_panel(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Public info endpoint returns allow_env_panel reflecting the share configuration:
      1. Create share with allow_env_panel=True → info returns allow_env_panel=True
      2. Create share without allow_env_panel → info returns allow_env_panel=False
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Env Panel Info Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 1: allow_env_panel=True → info returns True ─────────────────

    share_on = create_guest_share(
        client, superuser_token_headers, agent_id,
        label="env-panel-on-info",
        allow_env_panel=True,
    )
    body = guest_share_info(client, share_on["token"])

    assert body["is_valid"] is True
    assert body["allow_env_panel"] is True, "Info endpoint must return allow_env_panel=True"
    assert body["guest_share_id"] == share_on["id"]
    assert body["agent_name"] == "Env Panel Info Agent"

    # ── Phase 2: allow_env_panel omitted (defaults False) → info returns False

    share_off = create_guest_share(
        client, superuser_token_headers, agent_id,
        label="env-panel-off-info",
    )
    body_off = guest_share_info(client, share_off["token"])

    assert body_off["is_valid"] is True
    assert body_off["allow_env_panel"] is False, "Info endpoint must return allow_env_panel=False by default"
    assert body_off["guest_share_id"] == share_off["id"]
