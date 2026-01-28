import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ssh_key import generate_random_ssh_key, import_ssh_key, get_test_key_pair


# ---------------------------------------------------------------------------
# GENERATE
# ---------------------------------------------------------------------------

def test_generate_ssh_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"name": "My Generated Key"}
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/generate",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "My Generated Key"
    assert "id" in content
    assert "public_key" in content
    assert "fingerprint" in content
    assert "created_at" in content
    assert "updated_at" in content
    # Private key should not be returned
    assert "private_key" not in content
    assert "private_key_encrypted" not in content


def test_generate_ssh_key_no_auth(client: TestClient) -> None:
    data = {"name": "No Auth Key"}
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/generate",
        json=data,
    )
    assert response.status_code in (401, 403)


def test_generate_ssh_key_empty_name(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"name": ""}
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/generate",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# IMPORT
# ---------------------------------------------------------------------------

def test_import_ssh_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    public_key, private_key = get_test_key_pair()
    data = {
        "name": "My Imported Key",
        "public_key": public_key,
        "private_key": private_key,
    }
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "My Imported Key"
    assert "id" in content
    assert content["public_key"] == public_key
    assert "fingerprint" in content
    # Private key should not be returned
    assert "private_key" not in content
    assert "private_key_encrypted" not in content


def test_import_ssh_key_with_passphrase(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    public_key, private_key = get_test_key_pair()
    data = {
        "name": "Key With Passphrase",
        "public_key": public_key,
        "private_key": private_key,
        "passphrase": "my-secret-passphrase",
    }
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "Key With Passphrase"
    # Passphrase should not be returned
    assert "passphrase" not in content
    assert "passphrase_encrypted" not in content


def test_import_ssh_key_no_auth(client: TestClient) -> None:
    public_key, private_key = get_test_key_pair()
    data = {
        "name": "No Auth Key",
        "public_key": public_key,
        "private_key": private_key,
    }
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/",
        json=data,
    )
    assert response.status_code in (401, 403)


def test_import_ssh_key_empty_name(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    public_key, private_key = get_test_key_pair()
    data = {
        "name": "",
        "public_key": public_key,
        "private_key": private_key,
    }
    response = client.post(
        f"{settings.API_V1_STR}/ssh-keys/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# READ (single)
# ---------------------------------------------------------------------------

def test_read_ssh_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    key = generate_random_ssh_key(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["id"] == key["id"]
    assert content["name"] == key["name"]
    assert content["public_key"] == key["public_key"]
    assert content["fingerprint"] == key["fingerprint"]


def test_read_ssh_key_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.get(
        f"{settings.API_V1_STR}/ssh-keys/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_read_ssh_key_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    """Normal user cannot read a key owned by superuser."""
    key = generate_random_ssh_key(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

def test_read_ssh_keys(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    generate_random_ssh_key(client, superuser_token_headers)
    generate_random_ssh_key(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/ssh-keys/",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert "data" in content
    assert "count" in content
    assert len(content["data"]) >= 2
    assert content["count"] >= 2


def test_read_ssh_keys_returns_only_own(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    """Each user should only see their own SSH keys."""
    # Create key as superuser
    superuser_key = generate_random_ssh_key(client, superuser_token_headers)
    # List as normal user
    response = client.get(
        f"{settings.API_V1_STR}/ssh-keys/",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    # Normal user's list should not contain superuser's keys
    key_ids = [k["id"] for k in content["data"]]
    assert superuser_key["id"] not in key_ids


def test_read_ssh_keys_no_auth(client: TestClient) -> None:
    response = client.get(f"{settings.API_V1_STR}/ssh-keys/")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------

def test_update_ssh_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    key = generate_random_ssh_key(client, superuser_token_headers)
    update_data = {"name": "Updated Key Name"}
    response = client.put(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=superuser_token_headers,
        json=update_data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "Updated Key Name"
    assert content["id"] == key["id"]
    # Other fields should remain unchanged
    assert content["public_key"] == key["public_key"]
    assert content["fingerprint"] == key["fingerprint"]


def test_update_ssh_key_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.put(
        f"{settings.API_V1_STR}/ssh-keys/{uuid.uuid4()}",
        headers=superuser_token_headers,
        json={"name": "Does Not Exist"},
    )
    assert response.status_code == 404


def test_update_ssh_key_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    key = generate_random_ssh_key(client, superuser_token_headers)
    response = client.put(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=normal_user_token_headers,
        json={"name": "Hacked Name"},
    )
    assert response.status_code == 404


def test_update_ssh_key_empty_name(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    key = generate_random_ssh_key(client, superuser_token_headers)
    response = client.put(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=superuser_token_headers,
        json={"name": ""},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_ssh_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    key = generate_random_ssh_key(client, superuser_token_headers)
    response = client.delete(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "SSH key deleted successfully"

    # Verify it no longer exists
    response = client.get(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_delete_ssh_key_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.delete(
        f"{settings.API_V1_STR}/ssh-keys/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_delete_ssh_key_not_enough_permissions(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    key = generate_random_ssh_key(client, superuser_token_headers)
    response = client.delete(
        f"{settings.API_V1_STR}/ssh-keys/{key['id']}",
        headers=normal_user_token_headers,
    )
    assert response.status_code == 404
