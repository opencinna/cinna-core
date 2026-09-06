"""What the access policy does to the front door.

Four switches on the ``server_config`` singleton gate account creation and the
password paths. This file drives them **through the admin API** — the policy is
a database row, and patching a module attribute would only prove the mock
works.

The invariants worth stating out loud, because each one is a way a well-meaning
implementation goes wrong:

* **No enumeration.** A policy refusal returns the same status and the same
  body whether or not the address already has an account. The "already
  registered" 400 survives only where the policy said yes first — otherwise a
  closed instance becomes an address oracle for anyone who can reach ``/signup``.
* **Registration gates, login does not.** Flipping to invite-only or narrowing
  the email patterns stops new accounts; it never evicts or locks out someone
  who is already in. An admin editing a domain list assumes exactly this.
* **The superuser break-glass is real.** With password sign-in off, an
  administrator can still use it — otherwise a broken Google configuration
  would lock every administrator out of their own instance, and that is the
  reason the admin surface can afford to be strict about the switch.

The admin surface and its validation live in
``tests/api/server_config/access_policy_test.py``.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.utils import generate_password_reset_token
from tests.utils.google_oauth import (
    google_callback,
    login_with_google,
    random_google_id,
)
from tests.utils.server_config import set_access_policy
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR


# ── Helpers ────────────────────────────────────────────────────────────


def _signup(client: TestClient, email: str, password: str):
    """Raw signup response — every caller here asserts on its status."""
    return client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )


def _create_user(client: TestClient) -> tuple[str, str]:
    """Create a user through public signup; returns ``(email, password)``."""
    email, password = random_email(), random_lower_string()
    response = _signup(client, email, password)
    assert response.status_code == 200, response.text
    return email, password


def _login(client: TestClient, email: str, password: str):
    return client.post(
        f"{API}/login/access-token", data={"username": email, "password": password}
    )


def _disable_password_auth(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Put the instance into Google-only mode.

    The admin endpoint refuses the switch unless Google OAuth is configured
    *and* some administrator can actually use it, so both preconditions are
    established here the way an operator would: a Google sign-in on the
    superuser's address auto-links it.
    """
    login_with_google(
        client, email=settings.FIRST_SUPERUSER, google_id=random_google_id()
    )
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
    ):
        set_access_policy(
            client, superuser_token_headers, password_auth_enabled=False
        )


# ── Registration mode ──────────────────────────────────────────────────


