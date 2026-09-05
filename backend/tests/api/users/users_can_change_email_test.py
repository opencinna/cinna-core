"""``can_change_email`` — one policy fact, projected on every ``UserPublic``.

Users may edit their own address only while the instance has *no* allowed-email
pattern list. With one configured, the address is the identity the policy is
written against, so a user who could edit it could move themselves outside the
allowlist an admin set.

Two things are asserted together on purpose:

* the **flag** is present on every endpoint that answers with a ``UserPublic``
  (the field has no default — a producer that forgets it must fail loudly
  rather than ship the permissive answer), and
* the **enforcement** in ``PATCH /users/me`` agrees with the flag. A frontend
  that hides a field the API would have accepted is a nuisance; a frontend that
  shows one the API refuses is a bug report, and the two drift apart the moment
  they are derived separately.

The rest of the access policy is covered in
``tests/api/server_config/access_policy_test.py`` and
``tests/api/auth/access_policy_gating_test.py``.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.server_config import set_access_policy
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

# No agents or environments here — opt out of the heavy env stubs in
# tests/api/users/conftest.py.
NEEDS_AGENT_STUBS = False

API = settings.API_V1_STR


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get(f"{API}/users/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_email_editability_follows_the_allowed_pattern_list(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The flag and the rule move together:
      1. No pattern list → ``can_change_email`` is true and the change works
      2. Admin configures a list → the flag flips to false
      3. The email branch of ``PATCH /users/me`` now answers 403
      4. Other profile fields stay editable — only the address is frozen
      5. Clearing the list restores both the flag and the ability
    """
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )

    # ── Phase 1: Unrestricted instance ────────────────────────────────
    assert user["can_change_email"] is True
    assert _me(client, headers)["can_change_email"] is True

    new_email = random_email()
    changed = client.patch(f"{API}/users/me", headers=headers, json={"email": new_email})
    assert changed.status_code == 200, changed.text
    assert changed.json()["email"] == new_email
    assert changed.json()["can_change_email"] is True

    # The change is real, not just echoed back.
    assert _me(client, headers)["email"] == new_email

    # ── Phase 2: A pattern list makes the address load-bearing ────────
    set_access_policy(
        client, superuser_token_headers, allowed_email_patterns="*@acme.com"
    )
    assert _me(client, headers)["can_change_email"] is False

    # ── Phase 3: The route enforces what the flag advertises ──────────
    refused = client.patch(
        f"{API}/users/me", headers=headers, json={"email": random_email()}
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "Email changes are not allowed"

    # Not even to an address the pattern list *would* accept: the rule is
    # "the admin owns the address", not "stay inside the list".
    assert (
        client.patch(
            f"{API}/users/me",
            headers=headers,
            json={"email": f"{random_lower_string()}@acme.com"},
        ).status_code
        == 403
    )
    assert _me(client, headers)["email"] == new_email

    # ── Phase 4: Everything else on the profile still saves ───────────
    renamed = client.patch(
        f"{API}/users/me", headers=headers, json={"full_name": "Renamed Person"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["full_name"] == "Renamed Person"
    assert renamed.json()["can_change_email"] is False

    # ── Phase 5: Clearing the list gives the address back ─────────────
    set_access_policy(client, superuser_token_headers, allowed_email_patterns="")
    assert _me(client, headers)["can_change_email"] is True
    final_email = random_email()
    assert (
        client.patch(
            f"{API}/users/me", headers=headers, json={"email": final_email}
        ).status_code
        == 200
    )
    assert _me(client, headers)["email"] == final_email


def test_every_user_projection_carries_the_flag(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The field is instance-wide, so every producer must agree:
      1. Signup, ``/users/me``, ``/login/test-token``
      2. The admin single-user read and the admin list
      3. All of them flip together when the policy changes
    """
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )

    def _flags() -> dict[str, bool]:
        """``can_change_email`` as reported by every endpoint that projects it."""
        me = _me(client, headers)
        test_token = client.post(f"{API}/login/test-token", headers=headers)
        assert test_token.status_code == 200, test_token.text
        admin_read = client.get(
            f"{API}/users/{user['id']}", headers=superuser_token_headers
        )
        assert admin_read.status_code == 200, admin_read.text
        listed = client.get(
            f"{API}/users/", headers=superuser_token_headers, params={"limit": 100}
        )
        assert listed.status_code == 200, listed.text
        rows = [row for row in listed.json()["data"] if row["id"] == user["id"]]
        assert rows, "the user must appear in the admin list"
        return {
            "me": me["can_change_email"],
            "test_token": test_token.json()["can_change_email"],
            "admin_read": admin_read.json()["can_change_email"],
            "admin_list": rows[0]["can_change_email"],
        }

    # ── Phase 1 & 2: Every projection says the same thing ─────────────
    assert user["can_change_email"] is True
    assert set(_flags().values()) == {True}

    # ── Phase 3: And they move together ───────────────────────────────
    set_access_policy(
        client, superuser_token_headers, allowed_email_patterns="*@acme.com"
    )
    assert set(_flags().values()) == {False}
