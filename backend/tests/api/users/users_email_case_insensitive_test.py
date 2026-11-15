"""Email addresses are matched case-insensitively and stored lowercase.

Request schemas lowercase the address, so every write path stores one canonical
form; lookups (login, recovery, grants, credential shares) match regardless of
the case the caller types. A duplicate differing only in case is refused:
400 on signup / admin create, 409 on PATCH (me and admin).
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_lower_string

NEEDS_AGENT_STUBS = False

API = settings.API_V1_STR


def _mixed() -> tuple[str, str]:
    """(mixed-case address, its lowercase form)."""
    local = random_lower_string()[:8]
    domain = random_lower_string()[:8]
    return f"{local.capitalize()}@{domain.upper()}.com", f"{local}@{domain}.com"


def _signup(client: TestClient, email: str, password: str) -> object:
    return client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )


def test_signup_stores_lowercase_and_login_ignores_case(client: TestClient) -> None:
    """
    1. Signup with a mixed-case address → stored lowercase
    2. Login with the lowercase, the mixed and an upper-cased form all work
    3. A duplicate signup differing only in case → 400
    """
    mixed, lower = _mixed()
    password = random_lower_string()

    r = _signup(client, mixed, password)
    assert r.status_code == 200, r.text
    assert r.json()["email"] == lower

    for variant in (lower, mixed, lower.upper()):
        r = client.post(
            f"{API}/login/access-token",
            data={"username": variant, "password": password},
        )
        assert r.status_code == 200, f"login as {variant}: {r.text}"

    r = _signup(client, mixed.swapcase(), random_lower_string())
    assert r.status_code == 400, r.text
    r = _signup(client, lower, random_lower_string())
    assert r.status_code == 400, r.text


def test_admin_create_stores_lowercase_and_refuses_case_duplicate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Admin create with a mixed-case address → stored lowercase
    2. Admin create of the same address in another case → 400
    """
    mixed, lower = _mixed()
    password = random_lower_string()
    r = client.post(
        f"{API}/users/",
        headers=superuser_token_headers,
        json={"email": mixed, "password": password},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == lower

    r = client.post(
        f"{API}/users/",
        headers=superuser_token_headers,
        json={"email": lower.upper(), "password": password},
    )
    assert r.status_code == 400, r.text


def test_patch_refuses_case_only_duplicate_for_me_and_admin(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Two users A and B
    2. A patches own email to B's address in a different case → 409
    3. Admin patches A's email to B's address in a different case → 409
    4. A mixed-case change to a free address is stored lowercase
    5. Re-patching a user's own address in another case is not a conflict
    """
    mixed_a, lower_a = _mixed()
    mixed_b, lower_b = _mixed()
    pw = random_lower_string()
    a = _signup(client, mixed_a, pw).json()
    assert _signup(client, mixed_b, pw).status_code == 200
    headers_a = user_authentication_headers(client=client, email=lower_a, password=pw)

    r = client.patch(
        f"{API}/users/me", headers=headers_a, json={"email": lower_b.upper()}
    )
    assert r.status_code == 409, r.text

    r = client.patch(
        f"{API}/users/{a['id']}",
        headers=superuser_token_headers,
        json={"email": mixed_b},
    )
    assert r.status_code == 409, r.text

    # Own address in another case: not a conflict, stays lowercase.
    r = client.patch(
        f"{API}/users/me", headers=headers_a, json={"email": lower_a.upper()}
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == lower_a

    mixed_free, lower_free = _mixed()
    r = client.patch(
        f"{API}/users/{a['id']}",
        headers=superuser_token_headers,
        json={"email": mixed_free},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == lower_free


def test_password_recovery_finds_user_by_different_case(client: TestClient) -> None:
    """Recovery by an upper-cased address sends the same mail as the stored one."""
    mixed, lower = _mixed()
    assert _signup(client, mixed, random_lower_string()).status_code == 200

    with patch("app.services.users.user_service.send_email") as send:
        r = client.post(f"{API}/password-recovery/{lower.upper()}")
    assert r.status_code == 200, r.text
    assert send.call_count == 1


def test_credential_share_by_email_ignores_case(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Sharing a credential by a differently-cased address finds the user."""
    mixed, lower = _mixed()
    target = _signup(client, mixed, random_lower_string()).json()
    cred = client.post(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "Case Share",
            "type": "email_imap",
            "allow_sharing": True,
            "credential_data": {
                "host": "imap.example.com",
                "port": 993,
                "login": "u@example.com",
                "password": "secret",
                "is_ssl": True,
            },
        },
    ).json()

    r = client.post(
        f"{API}/credentials/{cred['id']}/shares",
        headers=superuser_token_headers,
        json={"shared_with_email": lower.upper()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["shared_with_email"] == target["email"]
