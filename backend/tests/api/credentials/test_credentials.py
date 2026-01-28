import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.credential import create_random_credential


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

def test_create_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "name": "My IMAP Account",
        "type": "email_imap",
        "notes": "For testing",
        "credential_data": {
            "host": "imap.example.com",
            "port": 993,
            "login": "user@example.com",
            "password": "secret",
            "is_ssl": True,
        },
    }
    response = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == data["name"]
    assert content["type"] == data["type"]
    assert content["notes"] == data["notes"]
    assert "id" in content
    assert "owner_id" in content
    # Public response must not leak credential_data
    assert "credential_data" not in content
    assert "encrypted_data" not in content


def test_create_credential_without_data(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Creating a credential with no credential_data should succeed (placeholder)."""
    data = {
        "name": "Empty Credential",
        "type": "odoo",
    }
    response = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "Empty Credential"
    assert content["type"] == "odoo"
    assert content["status"] == "incomplete"


def test_create_credential_various_types(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """All credential types should be creatable."""
    types_to_test = [
        "email_imap",
        "odoo",
        "api_token",
        "gmail_oauth",
        "gmail_oauth_readonly",
        "gdrive_oauth",
        "gdrive_oauth_readonly",
        "gcalendar_oauth",
        "gcalendar_oauth_readonly",
    ]
    for cred_type in types_to_test:
        cred = create_random_credential(
            client, superuser_token_headers, credential_type=cred_type
        )
        assert cred["type"] == cred_type
        assert cred["status"] == "complete"


def test_create_credential_invalid_type(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "name": "Bad Type",
        "type": "nonexistent_type",
    }
    response = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 422


def test_create_credential_no_auth(client: TestClient) -> None:
    data = {
        "name": "No Auth Credential",
        "type": "email_imap",
    }
    response = client.post(
        f"{settings.API_V1_STR}/credentials/",
        json=data,
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# READ (single)
# ---------------------------------------------------------------------------

def test_read_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["id"] == cred["id"]
    assert content["name"] == cred["name"]
    assert content["type"] == cred["type"]
    assert content["owner_id"] == cred["owner_id"]


def test_read_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_read_credential_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    """Normal user cannot read a credential owned by superuser."""
    cred = create_random_credential(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# READ with decrypted data
# ---------------------------------------------------------------------------

def test_read_credential_with_data(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    credential_data = {
        "host": "imap.test.com",
        "port": 993,
        "login": "me@test.com",
        "password": "my-secret",
        "is_ssl": True,
    }
    cred = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="email_imap",
        credential_data=credential_data,
    )
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/with-data",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["id"] == cred["id"]
    assert "credential_data" in content
    assert content["credential_data"]["host"] == "imap.test.com"
    assert content["credential_data"]["login"] == "me@test.com"
    assert content["credential_data"]["password"] == "my-secret"


def test_read_credential_with_data_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}/with-data",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_read_credential_with_data_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/with-data",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

def test_read_credentials(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    create_random_credential(client, superuser_token_headers)
    create_random_credential(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert "data" in content
    assert "count" in content
    assert len(content["data"]) >= 2


def test_read_credentials_returns_only_own(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    """Each user should only see their own credentials."""
    # Create credential as superuser
    create_random_credential(client, superuser_token_headers)
    # List as normal user
    response = client.get(
        f"{settings.API_V1_STR}/credentials/",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    # Normal user's list should not contain superuser's credentials
    for cred in content["data"]:
        assert cred["is_shared"] is False  # only own credentials


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------

def test_update_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    update_data = {"name": "Updated Name", "notes": "Updated notes"}
    response = client.put(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
        json=update_data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "Updated Name"
    assert content["notes"] == "Updated notes"
    assert content["id"] == cred["id"]


def test_update_credential_data(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Updating credential_data should re-encrypt and be retrievable."""
    cred = create_random_credential(client, superuser_token_headers)
    new_credential_data = {
        "host": "imap.updated.com",
        "port": 143,
        "login": "new@updated.com",
        "password": "new-password",
        "is_ssl": False,
    }
    response = client.put(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
        json={"credential_data": new_credential_data},
    )
    assert response.status_code == 200

    # Verify by reading with data
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/with-data",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["credential_data"]["host"] == "imap.updated.com"
    assert content["credential_data"]["port"] == 143
    assert content["credential_data"]["login"] == "new@updated.com"


def test_update_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.put(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}",
        headers=superuser_token_headers,
        json={"name": "Does Not Exist"},
    )
    assert response.status_code == 404


def test_update_credential_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    response = client.put(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=normal_user_token_headers,
        json={"name": "Hacked Name"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    response = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Credential deleted successfully"

    # Verify it no longer exists
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_delete_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.delete(
        f"{settings.API_V1_STR}/credentials/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_delete_credential_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    cred = create_random_credential(client, superuser_token_headers)
    response = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# STATUS / COMPLETENESS
# ---------------------------------------------------------------------------

def test_credential_status_complete(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A credential with all required fields should have status='complete'."""
    cred = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="email_imap",
        credential_data={
            "host": "imap.example.com",
            "port": 993,
            "login": "user@example.com",
            "password": "pass",
            "is_ssl": True,
        },
    )
    assert cred["status"] == "complete"


def test_credential_status_incomplete(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A credential missing required fields should have status='incomplete'."""
    cred = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="email_imap",
        credential_data={
            "host": "imap.example.com",
            # missing port, login, password
        },
    )
    assert cred["status"] == "incomplete"


def test_credential_status_incomplete_empty_data(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A credential with empty data should have status='incomplete'."""
    cred = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="odoo",
        credential_data={},
    )
    assert cred["status"] == "incomplete"
