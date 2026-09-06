"""The access policy: the admin surface, its validation, and its public face.

Six columns on the ``server_config`` singleton decide who may get an account on
this instance and how they sign in. This file covers the two ends of that row:

* ``PUT /admin/server-config`` — the only writer, and the place lockout
  prevention has to live. A UI that greys out a control is a courtesy; the
  rule is only real if the API refuses.
* ``GET /server-config/access-policy`` — the anonymous projection the login and
  signup pages render themselves from. It says what the instance *offers* and
  never who may do what, so ``allowed_email_patterns`` and
  ``default_user_role`` must not appear in it.

What the policy actually *does* to signup, login, the password paths and the
Google callback is covered in ``tests/api/auth/access_policy_gating_test.py``;
``can_change_email`` is covered in
``tests/api/users/users_can_change_email_test.py``.

Error contract: ``detail`` is a bare reason code. The single composite is
``invalid_email_pattern:<offending entry>``, so readers compare
``detail.split(":", 1)[0]``.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.google_oauth import login_with_google, random_google_id
from tests.utils.server_config import (
    ACCESS_POLICY_URL,
    SERVER_CONFIG_URL,
    get_public_access_policy,
    get_server_config,
    set_access_policy,
)
from tests.utils.utils import random_email, random_lower_string

# The documented shape of the public projection. Asserted as an exact set:
# a field added here without a deliberate decision is a leak, and the two
# fields deliberately left out are the whole point of the projection.
PUBLIC_POLICY_FIELDS = {
    "registration_open",
    "password_auth_enabled",
    "google_auth_enabled",
    "google_auto_register",
    "desktop_enabled",
    "project_name",
    # Added deliberately in phase 4: the *answer* to "may someone self-register
    # with a password here?", so /login, /signup and /start stop recombining
    # ``registration_open`` and ``password_auth_enabled`` for themselves (they
    # had already drifted to two different answers). Derived from two fields
    # already projected here, so it discloses nothing new.
    #
    # ``landing_markdown`` is deliberately NOT here: it is content, not
    # front-door policy, and it is served by ``GET /server-config/landing`` so
    # every login page load does not download a landing page it never renders.
    "password_signup_available",
}


@pytest.fixture(autouse=True)
def _fresh_access_policy_limiter(monkeypatch):
    """Give each test in this file its own rate-limit bucket.

    ``_access_policy_limiter`` is module-global and process-local, and
    ``is_private_peer("testclient")`` is False, so every request the suite
    makes to this endpoint shares one bucket keyed on the test transport's
    peer name. Without this fixture the 429 boundary asserted below would
    depend on how many times earlier tests happened to read the projection.
    ``monkeypatch`` restores the original (unpolluted) limiter afterwards, so
    this file also never leaves a spent bucket behind for other files.
    """
    from app.api.routes import server_config
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(server_config, "_access_policy_limiter", RateLimiter())


def _link_google_to_the_superuser(client: TestClient) -> None:
    """Give the seeded superuser a Google identity.

    Turning password sign-in off is refused unless some administrator could
    still sign in with Google (the break-glass is deliberately not enough on
    its own). A Google login on a known address auto-links, so this is the
    API-level way to satisfy that precondition.
    """
    login_with_google(
        client, email=settings.FIRST_SUPERUSER, google_id=random_google_id()
    )


# ── Public projection ──────────────────────────────────────────────────


def test_public_projection_tracks_the_policy_and_withholds_the_private_fields(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The anonymous projection, end to end:
      1. Readable with no credentials at all, with exactly the documented keys
      2. Reports the shipped defaults (open registration, password sign-in on)
      3. Follows an admin edit
      4. Never carries the patterns or the default role, even after they are set
      5. Those private fields are still readable behind the superuser endpoint
    """
    # ── Phase 1: Anonymous read, exact field set ──────────────────────
    response = client.get(ACCESS_POLICY_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == PUBLIC_POLICY_FIELDS

    # ── Phase 2: Defaults — a fresh instance is open to password signup ─
    assert body["registration_open"] is True
    assert body["password_auth_enabled"] is True
    assert body["google_auto_register"] is True
    # Deployment-derived, not admin-set: assert the source, not a constant,
    # so the file passes on an instance configured either way.
    assert body["google_auth_enabled"] == settings.google_oauth_enabled
    assert body["desktop_enabled"] == settings.DESKTOP_AUTH_ENABLED
    assert body["project_name"] == settings.PROJECT_NAME

    # ── Phase 3: An admin edit is visible to anonymous readers ────────
    set_access_policy(
        client,
        superuser_token_headers,
        registration_mode="invite_only",
        google_auto_register=False,
        allowed_email_patterns="*@acme.com, *@*.acme.com",
        default_user_role="agent-developer",
    )

    updated = get_public_access_policy(client)
    assert updated["registration_open"] is False
    assert updated["google_auto_register"] is False

    # ── Phase 4: The private fields never appear ──────────────────────
    assert set(updated) == PUBLIC_POLICY_FIELDS
    assert "allowed_email_patterns" not in updated
    assert "default_user_role" not in updated
    # Nor do their values leak under some other key.
    assert "acme.com" not in client.get(ACCESS_POLICY_URL).text

    # ── Phase 5: They are readable behind the superuser endpoint ──────
    admin_view = get_server_config(client, superuser_token_headers)
    assert admin_view["allowed_email_patterns"] == "*@acme.com, *@*.acme.com"
    assert admin_view["default_user_role"] == "agent-developer"
    assert admin_view["registration_mode"] == "invite_only"


def test_password_signup_available_matches_the_real_signup_outcome_across_the_matrix(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``password_signup_available`` is the single answer `/login`, `/signup` and
    `/start` are meant to render on, instead of each recombining
    ``registration_open`` and ``password_auth_enabled`` for itself (the two
    pages had already drifted to different answers — see the field's own
    docstring on ``AccessPolicyPublic``). Proven against the one thing that
    cannot lie about it: whether ``POST /users/signup`` actually lets someone
    in, across the full (``registration_mode``, ``password_auth_enabled``)
    matrix.

    Row 3 is the live bug a reviewer caught: an invite-only instance that
    still accepts passwords. ``password_signup_available`` is False there for
    the same reason it is False when password auth itself is off (self-serve
    registration is closed either way) — but those are not the same instance,
    and must stay distinguishable. They are: through ``password_auth_enabled``
    itself, which stays True on the invite-only row (an invited user can still
    set a password) and is False only when the switch is actually off. A
    ``/start`` or `/login` that only read ``password_signup_available`` would
    correctly hide the signup button in both rows without conflating them,
    because it never has to ask the question the collapsed field would
    answer wrong.
    """
    # A Google-linked administrator, established once, so every row below
    # that turns password auth off satisfies the lockout precondition.
    _link_google_to_the_superuser(client)

    # (registration_mode, password_auth_enabled, password_signup_available)
    matrix = [
        ("open", True, True),
        ("open", False, False),
        ("invite_only", True, False),
        ("invite_only", False, False),
    ]

    for registration_mode, password_auth_enabled, expected_available in matrix:
        if password_auth_enabled:
            set_access_policy(
                client,
                superuser_token_headers,
                registration_mode=registration_mode,
                password_auth_enabled=True,
            )
        else:
            # Disabling password auth is refused unless Google OAuth is
            # configured for the request that flips the switch.
            with (
                patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
                patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
            ):
                set_access_policy(
                    client,
                    superuser_token_headers,
                    registration_mode=registration_mode,
                    password_auth_enabled=False,
                )

        policy = get_public_access_policy(client)
        case = (registration_mode, password_auth_enabled)
        assert policy["password_signup_available"] is expected_available, case
        # The two rows where the derived field agrees (both False) are still
        # told apart by the field it is derived from.
        assert policy["password_auth_enabled"] is password_auth_enabled, case

        signup = client.post(
            f"{settings.API_V1_STR}/users/signup",
            json={"email": random_email(), "password": random_lower_string()},
        )
        if expected_available:
            assert signup.status_code == 200, (case, signup.text)
        else:
            assert signup.status_code == 403, (case, signup.text)
            assert signup.json()["detail"] in (
                "registration_closed",
                "password_auth_disabled",
            ), (case, signup.text)


def test_access_policy_reads_are_rate_limited_per_caller(
    client: TestClient, monkeypatch
) -> None:
    """The anonymous, database-touching reads here have a backstop.

    The budget is lowered rather than the configured default exhausted: the
    assertion is about the boundary existing, not about its size — and the
    default moves whenever another public endpoint joins the shared limiter.
    """
    monkeypatch.setattr(settings, "ACCESS_POLICY_RATE_LIMIT_PER_MIN", 3)

    for _ in range(3):
        assert client.get(ACCESS_POLICY_URL).status_code == 200

    throttled = client.get(ACCESS_POLICY_URL)
    assert throttled.status_code == 429, throttled.text
    assert throttled.json()["detail"] == "Rate limit exceeded"
    # Clients need to know when to come back; without this the only strategy
    # left is to keep hammering.
    assert int(throttled.headers["Retry-After"]) >= 0


# ── Admin writes and validation ────────────────────────────────────────


def test_valid_policy_edits_are_applied_and_survive_unrelated_updates(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The happy path for the admin surface:
      1. Every access-policy field can be set in one call
      2. The values round-trip through a fresh GET
      3. A bare ``*`` pattern is accepted and means "everyone"
      4. An unrelated (disclaimer) edit leaves the policy alone
      5. A policy edit never bumps ``disclaimer_version``
    """
    before = get_server_config(client, superuser_token_headers)

    # ── Phase 1: Set all six in one payload ───────────────────────────
    updated = set_access_policy(
        client,
        superuser_token_headers,
        registration_mode="invite_only",
        allowed_email_patterns="*@acme.com",
        google_auto_register=False,
        default_user_role="agent-developer",
        invite_include_desktop_default=False,
    )
    assert updated["registration_mode"] == "invite_only"
    assert updated["allowed_email_patterns"] == "*@acme.com"
    assert updated["google_auto_register"] is False
    assert updated["default_user_role"] == "agent-developer"
    assert updated["invite_include_desktop_default"] is False

    # ── Phase 2: Values persist ───────────────────────────────────────
    fetched = get_server_config(client, superuser_token_headers)
    assert fetched["registration_mode"] == "invite_only"
    assert fetched["default_user_role"] == "agent-developer"

    # ── Phase 3: "*" is a legitimate pattern meaning no restriction ───
    assert (
        set_access_policy(
            client, superuser_token_headers, allowed_email_patterns="*"
        )["allowed_email_patterns"]
        == "*"
    )

    # ── Phase 4: An unrelated edit does not reset the policy ──────────
    after_disclaimer = set_access_policy(
        client,
        superuser_token_headers,
        disclaimer_markdown="# Terms\n\nUse responsibly.",
    )
    assert after_disclaimer["registration_mode"] == "invite_only"
    assert after_disclaimer["default_user_role"] == "agent-developer"

    # ── Phase 5: Policy edits are not disclaimer content ──────────────
    # A version bump forces every user of the instance to re-acknowledge the
    # disclaimer; flipping a registration switch must never do that. (The
    # disclaimer edit in phase 4 is what makes this a +1 rather than a +0.)
    assert (
        after_disclaimer["disclaimer_version"] == before["disclaimer_version"] + 1
    )
    policy_only = set_access_policy(
        client, superuser_token_headers, registration_mode="open"
    )
    assert (
        policy_only["disclaimer_version"] == after_disclaimer["disclaimer_version"]
    )


def test_malformed_policy_values_are_rejected_with_their_reason_code(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Shape validation, and that a rejection applies nothing:
      1. An unknown registration mode
      2. ``admin`` as the default role (the role ⇔ is_superuser invariant)
      3. A domain with no ``@`` — looks right, matches nobody
      4. A pattern list with a missing comma
      5. The offending entry is named in the detail
      6. Nothing from a rejected payload reaches the row
    """
    before = get_server_config(client, superuser_token_headers)

    def _reject(payload: dict) -> str:
        response = client.put(
            SERVER_CONFIG_URL, headers=superuser_token_headers, json=payload
        )
        assert response.status_code == 400, response.text
        return response.json()["detail"]

    # ── Phase 1: Unknown mode ─────────────────────────────────────────
    assert _reject({"registration_mode": "closed"}) == "invalid_registration_mode"

    # ── Phase 2: admin is never a self-service default ────────────────
    assert _reject({"default_user_role": "admin"}) == "invalid_default_user_role"
    assert _reject({"default_user_role": "wizard"}) == "invalid_default_user_role"

    # ── Phase 3 & 4: Patterns that could never match a real address ───
    bare_domain = _reject({"allowed_email_patterns": "acme.com"})
    missing_comma = _reject({"allowed_email_patterns": "*@acme.com *@corp.com"})

    # ── Phase 5: The code is bare; only this one carries an argument ──
    assert bare_domain.split(":", 1)[0] == "invalid_email_pattern"
    assert bare_domain.split(":", 1)[1] == "acme.com"
    assert missing_comma.split(":", 1)[0] == "invalid_email_pattern"

    # ── Phase 6: A rejected update is a no-op, not a partial write ────
    # The payload below carries one good field and one bad one; the good one
    # must not land, or an admin fixing a typo would be applying half of a
    # change they never saw succeed.
    assert (
        _reject(
            {"registration_mode": "invite_only", "default_user_role": "admin"}
        )
        == "invalid_default_user_role"
    )
    after = get_server_config(client, superuser_token_headers)
    assert after["registration_mode"] == before["registration_mode"]
    assert after["default_user_role"] == before["default_user_role"]
    assert after["allowed_email_patterns"] == before["allowed_email_patterns"]

    # A valid list with blank entries (a trailing comma) is still accepted —
    # the rejection above is about entries that cannot match, not tidiness —
    # and it is stored canonicalised. Blank entries must not survive the write:
    # a string of nothing but separators is non-empty to a reader that only
    # tests truthiness and empty to one that splits, and those two readers
    # disagreeing is a silently closed instance the admin page calls open.
    assert (
        set_access_policy(
            client, superuser_token_headers, allowed_email_patterns="*@acme.com,"
        )["allowed_email_patterns"]
        == "*@acme.com"
    )

    # The degenerate case the canonicalisation exists for: a textarea cleared
    # down to its separators means "no restriction", and is stored as such.
    assert (
        set_access_policy(
            client, superuser_token_headers, allowed_email_patterns=" , "
        )["allowed_email_patterns"]
        == ""
    )


def test_turning_off_password_auth_is_refused_until_an_admin_can_use_google(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Lockout prevention on the one switch that can close the front door:
      1. Google OAuth not configured at all → refused
      2. Configured, but no administrator has a Google identity → refused
      3. Password sign-in is still on after both refusals
      4. With a Google-linked administrator → accepted
      5. The public projection now advertises Google-only sign-in
    """
    # ── Phase 1: No Google OAuth on this deployment ───────────────────
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", None),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", None),
    ):
        response = client.put(
            SERVER_CONFIG_URL,
            headers=superuser_token_headers,
            json={"password_auth_enabled": False},
        )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "google_oauth_not_configured"

    # ── Phase 2: Configured, but nobody with the admin bit can use it ─
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
    ):
        response = client.put(
            SERVER_CONFIG_URL,
            headers=superuser_token_headers,
            json={"password_auth_enabled": False},
        )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "no_admin_google_account"

    # ── Phase 3: Neither refusal changed anything ─────────────────────
    assert get_public_access_policy(client)["password_auth_enabled"] is True

    # ── Phase 4: Link Google to an administrator, then it is allowed ──
    _link_google_to_the_superuser(client)
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
    ):
        accepted = client.put(
            SERVER_CONFIG_URL,
            headers=superuser_token_headers,
            json={"password_auth_enabled": False},
        )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["password_auth_enabled"] is False

    # ── Phase 5: The login page is told to render Google only ─────────
    assert get_public_access_policy(client)["password_auth_enabled"] is False


