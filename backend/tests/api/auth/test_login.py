
from fastapi.testclient import TestClient

from app.core.config import settings
from app.utils import generate_password_reset_token
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string


def test_get_access_token(client: TestClient) -> None:
    login_data = {
        "username": settings.FIRST_SUPERUSER,
        "password": settings.FIRST_SUPERUSER_PASSWORD,
    }
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    tokens = r.json()
    assert r.status_code == 200
    assert "access_token" in tokens
    assert tokens["access_token"]


def test_get_access_token_incorrect_password(client: TestClient) -> None:
    login_data = {
        "username": settings.FIRST_SUPERUSER,
        "password": "incorrect",
    }
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 400


def test_use_access_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.post(
        f"{settings.API_V1_STR}/login/test-token",
        headers=superuser_token_headers,
    )
    result = r.json()
    assert r.status_code == 200
    assert "email" in result


# ``test_recovery_password`` and ``test_recovery_password_user_not_exits`` used
# to live here. Both pinned the pre-fix contract — a ``404`` for an unknown
# address and the old ``"Password recovery email sent"`` body — which was an
# account-existence oracle on an unauthenticated, unrate-limited route. The
# whole surface, including the reversal of the "D7 decision: keep 404" that
# put it there, is now covered in
# ``tests/api/auth/password_recovery_enumeration_test.py``.


def test_over_long_passwords_fail_login_identically_for_any_address(
    client: TestClient,
) -> None:
    """A malformed candidate is an ordinary failed login, whoever it is for.

    passlib raises ``PasswordSizeError`` once a candidate exceeds
    ``MAX_PASSWORD_SIZE`` (4096 bytes — bcrypt's 72-byte truncation is not the
    raising threshold). Uncaught, that raise lands on exactly *one* of the two
    login branches: a registered address reaches the real comparison and 500s,
    while an unknown address is refused with 400 before any hashing. Either way
    round, the status code answers "does this address have an account".

    That is what this asserts, and it asserts it at the boundary as well as
    over it. 4096 is the largest accepted candidate and 5000 is clearly past
    it, so a refactor that reintroduces the raise — or that moves the guard to
    only one of the two branches — splits the four responses into groups and
    fails here. The comparison is between the responses themselves rather than
    against a literal, so a re-worded refusal cannot repair it by being edited
    once.
    """
    known_email = random_email()
    known_password = random_lower_string()
    signup = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json={"email": known_email, "password": known_password},
    )
    assert signup.status_code == 200, signup.text
    unknown_email = random_email()

    boundary = "a" * 4096
    clearly_over = "a" * 5000

    responses = [
        client.post(
            f"{settings.API_V1_STR}/login/access-token",
            data={"username": email, "password": password},
        )
        for email in (known_email, unknown_email)
        for password in (boundary, clearly_over)
    ]

    distinct = {(r.status_code, r.content) for r in responses}
    assert len(distinct) == 1, [
        (r.status_code, r.text[:200]) for r in responses
    ]
    for response in responses:
        assert response.status_code == 400, response.text
        assert response.json() == {"detail": "Incorrect email or password"}

    # Not a vacuous pass: the account is real and its real password works.
    ok = client.post(
        f"{settings.API_V1_STR}/login/access-token",
        data={"username": known_email, "password": known_password},
    )
    assert ok.status_code == 200, ok.text


def test_reset_password(client: TestClient) -> None:
    # Create a user via signup API
    email = random_email()
    password = random_lower_string()
    new_password = random_lower_string()

    signup_data = {"email": email, "password": password}
    r = client.post(f"{settings.API_V1_STR}/users/signup", json=signup_data)
    assert r.status_code == 200

    token = generate_password_reset_token(email=email)
    headers = user_authentication_headers(client=client, email=email, password=password)
    data = {"new_password": new_password, "token": token}

    r = client.post(
        f"{settings.API_V1_STR}/reset-password/",
        headers=headers,
        json=data,
    )

    assert r.status_code == 200
    assert r.json() == {"message": "Password updated successfully"}

    # Verify new password works by logging in
    login_data = {"username": email, "password": new_password}
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_reset_password_invalid_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"new_password": "changethis", "token": "invalid"}
    r = client.post(
        f"{settings.API_V1_STR}/reset-password/",
        headers=superuser_token_headers,
        json=data,
    )
    response = r.json()

    assert "detail" in response
    assert r.status_code == 400
    assert response["detail"] == "Invalid token"
