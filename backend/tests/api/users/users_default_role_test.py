"""The role a new account gets, and where that default now comes from.

The default used to be the ``DEFAULT_USER_ROLE`` env setting. It is now a
column on the ``server_config`` singleton, edited on the Access tab of
``/admin/server-configuration`` and read through ``AccessPolicyService``; the
env setting survives only to seed a brand-new instance, and is deliberately not
consulted at runtime — a setting that is sometimes authoritative and sometimes
shadowed by the database is the worst of both, because an operator edits
``.env``, nothing changes, and nothing complains.

So these tests configure the role **through the admin API**, which is both the
real source of truth and the only one that can drift.

Covered here:

  1. Default instance — password signup yields ``agent-user``
  2. Configured ``agent-developer`` — signup and Google first-login both pick
     it up, and existing users are untouched (the default is creation-time only)
  3. Superuser creation always yields ``admin``, whatever the default says
  4. An explicit ``role`` in an admin create payload wins over the default

The admin endpoint's refusal to store ``admin`` as the default is covered in
``tests/api/server_config/access_policy_test.py``; the clamp that protects the
never-``admin``-for-a-non-superuser invariant even if a stored value drifts out
of range is unit-tested in ``tests/unit/test_default_user_role_service.py``.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.google_oauth import login_with_google, random_google_id
from tests.utils.server_config import set_access_policy
from tests.utils.utils import random_email, random_lower_string

# Opt out of the heavy agent/env stubs from tests/api/users/conftest.py
# (mirrors the pattern used by test_mfa_*.py and users_search_test.py).
NEEDS_AGENT_STUBS = False

API = settings.API_V1_STR


def _signup_user(client: TestClient, email: str | None = None, password: str | None = None) -> dict:
    """Create a user via the public signup API and return the response body."""
    email = email or random_email()
    password = password or random_lower_string()
    r = client.post(f"{API}/users/signup", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    body["_password"] = password
    return body


def _create_user_as_superuser(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    email: str | None = None,
    password: str | None = None,
    extra_fields: dict | None = None,
) -> dict:
    """Create a user via the admin POST /users/ endpoint."""
    email = email or random_email()
    password = password or random_lower_string()
    payload: dict = {"email": email, "password": password}
    if extra_fields:
        payload.update(extra_fields)
    with (
        patch("app.utils.send_email", return_value=None),
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
    ):
        r = client.post(f"{API}/users/", headers=superuser_token_headers, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{API}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario 1: Default instance — signup yields agent-user ──────────────────


def test_signup_default_role_is_agent_user(client: TestClient) -> None:
    """An instance nobody has configured still creates ordinary users.

    Regression guard for the shipped default, and the control that keeps the
    next test from passing vacuously.
    """
    user = _signup_user(client)
    assert user["role"] == "agent-user"
    assert user["is_superuser"] is False


# ── Scenario 2: The configured default is what new accounts get ──────────────


def test_configured_default_role_applies_to_new_accounts_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The admin-configured default, end to end:
      1. A user signs up before the change → ``agent-user``
      2. Admin sets the default to ``agent-developer``
      3. A new password signup gets ``agent-developer``
      4. A Google first-login gets it too — one default, every path
      5. The earlier user is unchanged: the default is creation-time only
      6. Setting it back applies to the next account, not to the previous ones
    """
    # ── Phase 1: Before ───────────────────────────────────────────────
    early = _signup_user(client)
    assert early["role"] == "agent-user"

    # ── Phase 2: Change the default through the admin surface ─────────
    assert (
        set_access_policy(
            client, superuser_token_headers, default_user_role="agent-developer"
        )["default_user_role"]
        == "agent-developer"
    )

    # ── Phase 3: Password signup ──────────────────────────────────────
    assert _signup_user(client)["role"] == "agent-developer"

    # ── Phase 4: Google first login ───────────────────────────────────
    google_headers = login_with_google(
        client, email=random_email(), google_id=random_google_id()
    )
    google_user = _me(client, google_headers)
    assert google_user["role"] == "agent-developer"
    assert google_user["is_superuser"] is False

    # ── Phase 5: Existing accounts are never re-roled ─────────────────
    early_read = client.get(
        f"{API}/users/{early['id']}", headers=superuser_token_headers
    )
    assert early_read.status_code == 200, early_read.text
    assert early_read.json()["role"] == "agent-user"

    # ── Phase 6: And back again ───────────────────────────────────────
    set_access_policy(client, superuser_token_headers, default_user_role="agent-user")
    assert _signup_user(client)["role"] == "agent-user"


# ── Scenario 3: Superuser creation ignores the configured default ────────────


def test_superuser_always_gets_admin_role_regardless_of_the_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The role ⇔ is_superuser invariant outranks the configured default."""
    set_access_policy(
        client, superuser_token_headers, default_user_role="agent-developer"
    )
    user = _create_user_as_superuser(
        client,
        superuser_token_headers,
        extra_fields={"is_superuser": True},
    )

    assert user["role"] == "admin"
    assert user["is_superuser"] is True


def test_superuser_gets_admin_role_with_the_default_policy(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Baseline for the invariant on an unconfigured instance."""
    user = _create_user_as_superuser(
        client,
        superuser_token_headers,
        extra_fields={"is_superuser": True},
    )
    assert user["role"] == "admin"
    assert user["is_superuser"] is True


# ── Scenario 4: Explicit caller-provided role is honoured ─────────────────────


def test_explicit_role_in_payload_is_not_overridden_by_the_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An admin naming a role means it, even when the default says otherwise."""
    set_access_policy(
        client, superuser_token_headers, default_user_role="agent-developer"
    )
    user = _create_user_as_superuser(
        client,
        superuser_token_headers,
        extra_fields={"role": "agent-user"},
    )

    assert user["role"] == "agent-user"


def test_explicit_agent_developer_role_honoured_when_the_default_is_agent_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The mirror image: an explicit upgrade on an otherwise default instance."""
    user = _create_user_as_superuser(
        client,
        superuser_token_headers,
        extra_fields={"role": "agent-developer"},
    )
    assert user["role"] == "agent-developer"
