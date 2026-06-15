"""Unit / service-level tests for the configurable DEFAULT_USER_ROLE setting.

Complements ``tests/api/users/users_default_role_test.py`` (API integration tests)
with three areas that either have no clean HTTP surface or are pure logic:

  A. ``RoleService.derive_default_role`` — pure-logic unit tests (no DB needed).
     * Superuser always returns 'admin'.
     * Non-superuser returns settings.DEFAULT_USER_ROLE (agent-user / agent-developer).
     * Defensive fallback: any value outside {agent-user, agent-developer} yields
       agent-user (guards the no-admin-for-non-superuser invariant).

  B. ``AuthService.create_user_from_google`` — service-level tests using the shared
     ``db`` fixture (no TestClient; no real Google token exchange).
     * Default setting: a Google first-login user gets 'agent-user'.
     * agent-developer setting: a Google first-login user gets 'agent-developer'.

  C. ``Settings`` model validation — pure Pydantic construction tests (no DB).
     * 'admin', 'superadmin', and other bogus values all raise ValidationError,
       confirming the Literal constraint is enforced at startup.

Cross-reference: API-observable behavior for the password-signup path is in
``tests/api/users/users_default_role_test.py``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app.services.users.role_service import RoleService


# ── A. derive_default_role — pure-logic tests (no DB) ───────────────────────


class TestDeriveDefaultRole:
    """Pure-logic tests for ``RoleService.derive_default_role``.

    These tests call the function directly; no database is needed.
    """

    def test_superuser_always_returns_admin(self) -> None:
        """Superuser flag maps to 'admin' regardless of DEFAULT_USER_ROLE."""
        with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-developer"):
            result = RoleService.derive_default_role(is_superuser=True)
        assert result == "admin"

    def test_superuser_returns_admin_with_default_setting(self) -> None:
        """Superuser maps to 'admin' when DEFAULT_USER_ROLE is at its default."""
        result = RoleService.derive_default_role(is_superuser=True)
        assert result == "admin"

    def test_non_superuser_defaults_to_agent_user(self) -> None:
        """With DEFAULT_USER_ROLE at its default, non-superuser gets 'agent-user'."""
        # Explicitly ensure we're hitting the real default value
        with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-user"):
            result = RoleService.derive_default_role(is_superuser=False)
        assert result == "agent-user"

    def test_non_superuser_gets_agent_developer_when_configured(self) -> None:
        """Non-superuser gets 'agent-developer' when DEFAULT_USER_ROLE is patched."""
        with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-developer"):
            result = RoleService.derive_default_role(is_superuser=False)
        assert result == "agent-developer"

    def test_defensive_fallback_for_invalid_configured_value(self) -> None:
        """If DEFAULT_USER_ROLE somehow holds a value outside the allowed set
        (e.g. after a Settings bypass), derive_default_role falls back to
        'agent-user' — the no-admin-for-non-superuser invariant is preserved."""
        # Bypass the Literal constraint by patching the already-constructed settings
        # object directly (simulates a future Settings widening or runtime override).
        with patch("app.core.config.settings.DEFAULT_USER_ROLE", "admin"):
            result = RoleService.derive_default_role(is_superuser=False)
        assert result == "agent-user"

    def test_defensive_fallback_for_completely_bogus_value(self) -> None:
        """Completely unknown values also fall back to 'agent-user'."""
        with patch("app.core.config.settings.DEFAULT_USER_ROLE", "superadmin"):
            result = RoleService.derive_default_role(is_superuser=False)
        assert result == "agent-user"


# ── B. AuthService.create_user_from_google — service-level tests (uses DB) ──


class TestCreateUserFromGoogleRole:
    """Service-level tests for ``AuthService.create_user_from_google``.

    These bypass the full OAuth exchange (no real Google token needed) and call
    the service method directly.  They require the shared ``db`` fixture so they
    belong in tests/unit/ as documented service-level tests (see unit README).
    """

    def test_google_first_login_default_role_is_agent_user(self, db: Session) -> None:
        """With DEFAULT_USER_ROLE at its default, a Google first-login user gets
        role 'agent-user'."""
        from tests.utils.utils import random_email, random_lower_string
        from app.services.users.auth_service import AuthService

        email = random_email()
        google_id = f"google_{random_lower_string()}"

        user = AuthService.create_user_from_google(
            session=db,
            email=email,
            google_id=google_id,
            full_name="Test User",
        )

        assert user.role == "agent-user"
        assert user.is_superuser is False
        assert user.google_id == google_id

    def test_google_first_login_gets_agent_developer_when_setting_configured(
        self, db: Session
    ) -> None:
        """When DEFAULT_USER_ROLE is 'agent-developer', a Google first-login user
        receives 'agent-developer'."""
        from tests.utils.utils import random_email, random_lower_string
        from app.services.users.auth_service import AuthService

        email = random_email()
        google_id = f"google_{random_lower_string()}"

        with patch("app.core.config.settings.DEFAULT_USER_ROLE", "agent-developer"):
            user = AuthService.create_user_from_google(
                session=db,
                email=email,
                google_id=google_id,
                full_name="Test Developer",
            )

        assert user.role == "agent-developer"
        assert user.is_superuser is False


# ── C. Settings validation — Pydantic construction tests (no DB) ─────────────


class TestSettingsValidation:
    """Tests that invalid DEFAULT_USER_ROLE values are rejected at Settings
    construction time — confirming 'admin' cannot be configured as the
    self-signup default and invalid values fail loud at startup."""

    def _build_minimal_settings_kwargs(self) -> dict:
        """Return the minimal required fields to construct a Settings instance.

        We take the existing (valid) settings object as a template so the test
        doesn't break if required fields are added in future.
        """
        from app.core.config import settings as live_settings

        # Extract all required / non-defaulted fields from the live instance so
        # we don't have to enumerate them manually here.
        return {
            "PROJECT_NAME": live_settings.PROJECT_NAME,
            "POSTGRES_SERVER": live_settings.POSTGRES_SERVER,
            "POSTGRES_PORT": live_settings.POSTGRES_PORT,
            "POSTGRES_USER": live_settings.POSTGRES_USER,
            "POSTGRES_PASSWORD": live_settings.POSTGRES_PASSWORD,
            "POSTGRES_DB": live_settings.POSTGRES_DB,
            "FIRST_SUPERUSER": live_settings.FIRST_SUPERUSER,
            "FIRST_SUPERUSER_PASSWORD": live_settings.FIRST_SUPERUSER_PASSWORD,
            # Avoid "changethis" warnings / errors from the model validator
            "SECRET_KEY": "test-secret-key-that-is-long-enough-for-testing",
            "ENCRYPTION_KEY": "test-encryption-key-that-is-long-enough-ok",
        }

    def test_admin_is_rejected_as_default_user_role(self) -> None:
        """'admin' is not an allowed DEFAULT_USER_ROLE value — it must raise
        ValidationError at Settings construction time."""
        from app.core.config import Settings

        kwargs = self._build_minimal_settings_kwargs()
        with pytest.raises(ValidationError):
            Settings(**kwargs, DEFAULT_USER_ROLE="admin")  # type: ignore[arg-type]

    def test_superadmin_is_rejected_as_default_user_role(self) -> None:
        """Completely bogus values must also raise ValidationError."""
        from app.core.config import Settings

        kwargs = self._build_minimal_settings_kwargs()
        with pytest.raises(ValidationError):
            Settings(**kwargs, DEFAULT_USER_ROLE="superadmin")  # type: ignore[arg-type]

    def test_valid_agent_user_value_accepted(self) -> None:
        """'agent-user' is a valid value and must not raise."""
        from app.core.config import Settings

        kwargs = self._build_minimal_settings_kwargs()
        s = Settings(**kwargs, DEFAULT_USER_ROLE="agent-user")
        assert s.DEFAULT_USER_ROLE == "agent-user"

    def test_valid_agent_developer_value_accepted(self) -> None:
        """'agent-developer' is a valid value and must not raise."""
        from app.core.config import Settings

        kwargs = self._build_minimal_settings_kwargs()
        s = Settings(**kwargs, DEFAULT_USER_ROLE="agent-developer")
        assert s.DEFAULT_USER_ROLE == "agent-developer"
