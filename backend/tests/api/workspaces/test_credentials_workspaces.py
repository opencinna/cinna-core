import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.workspace import create_random_workspace
from tests.utils.credential import create_random_credential
from tests.utils.user import create_random_user, user_authentication_headers


def _create_credential_in_workspace(
    client: TestClient,
    token_headers: dict[str, str],
    workspace_id: str | None = None,
) -> dict:
    """Create a credential optionally assigned to a workspace."""
    data = {
        "name": "Test Credential",
        "type": "email_imap",
        "credential_data": {
            "host": "imap.example.com",
            "port": 993,
            "login": "user@example.com",
            "password": "secret",
            "is_ssl": True,
        },
    }
    if workspace_id is not None:
        data["user_workspace_id"] = workspace_id

    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# CREATE CREDENTIAL WITH WORKSPACE
# ---------------------------------------------------------------------------

def test_create_credential_in_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A credential can be created with a workspace assignment."""
    workspace = create_random_workspace(client, superuser_token_headers)

    cred = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )

    assert cred["user_workspace_id"] == workspace["id"]


def test_create_credential_without_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A credential without workspace assignment belongs to default workspace (null)."""
    cred = _create_credential_in_workspace(client, superuser_token_headers)

    assert cred["user_workspace_id"] is None


def test_create_credential_invalid_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Creating a credential with non-existent workspace should fail."""
    data = {
        "name": "Test Credential",
        "type": "email_imap",
        "user_workspace_id": str(uuid.uuid4()),
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
    assert response.status_code == 400
    assert "workspace" in response.json()["detail"].lower()


def test_create_credential_in_other_users_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """User cannot create credential in another user's workspace."""
    # Create workspace as superuser
    workspace = create_random_workspace(client, superuser_token_headers)

    # Create second user
    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )

    # User2 tries to create credential in superuser's workspace
    data = {
        "name": "Test Credential",
        "type": "email_imap",
        "user_workspace_id": workspace["id"],
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
        headers=user2_headers,
        json=data,
    )
    # Should fail - user doesn't own the workspace
    assert response.status_code == 403
    assert "workspace" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# LIST CREDENTIALS - WORKSPACE FILTERING
# ---------------------------------------------------------------------------

def test_list_credentials_filter_by_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Credentials can be filtered by workspace."""
    workspace1 = create_random_workspace(client, superuser_token_headers, name="Workspace 1")
    workspace2 = create_random_workspace(client, superuser_token_headers, name="Workspace 2")

    cred1 = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace1["id"]
    )
    cred2 = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace2["id"]
    )

    # Filter by workspace1
    response = client.get(
        f"{settings.API_V1_STR}/credentials/?user_workspace_id={workspace1['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()

    cred_ids = [c["id"] for c in content["data"]]
    assert cred1["id"] in cred_ids
    assert cred2["id"] not in cred_ids


def test_list_credentials_filter_by_default_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Empty string filter returns credentials in default workspace (null)."""
    workspace = create_random_workspace(client, superuser_token_headers)

    # Credential in workspace
    cred_in_workspace = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )
    # Credential in default workspace
    cred_in_default = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=None
    )

    # Filter for default workspace using empty string
    response = client.get(
        f"{settings.API_V1_STR}/credentials/?user_workspace_id=",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()

    cred_ids = [c["id"] for c in content["data"]]
    assert cred_in_default["id"] in cred_ids
    assert cred_in_workspace["id"] not in cred_ids


def test_list_credentials_no_filter_returns_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Without workspace filter, all credentials are returned."""
    workspace = create_random_workspace(client, superuser_token_headers)

    cred_in_workspace = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )
    cred_in_default = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=None
    )

    # No filter
    response = client.get(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()

    cred_ids = [c["id"] for c in content["data"]]
    assert cred_in_workspace["id"] in cred_ids
    assert cred_in_default["id"] in cred_ids


def test_list_credentials_invalid_workspace_filter(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Invalid workspace ID format should return 400."""
    response = client.get(
        f"{settings.API_V1_STR}/credentials/?user_workspace_id=not-a-uuid",
        headers=superuser_token_headers,
    )
    assert response.status_code == 400
    assert "Invalid workspace ID format" in response.json()["detail"]


# ---------------------------------------------------------------------------
# READ CREDENTIAL - WORKSPACE ASSOCIATION
# ---------------------------------------------------------------------------

def test_read_credential_shows_workspace_id(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Reading a credential shows its workspace assignment."""
    workspace = create_random_workspace(client, superuser_token_headers)
    cred = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["user_workspace_id"] == workspace["id"]


def test_read_credential_in_default_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Credential in default workspace has null workspace_id."""
    cred = _create_credential_in_workspace(client, superuser_token_headers)

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["user_workspace_id"] is None


# ---------------------------------------------------------------------------
# WORKSPACE ISOLATION BETWEEN USERS
# ---------------------------------------------------------------------------

def test_users_cannot_see_other_users_credentials_in_workspaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Users can only see their own credentials, regardless of workspace."""
    # Superuser creates workspace and credential
    su_workspace = create_random_workspace(client, superuser_token_headers)
    su_cred = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=su_workspace["id"]
    )

    # User2 creates their own workspace and credential
    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )
    user2_workspace = create_random_workspace(client, user2_headers, name="User2 WS")
    user2_cred = _create_credential_in_workspace(
        client, user2_headers, workspace_id=user2_workspace["id"]
    )

    # User2 lists their credentials - should only see their own
    response = client.get(
        f"{settings.API_V1_STR}/credentials/",
        headers=user2_headers,
    )
    assert response.status_code == 200
    content = response.json()

    cred_ids = [c["id"] for c in content["data"]]
    assert user2_cred["id"] in cred_ids
    assert su_cred["id"] not in cred_ids


def test_users_cannot_read_other_users_credentials_in_workspaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """User cannot read another user's credential even if they know the ID."""
    workspace = create_random_workspace(client, superuser_token_headers)
    cred = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )

    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=user2_headers,
    )
    assert response.status_code == 400
    assert "permissions" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE WORKSPACE - CASCADE TO CREDENTIALS
