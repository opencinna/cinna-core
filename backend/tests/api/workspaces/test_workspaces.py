import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.workspace import create_random_workspace
from tests.utils.user import create_random_user, user_authentication_headers


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

def test_create_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"name": "My Workspace"}
    response = client.post(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "My Workspace"
    assert "id" in content
    assert "user_id" in content
    assert "created_at" in content
    assert "updated_at" in content


def test_create_workspace_with_icon(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"name": "Finance Workspace", "icon": "dollar-sign"}
    response = client.post(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "Finance Workspace"
    assert content["icon"] == "dollar-sign"


def test_create_workspace_name_too_short(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"name": ""}
    response = client.post(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=superuser_token_headers,
        json=data,
    )
    assert response.status_code == 422


def test_create_workspace_no_auth(client: TestClient) -> None:
    data = {"name": "Unauthorized Workspace"}
    response = client.post(
        f"{settings.API_V1_STR}/user-workspaces/",
        json=data,
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# READ (single)
# ---------------------------------------------------------------------------

def test_read_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    workspace = create_random_workspace(client, superuser_token_headers)
    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["id"] == workspace["id"]
    assert content["name"] == workspace["name"]
    assert content["user_id"] == workspace["user_id"]


def test_read_workspace_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_read_workspace_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """User cannot read a workspace owned by another user."""
    workspace = create_random_workspace(client, superuser_token_headers)

    # Create second user
    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )

    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=user2_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

def test_list_workspaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    create_random_workspace(client, superuser_token_headers)
    create_random_workspace(client, superuser_token_headers)

    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert "data" in content
    assert "count" in content
    assert len(content["data"]) >= 2
    assert content["count"] >= 2


def test_list_workspaces_returns_only_own(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Each user should only see their own workspaces."""
    # Create workspace as superuser
    su_workspace = create_random_workspace(client, superuser_token_headers)

    # Create second user and their workspace
    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )
    user2_workspace = create_random_workspace(client, user2_headers, name="User2 Workspace")

    # List as user2
    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=user2_headers,
    )
    assert response.status_code == 200
    content = response.json()

    # User2's list should contain their workspace but not superuser's
    workspace_ids = [w["id"] for w in content["data"]]
    assert user2_workspace["id"] in workspace_ids
    assert su_workspace["id"] not in workspace_ids


def test_list_workspaces_pagination(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    # Create 3 workspaces
    for i in range(3):
        create_random_workspace(client, superuser_token_headers, name=f"Workspace {i}")

    # Test skip and limit
    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/?skip=0&limit=2",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert len(content["data"]) == 2
    assert content["count"] >= 3  # Total count should still reflect all workspaces


def test_list_workspaces_empty(client: TestClient) -> None:
    """A new user with no workspaces should get an empty list."""
    user = create_random_user(client)
    user_headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )

    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=user_headers,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 0
    assert content["data"] == []


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------

def test_update_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    workspace = create_random_workspace(client, superuser_token_headers)

    update_data = {"name": "Updated Workspace Name"}
    response = client.put(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
        json=update_data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "Updated Workspace Name"
    assert content["id"] == workspace["id"]


def test_update_workspace_icon(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    workspace = create_random_workspace(client, superuser_token_headers)

    update_data = {"icon": "rocket"}
    response = client.put(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
        json=update_data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["icon"] == "rocket"


def test_update_workspace_name_and_icon(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    workspace = create_random_workspace(client, superuser_token_headers)

    update_data = {"name": "New Name", "icon": "briefcase"}
    response = client.put(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
        json=update_data,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["name"] == "New Name"
    assert content["icon"] == "briefcase"


def test_update_workspace_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.put(
        f"{settings.API_V1_STR}/user-workspaces/{uuid.uuid4()}",
        headers=superuser_token_headers,
        json={"name": "Does Not Exist"},
    )
    assert response.status_code == 404


def test_update_workspace_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """User cannot update a workspace owned by another user."""
    workspace = create_random_workspace(client, superuser_token_headers)

    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )

    response = client.put(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=user2_headers,
        json={"name": "Hacked Name"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_workspace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    workspace = create_random_workspace(client, superuser_token_headers)

    response = client.delete(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Workspace deleted successfully"

    # Verify it no longer exists
    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_delete_workspace_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    response = client.delete(
        f"{settings.API_V1_STR}/user-workspaces/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert response.status_code == 404


def test_delete_workspace_not_owner(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """User cannot delete a workspace owned by another user."""
    workspace = create_random_workspace(client, superuser_token_headers)

    user2 = create_random_user(client)
    user2_headers = user_authentication_headers(
        client=client, email=user2["email"], password=user2["_password"]
    )

    response = client.delete(
        f"{settings.API_V1_STR}/user-workspaces/{workspace['id']}",
        headers=user2_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# MULTIPLE WORKSPACES
# ---------------------------------------------------------------------------

def test_user_can_have_multiple_workspaces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A user can create multiple workspaces."""
    workspace1 = create_random_workspace(client, superuser_token_headers, name="Work")
    workspace2 = create_random_workspace(client, superuser_token_headers, name="Personal")
    workspace3 = create_random_workspace(client, superuser_token_headers, name="Side Project")

    response = client.get(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200
    content = response.json()

    workspace_ids = [w["id"] for w in content["data"]]
    assert workspace1["id"] in workspace_ids
    assert workspace2["id"] in workspace_ids
    assert workspace3["id"] in workspace_ids


def test_workspaces_have_unique_ids(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Each workspace should have a unique ID."""
    workspace1 = create_random_workspace(client, superuser_token_headers)
    workspace2 = create_random_workspace(client, superuser_token_headers)

    assert workspace1["id"] != workspace2["id"]


def test_workspaces_can_have_same_name(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Workspaces can have the same name (no unique constraint on name)."""
    workspace1 = create_random_workspace(client, superuser_token_headers, name="Duplicate Name")
    workspace2 = create_random_workspace(client, superuser_token_headers, name="Duplicate Name")

    assert workspace1["name"] == workspace2["name"]
    assert workspace1["id"] != workspace2["id"]
