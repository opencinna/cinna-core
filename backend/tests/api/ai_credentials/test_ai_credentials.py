import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.user import create_random_user, user_authentication_headers


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

def test_create_ai_credential_anthropic(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "name": "My Anthropic Key",
        "type": "anthropic",
        "api_key": "sk-ant-api03-test-key-123",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 200
    content = r.json()
    assert content["name"] == "My Anthropic Key"
    assert content["type"] == "anthropic"
    assert content["is_default"] is False
    assert content["has_api_key"] is True
    assert "id" in content
    assert "created_at" in content
    assert "updated_at" in content
    # Sensitive data must not leak
    assert "api_key" not in content
    assert "encrypted_data" not in content


def test_create_ai_credential_minimax(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "name": "Minimax Key",
        "type": "minimax",
        "api_key": "mm-test-key-456",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 200
    content = r.json()
    assert content["name"] == "Minimax Key"
    assert content["type"] == "minimax"
    assert content["has_api_key"] is True


def test_create_ai_credential_openai_compatible(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "name": "OpenAI Compatible",
        "type": "openai_compatible",
        "api_key": "sk-openai-test-789",
        "base_url": "https://api.example.com/v1",
        "model": "gpt-4",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 200
    content = r.json()
    assert content["name"] == "OpenAI Compatible"
    assert content["type"] == "openai_compatible"
    assert content["base_url"] == "https://api.example.com/v1"
    assert content["model"] == "gpt-4"


def test_create_ai_credential_openai_compatible_missing_base_url(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible type requires base_url."""
    data = {
        "name": "Incomplete OAI",
        "type": "openai_compatible",
        "api_key": "sk-test",
        "model": "gpt-4",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 400


def test_create_ai_credential_openai_compatible_missing_model(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible type requires model."""
    data = {
        "name": "Incomplete OAI",
        "type": "openai_compatible",
        "api_key": "sk-test",
        "base_url": "https://api.example.com/v1",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 400


def test_create_ai_credential_missing_api_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """api_key is required for all types."""
    data = {
        "name": "No Key",
        "type": "anthropic",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 422


def test_create_ai_credential_invalid_type(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "name": "Bad Type",
        "type": "nonexistent_provider",
        "api_key": "sk-test",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 422


def test_create_ai_credential_no_auth(client: TestClient) -> None:
    data = {
        "name": "No Auth",
        "type": "anthropic",
        "api_key": "sk-test",
    }
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        json=data,
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# READ (single)
# ---------------------------------------------------------------------------

def test_read_ai_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_ai_credential(client, superuser_token_headers)
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    content = r.json()
    assert content["id"] == cred["id"]
    assert content["name"] == cred["name"]
    assert content["type"] == cred["type"]
    assert content["has_api_key"] is True


def test_read_ai_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_read_ai_credential_other_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A different user cannot read another user's AI credential."""
    cred = create_random_ai_credential(client, superuser_token_headers)

    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )

    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=other_headers,
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

def test_list_ai_credentials(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    create_random_ai_credential(client, superuser_token_headers)
    create_random_ai_credential(client, superuser_token_headers)
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    content = r.json()
    assert "data" in content
    assert "count" in content
    assert content["count"] >= 2
    assert len(content["data"]) >= 2


def test_list_ai_credentials_returns_only_own(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Each user should only see their own AI credentials."""
    create_random_ai_credential(client, superuser_token_headers)

    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=other_headers,
    )
    assert r.status_code == 200
    content = r.json()
    assert content["count"] == 0
    assert len(content["data"]) == 0


def test_list_ai_credentials_no_auth(client: TestClient) -> None:
    r = client.get(f"{settings.API_V1_STR}/ai-credentials/")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------

def test_update_ai_credential_name(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_ai_credential(client, superuser_token_headers)
    r = client.patch(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
        json={"name": "Updated Name"},
    )
    assert r.status_code == 200
    content = r.json()
    assert content["name"] == "Updated Name"
    assert content["id"] == cred["id"]


def test_update_ai_credential_api_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Updating api_key should succeed; response still shows has_api_key=True."""
    cred = create_random_ai_credential(client, superuser_token_headers)
    r = client.patch(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
        json={"api_key": "sk-ant-api03-new-key"},
    )
    assert r.status_code == 200
    content = r.json()
    assert content["has_api_key"] is True
    assert "api_key" not in content


def test_update_ai_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.patch(
        f"{settings.API_V1_STR}/ai-credentials/{uuid.uuid4()}",
        headers=superuser_token_headers,
        json={"name": "Does Not Exist"},
    )
    assert r.status_code == 404


def test_update_ai_credential_other_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A different user cannot update another user's AI credential."""
    cred = create_random_ai_credential(client, superuser_token_headers)

    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )

    r = client.patch(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=other_headers,
        json={"name": "Hacked"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_ai_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_ai_credential(client, superuser_token_headers)
    r = client.delete(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["message"] == "AI credential deleted successfully"

    # Verify it no longer exists
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_delete_ai_credential_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.delete(
        f"{settings.API_V1_STR}/ai-credentials/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_delete_ai_credential_other_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A different user cannot delete another user's AI credential."""
    cred = create_random_ai_credential(client, superuser_token_headers)

    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )

    r = client.delete(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=other_headers,
    )
    assert r.status_code == 403

    # Verify it still exists
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# SET DEFAULT
# ---------------------------------------------------------------------------

def test_set_default_ai_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    cred = create_random_ai_credential(client, superuser_token_headers)
    assert cred["is_default"] is False

    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}/set-default",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    content = r.json()
    assert content["id"] == cred["id"]
    assert content["is_default"] is True


def test_set_default_replaces_previous(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Setting a new default for the same type should unset the previous one."""
    cred1 = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic"
    )
    cred2 = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic"
    )

    # Set cred1 as default
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{cred1['id']}/set-default",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["is_default"] is True

    # Set cred2 as default
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{cred2['id']}/set-default",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["is_default"] is True

    # Verify cred1 is no longer default
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred1['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["is_default"] is False


def test_set_default_different_types_independent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Defaults for different types are independent."""
    anthropic_cred = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic"
    )
    minimax_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="minimax",
        api_key="mm-key-test",
    )

    # Set both as defaults
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{anthropic_cred['id']}/set-default",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200

    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{minimax_cred['id']}/set-default",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200

    # Both should still be default
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{anthropic_cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.json()["is_default"] is True

    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{minimax_cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.json()["is_default"] is True


def test_set_default_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{uuid.uuid4()}/set-default",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_set_default_other_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A different user cannot set-default on another user's credential."""
    cred = create_random_ai_credential(client, superuser_token_headers)

    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )

    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}/set-default",
        headers=other_headers,
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE DEFAULT (side-effect)
# ---------------------------------------------------------------------------

def test_delete_default_credential_removes_from_list(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Deleting a default credential should remove it; list count decreases."""
    cred = create_random_ai_credential(client, superuser_token_headers)
    client.post(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}/set-default",
        headers=superuser_token_headers,
    )

    # Delete the default credential
    r = client.delete(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200

    # Verify gone
    r = client.get(
        f"{settings.API_V1_STR}/ai-credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404