def test_an_instance_already_in_google_only_mode_stays_editable(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The lockout rule guards the *transition*, not the state.

    Once password sign-in is off, an unrelated policy edit must still go
    through even if Google configuration later disappears — administrators
    keep the password break-glass, so the state is survivable, and refusing
    every future edit would not be.
    """
    _link_google_to_the_superuser(client)
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
    ):
        set_access_policy(client, superuser_token_headers, password_auth_enabled=False)

    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", None),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", None),
    ):
        # Editing something else while the switch stays off.
        still_ok = client.put(
            SERVER_CONFIG_URL,
            headers=superuser_token_headers,
            json={"registration_mode": "invite_only", "password_auth_enabled": False},
        )
    assert still_ok.status_code == 200, still_ok.text
    assert still_ok.json()["registration_mode"] == "invite_only"
    assert still_ok.json()["password_auth_enabled"] is False


def test_the_policy_is_an_admin_surface(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """A signed-in ordinary user may read the public projection and nothing more."""
    # The public projection is public — a token neither helps nor hurts.
    assert client.get(ACCESS_POLICY_URL, headers=normal_user_token_headers).status_code == 200

    assert (
        client.put(
            SERVER_CONFIG_URL,
            headers=normal_user_token_headers,
            json={"registration_mode": "invite_only"},
        ).status_code
        == 403
    )
    assert client.get(SERVER_CONFIG_URL, headers=normal_user_token_headers).status_code == 403
    # Unauthenticated writes are rejected too.
    assert (
        client.put(SERVER_CONFIG_URL, json={"registration_mode": "invite_only"}).status_code
        in (401, 403)
    )