def test_invite_only_stops_new_signups_without_touching_existing_users(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Invite-only, end to end:
      1. Signup works while registration is open
      2. Admin closes registration
      3. A new address is refused with ``registration_closed``
      4. A *known* address is refused identically — no enumeration
      5. The existing user still logs in and still uses their token
      6. Re-opening registration restores signup
    """
    # ── Phase 1: Open instance ────────────────────────────────────────
    known_email, known_password = _create_user(client)

    # ── Phase 2: Close it ─────────────────────────────────────────────
    set_access_policy(
        client, superuser_token_headers, registration_mode="invite_only"
    )

    # ── Phase 3: A stranger is refused ────────────────────────────────
    stranger = _signup(client, random_email(), random_lower_string())
    assert stranger.status_code == 403, stranger.text
    assert stranger.json()["detail"] == "registration_closed"

    # ── Phase 4: A known address is refused the same way ──────────────
    # Not 400 "already exists": that answer would tell an anonymous caller
    # which addresses have accounts here.
    known = _signup(client, known_email, random_lower_string())
    assert known.status_code == stranger.status_code
    assert known.json() == stranger.json()

    # ── Phase 5: The mode gates registration, not sign-in ─────────────
    login = _login(client, known_email, known_password)
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get(f"{API}/users/me", headers=headers).status_code == 200

    # ── Phase 6: Re-opening restores signup ───────────────────────────
    set_access_policy(client, superuser_token_headers, registration_mode="open")
    assert _signup(client, random_email(), random_lower_string()).status_code == 200


def test_email_patterns_gate_registration_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The allowed-email list:
      1. A user signs up before any list exists
      2. Admin adds a list that does not cover that user
      3. A non-matching address is refused with ``email_not_allowed``
      4. The already-registered non-matching address is refused identically
      5. A matching address may still sign up
      6. Signing up twice on a matching address gives the ordinary duplicate
         400 — the enumeration-safe answer is scoped to policy refusals
      7. The pre-existing user is not locked out of anything
      8. Clearing the list restores unrestricted signup
    """
    # ── Phase 1: No list yet ──────────────────────────────────────────
    outsider_email, outsider_password = _create_user(client)

    # ── Phase 2: Narrow the door ──────────────────────────────────────
    set_access_policy(
        client,
        superuser_token_headers,
        allowed_email_patterns="*@acme.com, *@*.acme.com",
    )

    # ── Phase 3: A stranger outside the list ──────────────────────────
    stranger = _signup(client, random_email(), random_lower_string())
    assert stranger.status_code == 403, stranger.text
    assert stranger.json()["detail"] == "email_not_allowed"

    # ── Phase 4: A known address outside the list, same answer ────────
    known = _signup(client, outsider_email, random_lower_string())
    assert known.status_code == stranger.status_code
    assert known.json() == stranger.json()

    # ── Phase 5: Matching addresses get in, including the subdomain glob
    insider_password = random_lower_string()
    insider_email = f"{random_lower_string()}@acme.com"
    assert _signup(client, insider_email, insider_password).status_code == 200
    assert (
        _signup(
            client, f"{random_lower_string()}@eu.acme.com", random_lower_string()
        ).status_code
        == 200
    )

    # ── Phase 6: Duplicates still answer 400, deliberately ────────────
    # The policy said yes for this address, so nothing is revealed that the
    # caller did not already establish by being allowed to register here.
    duplicate = _signup(client, insider_email, random_lower_string())
    assert duplicate.status_code == 400, duplicate.text
    assert duplicate.json()["detail"] != "email_not_allowed"

    # ── Phase 7: The outsider keeps their account ─────────────────────
    login = _login(client, outsider_email, outsider_password)
    assert login.status_code == 200, login.text
    outsider_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get(f"{API}/users/me", headers=outsider_headers).status_code == 200

    # ── Phase 8: Clearing the list re-opens signup ────────────────────
    set_access_policy(client, superuser_token_headers, allowed_email_patterns="")
    assert _signup(client, random_email(), random_lower_string()).status_code == 200


# ── Password sign-in ───────────────────────────────────────────────────


def test_password_auth_off_blocks_every_password_path_except_for_admins(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Google-only mode, across all five password surfaces:
      1. A user exists and can log in
      2. Admin turns password sign-in off
      3. Password login → 403; the superuser's login still succeeds
      4. Signup → 403 ``password_auth_disabled``
      5. Recovery answers generically but sends nothing — and still sends for
         the superuser
      6. Reset-password with a valid token → 403; superuser's token works
      7. Change-password and set-password → 403; the superuser may change theirs
    """
    # ── Phase 1: An ordinary user with a password ─────────────────────
    email, password = _create_user(client)
    # Their token is taken while they can still log in: the gate is on the
    # password paths, not on holding a session.
    headers = user_authentication_headers(client=client, email=email, password=password)

    # ── Phase 2: Google-only mode ─────────────────────────────────────
    _disable_password_auth(client, superuser_token_headers)

    # ── Phase 3: Login ────────────────────────────────────────────────
    blocked = _login(client, email, password)
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"] == "password_auth_disabled"

    # Break-glass: the administrator is never locked out of their instance.
    admin_login = _login(
        client, settings.FIRST_SUPERUSER, settings.FIRST_SUPERUSER_PASSWORD
    )
    assert admin_login.status_code == 200, admin_login.text

    # A wrong password is still a 400, not a 403 — the policy check runs
    # after authentication so it can never answer for an unknown address.
    assert _login(client, email, "wrong-password").status_code == 400
    assert _login(client, random_email(), random_lower_string()).status_code == 400

    # ── Phase 4: Signup ───────────────────────────────────────────────
    signup = _signup(client, random_email(), random_lower_string())
    assert signup.status_code == 403, signup.text
    assert signup.json()["detail"] == "password_auth_disabled"

    # ── Phase 5: Recovery is refused *silently* ───────────────────────
    # Raising here would identify which addresses belong to administrators,
    # since those keep the break-glass path.
    with (
        patch.object(settings, "SMTP_HOST", "smtp.example.com"),
        patch.object(settings, "SMTP_USER", "admin@example.com"),
        patch("app.services.users.user_service.send_email") as send_email,
    ):
        recovery = client.post(f"{API}/password-recovery/{email}")
        assert recovery.status_code == 200, recovery.text
        send_email.assert_not_called()

        admin_recovery = client.post(
            f"{API}/password-recovery/{settings.FIRST_SUPERUSER}"
        )
        assert admin_recovery.status_code == 200, admin_recovery.text
        assert send_email.call_count == 1

        # The two answers are byte-identical — compared to each other, not to
        # a literal message, so a re-wording cannot split them while both
        # halves keep matching their own copy of the string. The body used to
        # be asserted here as ``"Password recovery email sent"``; recovery is
        # now uniformly generic and the full contract lives in
        # ``password_recovery_enumeration_test.py``.
        assert (recovery.status_code, recovery.content) == (
            admin_recovery.status_code,
            admin_recovery.content,
        )

    # ── Phase 6: Reset with a genuinely valid token ───────────────────
    reset = client.post(
        f"{API}/reset-password/",
        json={
            "new_password": random_lower_string(),
            "token": generate_password_reset_token(email=email),
        },
    )
    assert reset.status_code == 403, reset.text
    assert reset.json()["detail"] == "password_auth_disabled"
    # The refusal is not a silent no-op: the old password would still work if
    # the user could log in at all, and the new one never became valid.

    admin_new_password = random_lower_string()
    admin_reset = client.post(
        f"{API}/reset-password/",
        json={
            "new_password": admin_new_password,
            "token": generate_password_reset_token(email=settings.FIRST_SUPERUSER),
        },
    )
    assert admin_reset.status_code == 200, admin_reset.text
    assert (
        _login(client, settings.FIRST_SUPERUSER, admin_new_password).status_code == 200
    )

    # ── Phase 7: The authenticated password endpoints ─────────────────
    change = client.patch(
        f"{API}/users/me/password",
        headers=headers,
        json={"current_password": password, "new_password": random_lower_string()},
    )
    assert change.status_code == 403, change.text
    assert change.json()["detail"] == "password_auth_disabled"

    # The gate runs before the endpoint's own "you already have a password"
    # rejection, so this answers 403 rather than 400.
    set_pw = client.post(
        f"{API}/users/me/set-password",
        headers=headers,
        json={"new_password": random_lower_string()},
    )
    assert set_pw.status_code == 403, set_pw.text
    assert set_pw.json()["detail"] == "password_auth_disabled"

    # The administrator may still rotate their own password.
    admin_change = client.patch(
        f"{API}/users/me/password",
        headers=superuser_token_headers,
        json={
            "current_password": admin_new_password,
            "new_password": random_lower_string(),
        },
    )
    assert admin_change.status_code == 200, admin_change.text


# ── Google registration ────────────────────────────────────────────────


def test_google_registers_only_on_an_open_instance_that_allows_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The Google callback as a registration path:
      1. Open + auto-register on → a first login creates the account
      2. auto-register off → ``google_auto_register_disabled``
      3. Invite-only → ``registration_closed``, whatever auto-register says
      4. Pattern miss → ``email_not_allowed``; a matching address gets in
      5. An *existing* account signs in through Google even on a closed
         instance — the callback is only gated where it would create a row
    """
    # ── Phase 1: The default open instance registers Google users ─────
    first_email = random_email()
    headers = login_with_google(
        client, email=first_email, google_id=random_google_id()
    )
    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == first_email

    # ── Phase 2: Auto-registration switched off ───────────────────────
    set_access_policy(client, superuser_token_headers, google_auto_register=False)
    refused = google_callback(
        client, email=random_email(), google_id=random_google_id()
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "google_auto_register_disabled"

    # ── Phase 3: Invite-only wins over auto-register ──────────────────
    set_access_policy(
        client,
        superuser_token_headers,
        registration_mode="invite_only",
        google_auto_register=True,
    )
    closed = google_callback(
        client, email=random_email(), google_id=random_google_id()
    )
    assert closed.status_code == 403, closed.text
    assert closed.json()["detail"] == "registration_closed"

    # ── Phase 4: Patterns apply to the Google path too ────────────────
    set_access_policy(
        client,
        superuser_token_headers,
        registration_mode="open",
        allowed_email_patterns="*@acme.com",
    )
    outside = google_callback(
        client, email=random_email(), google_id=random_google_id()
    )
    assert outside.status_code == 403, outside.text
    assert outside.json()["detail"] == "email_not_allowed"

    inside = google_callback(
        client, email=f"{random_lower_string()}@acme.com", google_id=random_google_id()
    )
    assert inside.status_code == 200, inside.text

    # ── Phase 5: Existing accounts sign in regardless of the policy ───
    # ``first_email`` was registered in phase 1 and matches no pattern. The
    # callback finds the existing account by address and links the identity to
    # it, so it never reaches the registration gate at all.
    set_access_policy(
        client,
        superuser_token_headers,
        registration_mode="invite_only",
        google_auto_register=False,
    )
    returning = login_with_google(
        client, email=first_email, google_id=random_google_id()
    )
    assert client.get(f"{API}/users/me", headers=returning).status_code == 200
