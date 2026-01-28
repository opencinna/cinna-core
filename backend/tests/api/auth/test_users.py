import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_email, random_lower_string


def _signup_user(client: TestClient, email: str | None = None, password: str | None = None) -> dict:
    """Helper to create a user via signup API and return response + password."""
    email = email or random_email()
    password = password or random_lower_string()
    data = {"email": email, "password": password}
    r = client.post(f"{settings.API_V1_STR}/users/signup", json=data)
    assert r.status_code == 200
    result = r.json()
    result["_password"] = password
    return result


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    """Helper to log in and return auth headers."""
    login_data = {"username": email, "password": password}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 200
    tokens = r.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_get_users_superuser_me(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    current_user = r.json()
    assert current_user
    assert current_user["is_active"] is True
    assert current_user["is_superuser"]
    assert current_user["email"] == settings.FIRST_SUPERUSER


def test_get_users_normal_user_me(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=normal_user_token_headers)
    current_user = r.json()
    assert current_user
    assert current_user["is_active"] is True
    assert current_user["is_superuser"] is False
    assert current_user["email"] == settings.EMAIL_TEST_USER


def test_create_user_new_email(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    with (
        patch("app.utils.send_email", return_value=None),
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
    ):
        username = random_email()
        password = random_lower_string()
        data = {"email": username, "password": password}
        r = client.post(
            f"{settings.API_V1_STR}/users/",
            headers=superuser_token_headers,
            json=data,
        )
        assert 200 <= r.status_code < 300
        created_user = r.json()
        assert created_user["email"] == username

        # Verify user exists by logging in
        login_data = {"username": username, "password": password}
        r2 = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
        assert r2.status_code == 200


def test_get_existing_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user_data = _signup_user(client)
    user_id = user_data["id"]
    r = client.get(
        f"{settings.API_V1_STR}/users/{user_id}",
        headers=superuser_token_headers,
    )
    assert 200 <= r.status_code < 300
    api_user = r.json()
    assert api_user["email"] == user_data["email"]


def test_get_existing_user_current_user(client: TestClient) -> None:
    user_data = _signup_user(client)
    user_id = user_data["id"]
    headers = _login(client, user_data["email"], user_data["_password"])

    r = client.get(
        f"{settings.API_V1_STR}/users/{user_id}",
        headers=headers,
    )
    assert 200 <= r.status_code < 300
    api_user = r.json()
    assert api_user["email"] == user_data["email"]


def test_get_existing_user_permissions_error(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    r = client.get(
        f"{settings.API_V1_STR}/users/{uuid.uuid4()}",
        headers=normal_user_token_headers,
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "The user doesn't have enough privileges"}


def test_create_user_existing_username(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user_data = _signup_user(client)
    data = {"email": user_data["email"], "password": random_lower_string()}
    r = client.post(
        f"{settings.API_V1_STR}/users/",
        headers=superuser_token_headers,
        json=data,
    )
    created_user = r.json()
    assert r.status_code == 400
    assert "_id" not in created_user


def test_create_user_by_normal_user(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    username = random_email()
    password = random_lower_string()
    data = {"email": username, "password": password}
    r = client.post(
        f"{settings.API_V1_STR}/users/",
        headers=normal_user_token_headers,
        json=data,
    )
    assert r.status_code == 403


def test_retrieve_users(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _signup_user(client)
    _signup_user(client)

    r = client.get(f"{settings.API_V1_STR}/users/", headers=superuser_token_headers)
    all_users = r.json()

    assert len(all_users["data"]) > 1
    assert "count" in all_users
    for item in all_users["data"]:
        assert "email" in item


def test_update_user_me(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    full_name = "Updated Name"
    email = random_email()
    data = {"full_name": full_name, "email": email}
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=normal_user_token_headers,
        json=data,
    )
    assert r.status_code == 200
    updated_user = r.json()
    assert updated_user["email"] == email
    assert updated_user["full_name"] == full_name


def test_update_password_me(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    new_password = random_lower_string()
    data = {
        "current_password": settings.FIRST_SUPERUSER_PASSWORD,
        "new_password": new_password,
    }
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/password",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 200
    updated_user = r.json()
    assert updated_user["message"] == "Password updated successfully"

    # Verify new password works by logging in
    login_data = {"username": settings.FIRST_SUPERUSER, "password": new_password}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 200

    # Revert to the old password to keep consistency in test
    old_data = {
        "current_password": new_password,
        "new_password": settings.FIRST_SUPERUSER_PASSWORD,
    }
    # Need new token since password changed
    new_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/password",
        headers=new_headers,
        json=old_data,
    )
    assert r.status_code == 200

    # Verify old password works again
    login_data = {"username": settings.FIRST_SUPERUSER, "password": settings.FIRST_SUPERUSER_PASSWORD}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 200


def test_update_password_me_incorrect_password(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    new_password = random_lower_string()
    data = {"current_password": new_password, "new_password": new_password}
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/password",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 400
    updated_user = r.json()
    assert updated_user["detail"] == "Incorrect password"


def test_update_user_me_email_exists(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    user_data = _signup_user(client)

    data = {"email": user_data["email"]}
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=normal_user_token_headers,
        json=data,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "User with this email already exists"


def test_update_password_me_same_password_error(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {
        "current_password": settings.FIRST_SUPERUSER_PASSWORD,
        "new_password": settings.FIRST_SUPERUSER_PASSWORD,
    }
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/password",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 400
    updated_user = r.json()
    assert (
        updated_user["detail"] == "New password cannot be the same as the current one"
    )


def test_register_user(client: TestClient) -> None:
    username = random_email()
    password = random_lower_string()
    full_name = random_lower_string()
    data = {"email": username, "password": password, "full_name": full_name}
    r = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json=data,
    )
    assert r.status_code == 200
    created_user = r.json()
    assert created_user["email"] == username
    assert created_user["full_name"] == full_name

    # Verify user can log in with the password
    login_data = {"username": username, "password": password}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 200


def test_register_user_already_exists_error(client: TestClient) -> None:
    password = random_lower_string()
    full_name = random_lower_string()
    data = {
        "email": settings.FIRST_SUPERUSER,
        "password": password,
        "full_name": full_name,
    }
    r = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json=data,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "The user with this email already exists in the system"


def test_update_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user_data = _signup_user(client)

    data = {"full_name": "Updated_full_name"}
    r = client.patch(
        f"{settings.API_V1_STR}/users/{user_data['id']}",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 200
    updated_user = r.json()
    assert updated_user["full_name"] == "Updated_full_name"


def test_update_user_not_exists(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"full_name": "Updated_full_name"}
    r = client.patch(
        f"{settings.API_V1_STR}/users/{uuid.uuid4()}",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "The user with this id does not exist in the system"


def test_update_user_email_exists(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user1 = _signup_user(client)
    user2 = _signup_user(client)

    data = {"email": user2["email"]}
    r = client.patch(
        f"{settings.API_V1_STR}/users/{user1['id']}",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "User with this email already exists"


def test_delete_user_me(client: TestClient) -> None:
    user_data = _signup_user(client)
    headers = _login(client, user_data["email"], user_data["_password"])

    r = client.delete(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
    )
    assert r.status_code == 200
    deleted_user = r.json()
    assert deleted_user["message"] == "User deleted successfully"

    # Verify user can no longer log in
    login_data = {"username": user_data["email"], "password": user_data["_password"]}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 400


def test_delete_user_me_as_superuser(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.delete(
        f"{settings.API_V1_STR}/users/me",
        headers=superuser_token_headers,
    )
    assert r.status_code == 403
    response = r.json()
    assert response["detail"] == "Super users are not allowed to delete themselves"


def test_delete_user_super_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user_data = _signup_user(client)
    user_id = user_data["id"]
    r = client.delete(
        f"{settings.API_V1_STR}/users/{user_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    deleted_user = r.json()
    assert deleted_user["message"] == "User deleted successfully"

    # Verify user can no longer log in
    login_data = {"username": user_data["email"], "password": user_data["_password"]}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 400


def test_delete_user_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.delete(
        f"{settings.API_V1_STR}/users/{uuid.uuid4()}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_delete_user_current_super_user_error(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    # Get the superuser's ID via /users/me
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    user_id = r.json()["id"]

    r = client.delete(
        f"{settings.API_V1_STR}/users/{user_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Super users are not allowed to delete themselves"


def test_delete_user_without_privileges(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    user_data = _signup_user(client)

    r = client.delete(
        f"{settings.API_V1_STR}/users/{user_data['id']}",
        headers=normal_user_token_headers,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "The user doesn't have enough privileges"
