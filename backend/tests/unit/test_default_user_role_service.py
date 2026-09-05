"""Unit tests for the creation-time default role and the policy logic under it.

The default role moved out of the ``DEFAULT_USER_ROLE`` env setting and into
``server_config.default_user_role``, resolved by ``AccessPolicyService``. The
env setting is now seed-only: it fills the column when the singleton row is
first created and is never read for a live decision. Tests that patch it and
expect a behaviour change are therefore asserting the *old* contract, which is
why this file no longer does that anywhere.

What is left here is what is genuinely pure logic — no database, no client:

  A. ``RoleService.derive_default_role`` — the superuser branch, which answers
     before it ever looks at the policy.
  B. ``AccessPolicyService.default_role`` — the clamp that keeps a stored value
     which has drifted out of range (a hand-edited row, a role a later release
     removed) from ever becoming ``admin`` for a non-superuser.
  C. ``AccessPolicy.registration_open`` — an unrecognised mode degrades to
     closed, not open.
  D. ``AccessPolicyService.is_email_allowed`` — the one place the shared
     matcher's fail-closed default is inverted, because an empty list here
     means "anyone", not "nobody".
  E. ``AccessPolicyService._first_invalid_pattern`` — what the admin endpoint
     calls malformed, and why a bare domain counts.
  F. ``Settings`` validation — ``admin`` still cannot be *seeded* as the
     default role.
  G. ``AccessPolicyService.normalize_email_patterns`` — the single answer to
     "is this pattern list empty?", which three readers would otherwise give
     three answers to.
  H. ``AccessPolicyService.validate_update`` — the break-glass half of the
     Google-only lockout rule. Driven with a stub session because the state it
     refuses (no administrator holds a password hash) cannot be reached
     through the API: the acting admin is always an active superuser, and no
     endpoint removes a password from an account.

Cross-reference: the API-observable behaviour of all of this — signup, admin
user creation, and the Google first-login all picking up the configured role —
lives in ``tests/api/users/users_default_role_test.py``, and the admin
endpoint's own validation of the value in
``tests/api/server_config/access_policy_test.py``.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.models.server_config.server_config import ServerConfig, ServerConfigUpdate
from app.models.users.user import User
from app.services.users.access_policy_service import (
    REASON_GOOGLE_OAUTH_NOT_CONFIGURED,
    REASON_NO_ADMIN_GOOGLE_ACCOUNT,
    REASON_NO_ADMIN_PASSWORD,
    AccessPolicy,
    AccessPolicyService,
    AccessPolicyValidationError,
)
from app.services.users.role_service import RoleService


def _policy(**overrides) -> AccessPolicy:
    """An ``AccessPolicy`` with the shipped defaults, minus any override."""
    defaults = {
        "registration_mode": "open",
        "allowed_email_patterns": "",
        "password_auth_enabled": True,
        "google_auto_register": True,
        "default_user_role": "agent-user",
        "invite_include_desktop_default": True,
        "google_auth_enabled": True,
    }
    return AccessPolicy(**{**defaults, **overrides})


# ── A. derive_default_role — the superuser branch ───────────────────────────


class TestDeriveDefaultRole:
    """``RoleService.derive_default_role`` — the half that needs no session."""

    def test_superuser_returns_admin_without_reading_the_policy(self) -> None:
        """Superusers map to ``admin`` before the policy is consulted at all.

        ``session=None`` is the assertion: if this branch ever started
        resolving the policy first, the call would raise instead of returning.
        A superuser's role must not depend on a readable ``server_config`` row,
        because the first superuser is created before one exists.
        """
        assert RoleService.derive_default_role(session=None, is_superuser=True) == "admin"


# ── B. default_role — the out-of-range clamp ────────────────────────────────


class TestAccessPolicyDefaultRole:
    """``AccessPolicyService.default_role`` clamps what it hands out.

    The admin endpoint validates the value on the way in, so the only way an
    out-of-range role reaches storage is around that endpoint — a hand-edited
    row, a restored dump, a role a later release removed. This is the guard
    that keeps such a value from being handed to a new account.
    """

    @pytest.mark.parametrize(
        "stored, expected",
        [
            ("agent-user", "agent-user"),
            ("agent-developer", "agent-developer"),
            # The one that matters: never ``admin`` for a non-superuser.
            ("admin", "agent-user"),
            ("superadmin", "agent-user"),
            ("", "agent-user"),
        ],
    )
    def test_stored_role_is_clamped_to_an_assignable_one(
        self, monkeypatch, stored: str, expected: str
    ) -> None:
        monkeypatch.setattr(
            AccessPolicyService,
            "resolve",
            staticmethod(lambda session: _policy(default_user_role=stored)),
        )
        assert AccessPolicyService.default_role(session=None) == expected


# ── C. registration_open — unknown modes fail closed ────────────────────────


class TestRegistrationOpen:
    def test_only_the_literal_open_mode_opens_registration(self) -> None:
        assert _policy(registration_mode="open").registration_open is True

    @pytest.mark.parametrize("mode", ["invite_only", "closed", "OPEN", "", "open "])
    def test_anything_else_is_closed(self, mode: str) -> None:
        """A value nobody has reasoned about must not become an open door.

        Newer deployments, hand-edited rows and typos all land here, and the
        permissive branch is the one that has to be narrow.
        """
        assert _policy(registration_mode=mode).registration_open is False


# ── D. is_email_allowed — empty means everyone ──────────────────────────────


class TestIsEmailAllowed:
    @pytest.mark.parametrize("patterns", ["", "   ", "\n\t"])
    def test_an_empty_list_admits_everyone(self, patterns: str) -> None:
        """The inverted default: ``match_email_pattern`` alone admits nobody.

        This list gates self-registration, where empty is the
        backward-compatible "anyone may sign up" the platform shipped with —
        the opposite of a channel's sender allowlist.
        """
        assert AccessPolicyService.is_email_allowed(
            _policy(allowed_email_patterns=patterns), "someone@anywhere.test"
        ) is True

    def test_a_configured_list_admits_only_matches(self) -> None:
        policy = _policy(allowed_email_patterns="*@acme.com, *@*.acme.com")
        assert AccessPolicyService.is_email_allowed(policy, "ann@acme.com") is True
        assert AccessPolicyService.is_email_allowed(policy, "bo@eu.acme.com") is True
        assert AccessPolicyService.is_email_allowed(policy, "cy@notacme.com") is False

    def test_a_bare_wildcard_admits_everyone(self) -> None:
        assert AccessPolicyService.is_email_allowed(
            _policy(allowed_email_patterns="*"), "someone@anywhere.test"
        ) is True


# ── E. _first_invalid_pattern — what "malformed" means ──────────────────────


class TestFirstInvalidPattern:
    """The admin endpoint's shape check for the pattern list.

    ``fnmatch`` compiles any string, so the matcher rejects nothing — the
    definition of malformed lives entirely in this helper, and the entry it
    names is what the ``invalid_email_pattern:<entry>`` detail carries.
    """

    @pytest.mark.parametrize(
        "patterns",
        ["", "*", "*@acme.com", "*@acme.com, *@*.acme.com", "*@acme.com,", " , *@a.io"],
    )
    def test_usable_lists_are_accepted(self, patterns: str) -> None:
        assert AccessPolicyService._first_invalid_pattern(patterns) is None

    def test_a_bare_domain_is_rejected_and_named(self) -> None:
        """``acme.com`` looks like it should work and matches nothing.

        Silently keeping it is the worst possible failure for a security
        control: the admin believes the door is narrowed and it is shut.
        """
        assert AccessPolicyService._first_invalid_pattern("acme.com") == "acme.com"

    def test_a_missing_comma_is_rejected_as_one_entry(self) -> None:
        assert (
            AccessPolicyService._first_invalid_pattern("*@acme.com *@corp.com")
            == "*@acme.com *@corp.com"
        )

    def test_the_first_offender_is_the_one_reported(self) -> None:
        assert (
            AccessPolicyService._first_invalid_pattern("*@ok.com, bad.com, worse com")
            == "bad.com"
        )


# ── F. Settings validation — the seed value is still constrained ────────────


class TestSettingsValidation:
    """``DEFAULT_USER_ROLE`` is seed-only now, but it still seeds a column.

    A value that could not be stored through the admin endpoint must not be
    able to arrive through the first-boot seed either, so the ``Literal``
    constraint still has to fail loudly at startup.
    """

    def _build_minimal_settings_kwargs(self) -> dict:
        """Minimal required fields for a ``Settings`` instance.

        Taken from the live object so the test does not break when a new
        required field is added.
        """
        from app.core.config import settings as live_settings

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
        """``admin`` is not a seedable default — the role ⇔ is_superuser rule."""
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
        from app.core.config import Settings

        kwargs = self._build_minimal_settings_kwargs()
        s = Settings(**kwargs, DEFAULT_USER_ROLE="agent-user")
        assert s.DEFAULT_USER_ROLE == "agent-user"

    def test_valid_agent_developer_value_accepted(self) -> None:
        from app.core.config import Settings

        kwargs = self._build_minimal_settings_kwargs()
        s = Settings(**kwargs, DEFAULT_USER_ROLE="agent-developer")
        assert s.DEFAULT_USER_ROLE == "agent-developer"


# ── G. normalize_email_patterns — "empty" decided once ──────────────────────


class TestNormalizeEmailPatterns:
    """The canonical form of a comma-separated glob list.

    The reason this exists is the third case below. ``" , "`` is non-empty to a
    reader that tests truthiness and empty to one that splits and drops blanks:
    ``is_email_allowed`` took the first view and handed the matcher a list with
    no usable entries — refusing **everyone** — while the admin card took the
    second and rendered "anyone can register". An admin who cleared the
    textarea imperfectly got a silently closed instance the UI called open.
    """

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("", ""),
            (None, ""),
            ("   ", ""),
            (",", ""),
            (" , ", ""),
            ("*@acme.com", "*@acme.com"),
            ("*@acme.com,", "*@acme.com"),
            ("  *@acme.com  ,  *@corp.com ", "*@acme.com, *@corp.com"),
            ("*@acme.com,,*@corp.com", "*@acme.com, *@corp.com"),
        ],
    )
    def test_canonical_form(self, stored: str | None, expected: str) -> None:
        assert AccessPolicyService.normalize_email_patterns(stored) == expected

    def test_a_separator_only_list_means_no_restriction(self) -> None:
        """The end-to-end consequence, asserted on the reader that was wrong.

        Not a restatement of the case above: this is the predicate whose answer
        actually decides whether anyone may sign up.
        """
        normalized = AccessPolicyService.normalize_email_patterns(" , ")
        policy = _policy(allowed_email_patterns=normalized)
        assert AccessPolicyService.is_email_allowed(policy, "anyone@example.com")


# ── H. validate_update — the password break-glass must exist ────────────────


class _FirstOnly:
    """The sliver of ``session.exec(...)`` that ``validate_update`` uses."""

    def __init__(self, row: User | None) -> None:
        self._row = row

    def first(self) -> User | None:
        return self._row


class _StubSession:
    """Answers the two admin-capability probes and refuses anything else.

    Both probes are ``select(User).where(...)`` over the same table, differing
    only in the final predicate, so the stub routes on the **rendered SQL**
    rather than on call order. An order-based stub would hand each check the
    other's answer if the two were ever swapped, and the test would stay green
    while asserting the wrong rule. Any third query raises, so a probe added
    later cannot silently take an answer meant for one of these two.
    """

    def __init__(
        self,
        *,
        google_linked_admin: User | None,
        password_capable_admin: User | None,
    ) -> None:
        self._google_linked_admin = google_linked_admin
        self._password_capable_admin = password_capable_admin

    def exec(self, statement: object) -> _FirstOnly:
        sql = str(statement)
        if "hashed_password" in sql:
            return _FirstOnly(self._password_capable_admin)
        if "google_id" in sql:
            return _FirstOnly(self._google_linked_admin)
        raise AssertionError(f"Unexpected query in validate_update: {sql}")


def _admin(*, google_id: str | None = None, password: str | None = None) -> User:
    return User(
        email="admin@example.com",
        is_superuser=True,
        is_active=True,
        google_id=google_id,
        hashed_password=password,
    )


@pytest.fixture
def _google_oauth_configured(monkeypatch):
    """Get past the first lockout rule so the later ones are reachable."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret")