# ---------------------------------------------------------------------------

def test_delete_workspace_moves_credentials_to_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Deleting a workspace should move its credentials to the default workspace."""
    workspace = create_random_workspace(client, superuser_token_headers)
    cred = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )

    # Delete workspace
    response = client.delete(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200

    # Credential should still exist but moved to default workspace (null)
    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["user_workspace_id"] is None


# ---------------------------------------------------------------------------
# MULTIPLE CREDENTIALS IN SAME WORKSPACE
# ---------------------------------------------------------------------------

def test_multiple_credentials_in_same_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A workspace can contain multiple credentials."""
    workspace = create_random_workspace(client, superuser_token_headers)

    cred1 = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )
    cred2 = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )
    cred3 = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )

    # Filter by workspace
    response = client.get(
        f"{settings.API_V1_STR}/credentials/?user_workspace_id={workspace['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()

    cred_ids = [c["id"] for c in content["data"]]
    assert cred1["id"] in cred_ids
    assert cred2["id"] in cred_ids
    assert cred3["id"] in cred_ids


def test_credentials_distributed_across_workspaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Credentials can be distributed across multiple workspaces and default."""
    workspace1 = create_random_workspace(client, superuser_token_headers, name="Work")
    workspace2 = create_random_workspace(client, superuser_token_headers, name="Personal")

    cred_work = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace1["id"]
    )
    cred_personal = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace2["id"]
    )
    cred_default = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=None
    )

    # Verify each credential is in its correct workspace
    r1 = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_work['id']}",
        headers=superuser_token_headers,
    )
    assert r1.json()["user_workspace_id"] == workspace1["id"]

    r2 = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_personal['id']}",
        headers=superuser_token_headers,
    )
    assert r2.json()["user_workspace_id"] == workspace2["id"]

    r3 = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_default['id']}",
        headers=superuser_token_headers,
    )
    assert r3.json()["user_workspace_id"] is None


# ---------------------------------------------------------------------------
# CREDENTIAL WITH_DATA - WORKSPACE PRESERVED
# ---------------------------------------------------------------------------

def test_credential_with_data_shows_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The with-data endpoint also returns workspace_id."""
    workspace = create_random_workspace(client, superuser_token_headers)
    cred = _create_credential_in_workspace(
        client, superuser_token_headers, workspace_id=workspace["id"]
    )

    response = client.get(
        f"{settings.API_V1_STR}/credentials/{cred['id']}/with-data",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["user_workspace_id"] == workspace["id"]
    assert "credential_data" in content
