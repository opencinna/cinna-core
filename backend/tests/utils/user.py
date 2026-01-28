from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_email, random_lower_string


def user_authentication_headers(
    *, client: TestClient, email: str, password: str
) -> dict[str, str]:
    data = {"username": email, "password": password}

    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=data)
    response = r.json()
    auth_token = response["access_token"]
    headers = {"Authorization": f"Bearer {auth_token}"}
    return headers


def create_random_user(client: TestClient) -> dict:
    """Create a random user via the signup API and return the response data."""
    email = random_email()
    password = random_lower_string()
    data = {"email": email, "password": password}
    r = client.post(f"{settings.API_V1_STR}/users/signup", json=data)
    assert r.status_code == 200
    result = r.json()
    result["_password"] = password  # stash password for test use
    return result


def authentication_token_from_email(
    *, client: TestClient, email: str
) -> dict[str, str]:
    """
    Return a valid token for the user with given email.

    If the user doesn't exist it is created first via signup API.
    """
    password = random_lower_string()
    # Try to sign up — if user already exists, set password via admin flow
    signup_data = {"email": email, "password": password}
    r = client.post(f"{settings.API_V1_STR}/users/signup", json=signup_data)
    if r.status_code == 200:
        # New user created
        return user_authentication_headers(client=client, email=email, password=password)
    else:
        # User exists — log in as superuser, find user and reset their password
        # For the test user, we use a known password via the admin update endpoint
        su_headers = _get_superuser_headers(client)
        # Get users list to find the user ID
        r = client.get(f"{settings.API_V1_STR}/users/", headers=su_headers)
        assert r.status_code == 200
        users = r.json()["data"]
        user_id = None
        for u in users:
            if u["email"] == email:
                user_id = u["id"]
                break
        if user_id:
            # Update user password via admin endpoint
            client.patch(
                f"{settings.API_V1_STR}/users/{user_id}",
                headers=su_headers,
                json={"password": password},
            )
        return user_authentication_headers(client=client, email=email, password=password)


def _get_superuser_headers(client: TestClient) -> dict[str, str]:
    login_data = {
        "username": settings.FIRST_SUPERUSER,
        "password": settings.FIRST_SUPERUSER_PASSWORD,
    }
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    tokens = r.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}
