"""API-level tests for the configurable DEFAULT_USER_ROLE setting.

Covers the observable API surface of the creation-time role assignment:

  1. Default (unset) — password-signup creates a user with role ``agent-user``
     (regression guard for current behavior).
  2. agent-developer configured — patching ``settings.DEFAULT_USER_ROLE`` to
     ``agent-developer`` causes a new signup user to receive that role.
  3. Superuser path unaffected — creating a user with ``is_superuser=True`` always
     yields role ``admin`` regardless of ``DEFAULT_USER_ROLE``.
  4. Explicit role override preserved — if a superuser creates a user with an
     explicit ``role`` in the payload, that role is honoured and is NOT overridden
     by ``DEFAULT_USER_ROLE``.

The Google OAuth first-login path is tested at the service level in
``tests/unit/test_default_user_role_service.py`` because it requires calling
``AuthService.create_user_from_google`` directly (no HTTP route for a clean
first-login simulation without real Google token exchange).

Unit tests for ``derive_default_role`` and ``Settings`` validation also live in
``tests/unit/test_default_user_role_service.py``.

No agents or environments are created in this file, so the heavy env stubs
from the users conftest are not needed.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
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


# ── Scenario 1: Default (unset) — signup yields agent-user ───────────────────


def test_signup_default_role_is_agent_user(client: TestClient) -> None:
    """With DEFAULT_USER_ROLE at its default ('agent-user'), a new signup user gets
    role 'agent-user'. This is the regression guard for the existing behavior."""
    user = _signup_user(client)
    assert user["role"] == "agent-user"
    assert user["is_superuser"] is False


# ── Scenario 2: agent-developer configured — signup gets agent-developer ──────


def test_signup_role_respects_agent_developer_setting(client: TestClient) -> None:
    """When DEFAULT_USER_ROLE is patched to 'agent-developer', a new password-signup
    user receives role 'agent-developer'."""
    with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-developer"):
        user = _signup_user(client)

    assert user["role"] == "agent-developer"
    assert user["is_superuser"] is False


# ── Scenario 3: Superuser creation ignores DEFAULT_USER_ROLE ─────────────────


def test_superuser_always_gets_admin_role_regardless_of_setting(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Creating a user with is_superuser=True always yields role 'admin',
    even when DEFAULT_USER_ROLE is set to 'agent-developer'."""
    with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-developer"):
        user = _create_user_as_superuser(
            client,
            superuser_token_headers,
            extra_fields={"is_superuser": True},
        )

    assert user["role"] == "admin"
    assert user["is_superuser"] is True


def test_superuser_gets_admin_role_with_default_setting(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """With DEFAULT_USER_ROLE at its default, a newly created superuser still gets
    role 'admin' (baseline check for the superuser ⇔ admin invariant)."""
    user = _create_user_as_superuser(
        client,
        superuser_token_headers,
        extra_fields={"is_superuser": True},
    )
    assert user["role"] == "admin"
    assert user["is_superuser"] is True


# ── Scenario 4: Explicit caller-provided role is honoured ─────────────────────


def test_explicit_role_in_payload_is_not_overridden_by_setting(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """When a superuser explicitly provides a 'role' in the create-user payload,
    that role wins over DEFAULT_USER_ROLE. Here agent-user is explicitly set while
    DEFAULT_USER_ROLE is patched to agent-developer — the user gets agent-user."""
    with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-developer"):
        user = _create_user_as_superuser(
            client,
            superuser_token_headers,
            extra_fields={"role": "agent-user"},
        )

    assert user["role"] == "agent-user"


def test_explicit_agent_developer_role_honoured_when_setting_is_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A caller-provided 'agent-developer' role is honoured even when DEFAULT_USER_ROLE
    is at its default ('agent-user')."""
    # DEFAULT_USER_ROLE is "agent-user" here (the default)
    user = _create_user_as_superuser(
        client,
        superuser_token_headers,
        extra_fields={"role": "agent-developer"},
    )
    assert user["role"] == "agent-developer"
