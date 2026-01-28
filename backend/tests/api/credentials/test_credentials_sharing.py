import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.credential import create_random_credential
from tests.utils.user import create_random_user, user_authentication_headers


def _create_shareable_credential(
    client: TestClient, token_headers: dict[str, str]
) -> dict:
    """Create a credential with allow_sharing=True."""
    data = {
        "name": "Shareable Cred",
        "type": "email_imap",
        "allow_sharing": True,
        "credential_data": {
            "host": "imap.example.com",
            "port": 993,
            "login": "user@example.com",
            "password": "secret",
            "is_ssl": True,
        },
    }
    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()


def _create_second_user(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a second user and return (user_data, auth_headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    return user, headers


# ---------------------------------------------------------------------------
# ENABLE / DISABLE SHARING (PATCH /{credential_id}/sharing)
# ---------------------------------------------------------------------------

def test_enable_sharing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    assert cred["allow_sharing"] is False

    response = client.patch(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/sharing",
        headers=superuser_token_headers,
        json={"allow_sharing": True},
    )
    assert response.status_code == 200
    assert response.json()["allow_sharing"] is True


def test_disable_sharing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)
    assert cred["allow_sharing"] is True

    response = client.patch(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/sharing",
        headers=superuser_token_headers,
        json={"allow_sharing": False},
    )
    assert response.status_code == 200
    assert response.json()["allow_sharing"] is False


def test_toggle_sharing_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.patch(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}/sharing",
        headers=superuser_token_headers,
        json={"allow_sharing": True},
    )
    assert response.status_code == 404


def test_toggle_sharing_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    _, user2_headers = _create_second_user(client)

    response = client.patch(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/sharing",
        headers=user2_headers,
        json={"allow_sharing": True},
    )
    assert response.status_code == 400
    assert "permissions" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# SHARE CREDENTIAL (POST /{credential_id}/shares)
# ---------------------------------------------------------------------------

def test_share_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, _ = _create_second_user(client)

    response = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    assert response.status_code == 200
    content = response.json()
    assert content["credential_id"] == cred["id"]
    assert content["shared_with_email"] == user2["email"]
    assert content["access_level"] == "read"
    assert "id" in content
    assert "shared_at" in content


def test_share_credential_sharing_not_enabled(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Cannot share a credential that has allow_sharing=false."""
    cred = create_random_credential(client, superuser_token_headers)
    assert cred["allow_sharing"] is False

    user2, _ = _create_second_user(client)

    response = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    assert response.status_code == 400
    assert "sharing" in response.json()["detail"].lower()


def test_share_credential_with_self(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Cannot share a credential with yourself."""
    cred = _create_shareable_credential(client, superuser_token_headers)

    response = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": settings.FIRST_SUPERUSER},
    )
    assert response.status_code == 400
    assert "yourself" in response.json()["detail"].lower()


def test_share_credential_nonexistent_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)

    response = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": "nobody@nonexistent.com"},
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


def test_share_credential_duplicate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Sharing the same credential with the same user twice should fail."""
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, _ = _create_second_user(client)

    # First share succeeds
    r1 = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    assert r1.status_code == 200

    # Second share fails
    r2 = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    assert r2.status_code == 400
    assert "already shared" in r2.json()["detail"].lower()


def test_share_credential_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Only the owner can share a credential."""
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, user2_headers = _create_second_user(client)
    user3, _ = _create_second_user(client)

    response = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=user2_headers,
        json={"shared_with_email": user3["email"]},
    )
    assert response.status_code == 400
    assert "permissions" in response.json()["detail"].lower()


def test_share_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user2, _ = _create_second_user(client)

    response = client.post(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# LIST SHARES (GET /{credential_id}/shares)
# ---------------------------------------------------------------------------

def test_get_credential_shares(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, _ = _create_second_user(client)
    user3, _ = _create_second_user(client)

    # Share with both users
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user3["email"]},
    )

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 2
    assert len(content["data"]) == 2
    emails = {s["shared_with_email"] for s in content["data"]}
    assert user2["email"] in emails
    assert user3["email"] in emails


def test_get_credential_shares_empty(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 0
    assert content["data"] == []


def test_get_credential_shares_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)
    _, user2_headers = _create_second_user(client)

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=user2_headers,
    )
    assert response.status_code == 400
    assert "permissions" in response.json()["detail"].lower()


def test_get_credential_shares_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}/shares",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# SHARED WITH ME (GET /shared-with-me)
# ---------------------------------------------------------------------------

def test_get_credentials_shared_with_me(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, user2_headers = _create_second_user(client)

    # Owner shares with user2
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )

    # user2 should see it in shared-with-me
    response = client.get(
        f"{settings.API_V1_STR}/credentials/shared-with-me",
        headers=user2_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] >= 1
    cred_ids = [c["id"] for c in content["data"]]
    assert cred["id"] in cred_ids


def test_get_credentials_shared_with_me_empty(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A user with no shares should get an empty list."""
    _, user2_headers = _create_second_user(client)

    response = client.get(
        f"{settings.API_V1_STR}/credentials/shared-with-me",
        headers=user2_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 0
    assert content["data"] == []


# ---------------------------------------------------------------------------
# SHARED CREDENTIAL ACCESS VIA CREDENTIAL ROUTES
# ---------------------------------------------------------------------------

def test_shared_user_can_read_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A recipient of a shared credential can read it via GET /{id}."""
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, user2_headers = _create_second_user(client)

    # Share with user2
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )

    # user2 reads the credential
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=user2_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["id"] == cred["id"]
    assert content["is_shared"] is True
    assert content["owner_email"] is not None


def test_share_count_increments(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """share_count should reflect the number of active shares."""
    cred = _create_shareable_credential(client, superuser_token_headers)

    # Initially 0
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.json()["share_count"] == 0

    # Share with a user
    user2, _ = _create_second_user(client)
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )

    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.json()["share_count"] == 1


# ---------------------------------------------------------------------------
# REVOKE SHARE (DELETE /{credential_id}/shares/{share_id})
# ---------------------------------------------------------------------------

def test_revoke_share(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, user2_headers = _create_second_user(client)

    # Share
    share_resp = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    share_id = share_resp.json()["id"]

    # Revoke
    response = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares/{share_id}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Share revoked successfully"

    # user2 can no longer read the credential
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=user2_headers,
    )
    assert response.status_code == 400


def test_revoke_share_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = _create_shareable_credential(client, superuser_token_headers)

    response = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


def test_revoke_share_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Only the credential owner can revoke a share."""
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, user2_headers = _create_second_user(client)

    # Share with user2
    share_resp = client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    share_id = share_resp.json()["id"]

    # user2 tries to revoke — should fail
    response = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares/{share_id}",
        headers=user2_headers,
    )
    assert response.status_code == 400
    assert "permissions" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DISABLE SHARING REVOKES ALL SHARES
# ---------------------------------------------------------------------------

def test_disable_sharing_revokes_all_shares(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Disabling sharing should delete all existing shares immediately."""
    cred = _create_shareable_credential(client, superuser_token_headers)
    user2, user2_headers = _create_second_user(client)
    user3, _ = _create_second_user(client)

    # Share with two users
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user2["email"]},
    )
    client.post(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": user3["email"]},
    )

    # Verify shares exist
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
    )
    assert r.json()["count"] == 2

    # Disable sharing
    client.patch(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/sharing",
        headers=superuser_token_headers,
        json={"allow_sharing": False},
    )

    # All shares should be gone
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
    )
    assert r.json()["count"] == 0

    # user2 can no longer read the credential
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=user2_headers,
    )
    assert response.status_code == 400
