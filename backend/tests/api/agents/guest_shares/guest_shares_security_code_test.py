"""
Integration tests: guest share security code feature.

Tests the security code verification flow:
  1. Code included in creation response (4 digits, numeric)
  2. Auth succeeds with correct code
  3. Auth fails with wrong code (shows remaining attempts)
  4. Link blocked after 3 failures (correct code also rejected)
  5. Owner resets blocked state by editing code
  6. Owner can view code in list/get
  7. Auth without code returns 403 "Security code is required"
  8. Activate (authenticated user) also requires code
  9. Info endpoint returns requires_code and is_code_blocked

Only environment adapter is stubbed (via conftest autouse fixtures).
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.guest_share import (
    activate_guest_grant,
    create_guest_share,
    get_guest_share,
    guest_auth,
    guest_share_info,
    list_guest_shares,
    setup_guest_share_agent,
    update_guest_share,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


# ── Tests: Security Code on Creation ─────────────────────────────────────


def test_security_code_included_in_creation_response(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Creating a guest share returns a 4-digit numeric security code.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Code Creation Agent",
        share_label="Code Test",
    )

    assert "security_code" in share
    code = share["security_code"]
    assert isinstance(code, str)
    assert len(code) == 4
    assert code.isdigit()


# ── Tests: Auth with Security Code ───────────────────────────────────────


def test_auth_succeeds_with_correct_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous auth succeeds when the correct security code is provided.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Correct Code Agent",
    )

    body = guest_auth(client, share["token"], security_code=share["security_code"])
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_auth_fails_without_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous auth fails with 403 when no security code is provided.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="No Code Agent",
    )

    r = client.post(f"{API}/guest-share/{share['token']}/auth")
    assert r.status_code == 403
    assert "security code is required" in r.json()["detail"].lower()


def test_auth_fails_with_wrong_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous auth fails with 403 and shows remaining attempts on wrong code.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Wrong Code Agent",
    )

    # Use a wrong code (different from the real one)
    wrong_code = "0000" if share["security_code"] != "0000" else "1111"

    r = client.post(
        f"{API}/guest-share/{share['token']}/auth",
        json={"security_code": wrong_code},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "incorrect" in detail.lower()
    assert "2 attempt(s) remaining" in detail


def test_link_blocked_after_three_failures(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    After 3 wrong code attempts, the link is blocked.
    Even the correct code is rejected afterwards.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Block Agent",
    )
    token = share["token"]
    correct_code = share["security_code"]
    wrong_code = "0000" if correct_code != "0000" else "1111"

    # Attempt 1: wrong code → 2 remaining
    r = client.post(f"{API}/guest-share/{token}/auth", json={"security_code": wrong_code})
    assert r.status_code == 403
    assert "2 attempt(s) remaining" in r.json()["detail"]

    # Attempt 2: wrong code → 1 remaining
    r = client.post(f"{API}/guest-share/{token}/auth", json={"security_code": wrong_code})
    assert r.status_code == 403
    assert "1 attempt(s) remaining" in r.json()["detail"]

    # Attempt 3: wrong code → blocked
    r = client.post(f"{API}/guest-share/{token}/auth", json={"security_code": wrong_code})
    assert r.status_code == 403
    assert "blocked" in r.json()["detail"].lower()

    # Attempt with correct code → still blocked
    r = client.post(f"{API}/guest-share/{token}/auth", json={"security_code": correct_code})
    assert r.status_code == 403
    assert "blocked" in r.json()["detail"].lower()

    # Info endpoint reflects blocked state
    info = guest_share_info(client, token)
    assert info["is_code_blocked"] is True


# ── Tests: Owner Resets Blocked State ─────────────────────────────────────


def test_owner_resets_blocked_by_editing_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner can unblock a link by setting a new security code.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Reset Block Agent",
    )
    token = share["token"]
    agent_id = agent["id"]
    share_id = share["id"]
    wrong_code = "0000" if share["security_code"] != "0000" else "1111"

    # Block the link with 3 wrong attempts
    for _ in range(3):
        client.post(f"{API}/guest-share/{token}/auth", json={"security_code": wrong_code})

    # Confirm it's blocked
    r = client.post(f"{API}/guest-share/{token}/auth", json={"security_code": share["security_code"]})
    assert r.status_code == 403

    # Owner updates the code
    new_code = "5678"
    updated = update_guest_share(
        client, superuser_token_headers, agent_id, share_id, security_code=new_code,
    )
    assert updated["security_code"] == new_code
    assert updated["is_code_blocked"] is False

    # Now guest can auth with the new code
    body = guest_auth(client, token, security_code=new_code)
    assert "access_token" in body


# ── Tests: Owner Views Code ───────────────────────────────────────────────


def test_owner_can_view_code_in_list(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner can see the decrypted security code in the guest shares list.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="View Code List Agent",
    )

    shares = list_guest_shares(client, superuser_token_headers, agent["id"])
    assert len(shares) == 1
    assert shares[0]["security_code"] == share["security_code"]
    assert shares[0]["is_code_blocked"] is False


def test_owner_can_view_code_in_get(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner can see the decrypted security code when getting a single share.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="View Code Get Agent",
    )

    fetched = get_guest_share(client, superuser_token_headers, agent["id"], share["id"])
    assert fetched["security_code"] == share["security_code"]
    assert fetched["is_code_blocked"] is False


# ── Tests: Activate Also Requires Code ────────────────────────────────────


def test_activate_fails_without_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Grant activation fails with 403 when no security code is provided.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Activate No Code Agent",
    )

    _, user_headers = create_random_user_with_headers(client)

    r = client.post(
        f"{API}/guest-share/{share['token']}/activate",
        headers=user_headers,
    )
    assert r.status_code == 403
    assert "security code is required" in r.json()["detail"].lower()


def test_activate_succeeds_with_correct_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Grant activation succeeds when the correct security code is provided.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Activate Code Agent",
    )

    _, user_headers = create_random_user_with_headers(client)
    body = activate_guest_grant(
        client, user_headers, share["token"], security_code=share["security_code"],
    )
    assert body["guest_share_id"] == share["id"]
    assert body["agent_id"] == agent["id"]


# ── Tests: Info Endpoint ──────────────────────────────────────────────────


def test_info_returns_requires_code_and_blocked_status(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Info endpoint returns requires_code=true and is_code_blocked for shares with codes.
    """
    _, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Info Code Agent",
    )

    info = guest_share_info(client, share["token"])
    assert info["requires_code"] is True
    assert info["is_code_blocked"] is False
    assert info["is_valid"] is True


# ── Tests: Update Guest Share ──────────────────────────────────────────────


def test_update_guest_share_label(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner can update the label of a guest share.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Update Label Agent",
        share_label="Original Label",
    )

    updated = update_guest_share(
        client, superuser_token_headers, agent["id"], share["id"],
        label="New Label",
    )
    assert updated["label"] == "New Label"
    assert updated["security_code"] == share["security_code"]  # code unchanged


def test_update_guest_share_security_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner can update the security code of a guest share.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Update Code Agent",
    )

    new_code = "9876"
    updated = update_guest_share(
        client, superuser_token_headers, agent["id"], share["id"],
        security_code=new_code,
    )
    assert updated["security_code"] == new_code

    # Old code should fail
    r = client.post(
        f"{API}/guest-share/{share['token']}/auth",
        json={"security_code": share["security_code"]},
    )
    # If old code == new code, it would succeed, so use a definitely different one
    if share["security_code"] != new_code:
        assert r.status_code == 403

    # New code should work
    body = guest_auth(client, share["token"], security_code=new_code)
    assert "access_token" in body