def _turn_password_auth_off(
    session: _StubSession, acting_user: User, *, currently_on: bool = True
) -> None:
    AccessPolicyService.validate_update(
        session,  # type: ignore[arg-type]
        ServerConfig(password_auth_enabled=currently_on),
        ServerConfigUpdate(password_auth_enabled=False),
        acting_user,
    )


class TestGoogleOnlyLockoutRules:
    """Turning password sign-in off needs *both* doors proven open.

    Superusers keep password sign-in when the switch is off — that break-glass
    is the stated reason the rest of the rule can afford to be strict. But
    ``is_password_auth_allowed`` returning True for a superuser is vacuous when
    that superuser has no password hash, which is exactly what
    ``create_user_from_google`` leaves behind. Without the second probe the
    sole Google-provisioned admin could throw the switch and be locked out for
    good the day the Google client secret rotated.
    """

    def test_refused_when_no_administrator_holds_a_password(
        self, _google_oauth_configured
    ) -> None:
        session = _StubSession(
            google_linked_admin=_admin(google_id="g-1"),
            password_capable_admin=None,
        )
        with pytest.raises(AccessPolicyValidationError) as exc:
            _turn_password_auth_off(session, _admin(google_id="g-1"))
        assert exc.value.reason == REASON_NO_ADMIN_PASSWORD
        # The remedy has to be nameable, not just implied by the code.
        assert "password" in exc.value.message.lower()

    def test_allowed_when_both_doors_are_proven(
        self, _google_oauth_configured
    ) -> None:
        session = _StubSession(
            google_linked_admin=_admin(google_id="g-1"),
            password_capable_admin=_admin(password="hashed"),
        )
        _turn_password_auth_off(session, _admin(google_id="g-1"))

    def test_the_google_rule_is_reported_first(
        self, _google_oauth_configured
    ) -> None:
        """Both rules failing must name the Google one.

        Order matters to the admin: linking Google is the step that makes the
        switch meaningful at all, and reporting the password remedy first would
        send them to fix the survivable half of a state they cannot enter.
        """
        session = _StubSession(
            google_linked_admin=None, password_capable_admin=None
        )
        with pytest.raises(AccessPolicyValidationError) as exc:
            _turn_password_auth_off(session, _admin(password="hashed"))
        assert exc.value.reason == REASON_NO_ADMIN_GOOGLE_ACCOUNT

    def test_an_unconfigured_google_is_reported_before_either_probe(
        self, monkeypatch
    ) -> None:
        """No OAuth at all is answered without querying for admins.

        The stub raises on any query, so this also pins that the cheapest,
        most-actionable refusal short-circuits the two database reads.

        The settings are cleared explicitly: the container the suite runs in
        *does* carry Google credentials, so a test that merely omits the
        ``_google_oauth_configured`` fixture asserts nothing about this branch.
        """
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", None)
        session = _StubSession(google_linked_admin=None, password_capable_admin=None)
        with pytest.raises(AccessPolicyValidationError) as exc:
            _turn_password_auth_off(session, _admin(google_id="g-1"))
        assert exc.value.reason == REASON_GOOGLE_OAUTH_NOT_CONFIGURED

    def test_an_instance_already_in_google_only_mode_is_not_re_checked(
        self, _google_oauth_configured
    ) -> None:
        """The rule guards the transition, not the state.

        Asserted by the stub raising on any query: an instance whose password
        auth is already off must stay editable without either probe running,
        so a later config edit is never refused for a state the admin is not
        entering.
        """
        session = _StubSession(google_linked_admin=None, password_capable_admin=None)
        _turn_password_auth_off(
            session, _admin(google_id="g-1"), currently_on=False
        )
