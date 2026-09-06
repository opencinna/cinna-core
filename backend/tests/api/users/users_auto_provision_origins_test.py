"""Every way an account can arrive hands it the company's AI keys.

Zero-touch onboarding phase 2 replaced three hand-rolled ``User(...)``
constructions with one chokepoint, ``UserService.create_account(origin=…)``,
and hung auto-provisioning off it. The value of a chokepoint is entirely in
"every path goes through it", so testing one path and assuming the rest is
testing the wrong thing — the bug this design prevents is precisely the path
somebody forgot. Each of the six ``AccountOrigin`` members therefore gets its
own scenario here:

  ``signup``    — ``POST /users/signup``
  ``google``    — the OAuth callback's first login for an unknown Google id
  ``admin``     — ``POST /users/`` (a superuser creating an account)
  ``external``  — a whitelisted unknown sender on a Server Channel webhook
  ``seed``      — the first-superuser bootstrap (see the note on that test)
  ``invite``    — phase 3; the origin exists but nothing constructs one yet

Two things are asserted for each, not one: that the child credential exists
(membership of the managed parent) *and* that the owner's SDK defaults were
wired — credential pointer, engine and per-mode model override. A grant that
lands but is not wired is invisible to the person it was granted to, which
from their side is the same as not having been granted.

The recorded ``origin`` is checked from the receiving account's own security
feed wherever the account can log in. That is the only place the distinction
between paths is observable after the fact, and it is what an admin looks at.

Notes:
  * ``tests/architecture/account_creation_chokepoint_test.py`` pins the
    structural half — that ``User(`` is built in exactly one place, so a
    seventh path cannot appear without joining this file.
  * The failure semantics of provisioning (an account is never lost to it) are
    in ``users_auto_provision_resilience_test.py``.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.ai_credential import list_ai_credentials
from tests.utils.account_provisioning import (
    AccountOrigin,
    create_account_via_service,
    get_user_row,
    provision_account,
)
from tests.utils.google_oauth import login_with_google, random_google_id
from tests.utils.managed_ai_credential import (
    create_managed_credential,
    get_managed_credential,
    member_for,
    member_user_ids,
)
from tests.utils.routing import post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
)
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR

# The Server-Channel scenario drives a real webhook, so the heavy agent/env
# stubs from ``tests/api/users/conftest.py`` stay on for this module.

CONVERSATION_MODEL = "claude-test-conversation-model"
BUILDING_MODEL = "claude-test-building-model"
# ``anthropic`` composes this engine string for both modes; the wiring is
# skipped for a mode whose engine is incompatible with the credential type, so
# asserting the value (not merely "not None") is what proves it ran.
ANTHROPIC_ENGINE = "claude-code/anthropic"


# ── Extra stubs this module needs beyond the users-domain conftest ──────


@pytest.fixture(autouse=True)
def _patch_anyio_to_thread():
    """Keep the channel pipeline's routing passes on the test thread.

    ``ChannelRoutingService.run_in_thread`` offloads via
    ``anyio.to_thread.run_sync``; unpatched, routing genuinely runs on another
    OS thread against this test's SQLAlchemy session, which is unsupported.
    Copied from ``tests/api/server_channels/conftest.py`` rather than moved
    into the users conftest, because this is the only file in the domain that
    reaches a channel. The keyword arguments are anyio's own thread controls
    and are dropped, never forwarded to ``func``.
    """

    async def _run_sync(func, *args, **kwargs):
        return func(*args)

    with patch("anyio.to_thread.run_sync", _run_sync):
        yield


# ── Local helpers ──────────────────────────────────────────────────────


def _company_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    roles: list[str] | None = None,
) -> dict:
    """The shape an admin configures once: auto-provision + wired defaults.

    Member list starts empty on purpose — that is the auto-provision-only
    record ``target_user_ids`` was made optional for, and it means every
    member observed later got there by arriving, not by being listed.
    """
    result = create_managed_credential(
        client,
        superuser_token_headers,
        name=f"Company Anthropic {random_lower_string()[:6]}",
        auto_provision_roles=roles if roles is not None else ["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
        model_override_conversation=CONVERSATION_MODEL,
        model_override_building=BUILDING_MODEL,
        set_as_default=True,
    )
    assert result["added"] == []
    assert result["record"]["auto_provision_roles"] == (
        roles if roles is not None else ["agent-user"]
    )
    return result["record"]


def _read_user(
    client: TestClient, superuser_token_headers: dict[str, str], user_id: str
) -> dict:
    response = client.get(f"{API}/users/{user_id}", headers=superuser_token_headers)
    assert response.status_code == 200, response.text
    return response.json()


def _find_user_id_by_email(
    client: TestClient, superuser_token_headers: dict[str, str], email: str
) -> str | None:
    """Locate an account by address through the admin listing.

    Needed for the passwordless external sender, who has no way to log in and
    ask ``/users/me`` who they are.
    """
    response = client.get(
        f"{API}/users/", headers=superuser_token_headers, params={"limit": 200}
    )
    assert response.status_code == 200, response.text
    for row in response.json()["data"]:
        if row["email"] == email.lower():
            return row["id"]
    return None


def _assert_provisioned_and_wired(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    parent_id: str,
    user_id: str,
) -> dict:
    """The full outcome of one auto-provisioned grant, asserted in one place.

    Membership *and* wiring, because either alone is a half-delivered feature:
    a child nobody's defaults point at is a credential the owner never sees,
    and a pointer with no child is impossible but would be worth knowing.
    """
    record = get_managed_credential(client, superuser_token_headers, parent_id)
    assert user_id in member_user_ids(record), (
        f"user {user_id} did not receive the auto-provisioned credential; "
        f"members were {member_user_ids(record)}"
    )
    member = member_for(record, user_id)
    assert member is not None
    child_id = member["child_credential_id"]
    assert member["is_default"] is True, "set_as_default did not reach the child"

    owner = _read_user(client, superuser_token_headers, user_id)
    assert owner["default_ai_credential_conversation_id"] == child_id
    assert owner["default_ai_credential_building_id"] == child_id
    assert owner["default_sdk_conversation"] == ANTHROPIC_ENGINE
    assert owner["default_sdk_building"] == ANTHROPIC_ENGINE
    assert owner["default_model_override_conversation"] == CONVERSATION_MODEL
    assert owner["default_model_override_building"] == BUILDING_MODEL
    return owner


def _auto_provision_events(client: TestClient, headers: dict[str, str]) -> list[dict]:
    """The receiving account's own audit rows for automatic grants."""
    response = client.get(
        f"{API}/security-events/",
        headers=headers,
        params={"event_type": "admin.ai_credential.auto_provision"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@contextmanager
def _admin_email_kept_off_the_wire():
    """Stop ``POST /users/``'s new-account email at the right seam.

    Both modules do ``from app.utils import send_email``, which binds the
    symbol at import time — so patching ``app.utils.send_email`` (as some
    older files in this suite still do) leaves the route's own reference
    untouched, a real SMTP connection is attempted, and the test passes
    anyway. That is memory ``project_test_users_send_email_patch_bug``, and it
    is why these two targets are the module-local names rather than the
    definition site.

    Note what is deliberately *not* done here: disabling outbound email
    wholesale by blanking ``settings.SMTP_HOST``. That is a *different*
    configuration rather than a quieter version of this one — with email off
    the route never calls ``send_confirmation_email``, and that call is what
    un-expires the ``User`` row after provisioning's commits, so turning it off
    changes what the route does after the account is written. It has its own
    test, ``test_account_creation_survives_provisioning_on_an_instance_without_smtp``
    in ``users_auto_provision_resilience_test.py``, where blanking ``SMTP_HOST``
    is the load-bearing part. These scenarios want the ordinary emails-enabled
    path with nothing but the send itself stubbed.
    """
    with (
        patch("app.api.routes.users.send_email", return_value=None),
        patch(
            "app.services.users.email_confirmation_service.send_email",
            return_value=None,
        ),
    ):
        yield


# ── Origin: signup ─────────────────────────────────────────────────────


def test_password_signup_receives_the_auto_provisioned_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The self-service path, end to end:
      1. Admin configures a company credential for ``agent-user`` accounts
      2. Somebody signs up with a password
      3. They are a member of the managed record, with a child credential
      4. Their profile defaults point at that child, in both modes, with the
         admin's per-mode model overrides
      5. Their own security feed records the grant, stamped ``origin=signup``
         and attributed to the system rather than to an admin
    """
    # ── Phase 1: Admin configures it once ──────────────────────────────
    parent = _company_credential(client, superuser_token_headers)

    # ── Phase 2: Somebody signs up ─────────────────────────────────────
    email = random_email()
    password = random_lower_string()
    response = client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    user = response.json()
    assert user["role"] == "agent-user"

    # ── Phase 3 + 4: Granted and wired ─────────────────────────────────
    _assert_provisioned_and_wired(
        client, superuser_token_headers, parent["id"], user["id"]
    )

    # ── Phase 5: Audited in the receiving account's own feed ───────────
    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    events = _auto_provision_events(client, headers)
    assert len(events) == 1, events
    details = events[0]["details"]
    assert details["origin"] == "signup"
    assert details["actor"] == "system"
    assert details["managed_credential_id"] == parent["id"]
    assert details["role"] == "agent-user"
    assert events[0]["severity"] == "low"
    # The audit row must not carry the key it is about.
    assert "api_key" not in str(details)


# ── Origin: google ─────────────────────────────────────────────────────


def test_google_first_login_receives_the_auto_provisioned_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The zero-touch path the feature is named for:
      1. Admin configures the company credential
      2. An unknown Google identity signs in for the first time
      3. The account created by the callback holds the child credential and
         has its defaults wired — "already has keys on first login"
      4. The grant is stamped ``origin=google``
    """
    parent = _company_credential(client, superuser_token_headers)

    email = random_email()
    headers = login_with_google(
        client, email=email, google_id=random_google_id()
    )

    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    user = me.json()
    assert user["role"] == "agent-user"
    assert user["has_google_account"] is True

    _assert_provisioned_and_wired(
        client, superuser_token_headers, parent["id"], user["id"]
    )

    events = _auto_provision_events(client, headers)
    assert len(events) == 1, events
    assert events[0]["details"]["origin"] == "google"
    assert events[0]["details"]["actor"] == "system"


# ── Origin: admin ──────────────────────────────────────────────────────


def test_admin_created_account_receives_the_auto_provisioned_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``POST /users/`` is a caller of the chokepoint like any other:
      1. Admin configures the company credential
      2. Admin creates an account by hand
      3. It is provisioned and wired exactly as a self-service signup is
      4. The grant is stamped ``origin=admin`` — the audit distinguishes the
         path even though the outcome is identical
    """
    parent = _company_credential(client, superuser_token_headers)

    email = random_email()
    password = random_lower_string()
    with _admin_email_kept_off_the_wire():
        response = client.post(
            f"{API}/users/",
            headers=superuser_token_headers,
            json={"email": email, "password": password},
        )
    assert response.status_code == 200, response.text
    user = response.json()
    assert user["role"] == "agent-user"

    _assert_provisioned_and_wired(
        client, superuser_token_headers, parent["id"], user["id"]
    )

    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    events = _auto_provision_events(client, headers)
    assert len(events) == 1, events
    assert events[0]["details"]["origin"] == "admin"


# ── Origin: external (Server Channel auto-registration) ────────────────


def test_external_channel_auto_registered_sender_is_provisioned(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A whitelisted stranger messages a Server Channel and gets an account:
      1. Admin configures the company credential
      2. Admin opens a Google Chat channel with auto-registration on
      3. An unknown address posts a message; the pipeline creates the account
      4. That account — passwordless, so it can never log in to ask for
         itself — is nevertheless a member with its defaults wired

    This is the path most likely to be forgotten by a future refactor: it
    creates accounts from inside a webhook, nowhere near the auth routes.
    """
    parent = _company_credential(client, superuser_token_headers)
    channel = create_server_channel(
        client,
        superuser_token_headers,
        auto_register_users=True,
        email_whitelist="*",
    )

    sender_email = random_email()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hello, is anybody there?",
        sender_email=sender_email,
    )
    response, _ = post_channel_message(
        client, channel, GoogleChatJWTSigner(), event
    )
    assert response.status_code == 200, response.text

    user_id = _find_user_id_by_email(client, superuser_token_headers, sender_email)
    assert user_id is not None, (
        "the whitelisted sender was not auto-registered — the rest of this "
        "test cannot mean anything"
    )

    owner = _assert_provisioned_and_wired(
        client, superuser_token_headers, parent["id"], user_id
    )
    # The account really is the passwordless external shape, so the assertion
    # above was made about the account this test claims to be about.
    assert owner["has_password"] is False
    assert owner["role"] == "agent-user"


# ── Origin: seed ───────────────────────────────────────────────────────


def test_seed_origin_provisions_the_bootstrapped_superuser(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``AccountOrigin.SEED`` — ``core.db.init_db``'s first-superuser bootstrap.

    Alone among the origins this has no HTTP surface: it runs once, on an
    instance with no admin and no policy row, and can never be replayed by a
    request. It is exercised through the documented Rule-1 exemption in
    ``tests/utils/account_provisioning.py``; that ``init_db`` is the caller
    passing this origin is pinned statically in
    ``tests/architecture/account_creation_chokepoint_test.py``.

      1. A company credential covering ``admin`` accounts exists
      2. An account is created at the chokepoint with ``origin=seed`` and
         ``is_superuser=True``
      3. The ``role ⇔ is_superuser`` invariant pins it to ``admin`` …
      4. … and provisioning follows the resolved role, not the requested one
    """
    parent = _company_credential(client, superuser_token_headers, roles=["admin"])

    seeded = create_account_via_service(
        db,
        email=random_email(),
        origin=AccountOrigin.SEED,
        password=random_lower_string(),
        is_superuser=True,
    )
    seeded_id = str(seeded.id)

    owner = _read_user(client, superuser_token_headers, seeded_id)
    assert owner["is_superuser"] is True
    assert owner["role"] == "admin"

    _assert_provisioned_and_wired(
        client, superuser_token_headers, parent["id"], seeded_id
    )


# ── Idempotency ────────────────────────────────────────────────────────


def test_auto_provisioning_an_existing_member_adds_nothing(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    Being a member already is the outcome, not a conflict:
      1. A signup is auto-provisioned (one member, one child)
      2. Provisioning runs again for the same account
      3. Nothing is added and nothing is reported as skipped — "already has
         it" is success, and reporting it would write a medium-severity
         failure event into an account whose state is exactly right
      4. Membership is still one child, not two

    Phase 3's invite wizard is the caller that makes this real: it applies an
    explicit credential list and then the chokepoint's auto-provisioning runs
    over the same parents.
    """
    parent = _company_credential(client, superuser_token_headers)

    email = random_email()
    password = random_lower_string()
    response = client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    user_id = response.json()["id"]

    before = get_managed_credential(client, superuser_token_headers, parent["id"])
    assert before["member_count"] == 1

    # ── Phase 2: Run it again over an account that already holds it ────
    report = provision_account(db, get_user_row(db, user_id))
    assert report.added == [], report
    assert report.skipped == [], report

    after = get_managed_credential(client, superuser_token_headers, parent["id"])
    assert after["member_count"] == 1
    assert member_user_ids(after) == {user_id}

    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    assert len(_auto_provision_events(client, headers)) == 1

    # And the owner holds one credential from this parent, not two.
    creds = list_ai_credentials(client, headers)
    managed = [c for c in creds["data"] if c.get("is_admin_managed")]
    assert len(managed) == 1, managed


# ── The control: an inactive account, on the path with no list ─────────


def test_an_inactive_account_on_the_automatic_path_reports_nothing_at_all(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The automatic half of the inactive short-circuit, which has no skips.

    The explicit path reports **one skip per requested credential** on an
    inactive account, so that an admin who ticked three boxes does not see the
    screen of an admin who ticked none (asserted in
    ``users_invitation_lifecycle_test.py``). This is the guard on the other
    side of that change: the automatic path is handed no ids at all, the
    candidate set is precisely the query the short-circuit exists to skip, and
    an empty report is the honest answer there.

    It is a regression guard rather than a feature: the skip list was added to
    the shared ``_provision``, so a version that names the automatic set would
    have to run the query on every deactivated signup and would report
    credentials nobody asked for.

      1. A managed credential auto-provisions to ``agent-user``
      2. An admin creates a **deactivated** account through ``POST /users/``,
         which is the real automatic path for it
      3. Nothing was granted — the record has no members
      4. ``on_account_created`` on that row reports empty ``added``, empty
         ``skipped``, and is not ``failed``
    """
    parent = _company_credential(client, superuser_token_headers)

    email = random_email()
    with _admin_email_kept_off_the_wire():
        created = client.post(
            f"{API}/users/",
            headers=superuser_token_headers,
            json={
                "email": email,
                "password": random_lower_string(),
                "is_active": False,
            },
        )
    assert created.status_code == 200, created.text
    assert created.json()["is_active"] is False
    user_id = created.json()["id"]

    record = get_managed_credential(client, superuser_token_headers, parent["id"])
    assert record["members"] == []
    assert record["member_count"] == 0

    report = provision_account(db, get_user_row(db, user_id))
    assert report.added == [], report
    assert report.skipped == [], (
        "the automatic path named credentials it was never handed — the "
        "inactive skip list belongs to the explicit path, which has one"
    )
    assert report.failed is False, report


# ── The control: a role the record does not cover gets nothing ─────────


def test_a_role_outside_auto_provision_roles_receives_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Keeps every test above honest.

    If auto-provisioning granted to everyone regardless of role, all five
    origin scenarios would still pass. This is the assertion that fails in
    that world: an ``agent-user`` signup against an ``admin``-only record must
    come away with nothing, and no failure event either — not being covered is
    not a failure.
    """
    parent = _company_credential(client, superuser_token_headers, roles=["admin"])

    email = random_email()
    password = random_lower_string()
    response = client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    user = response.json()
    assert user["role"] == "agent-user"

    record = get_managed_credential(client, superuser_token_headers, parent["id"])
    assert record["members"] == []
    assert record["member_count"] == 0

    owner = _read_user(client, superuser_token_headers, user["id"])
    assert owner["default_ai_credential_conversation_id"] is None
    assert owner["default_ai_credential_building_id"] is None

    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    assert _auto_provision_events(client, headers) == []
    failures = client.get(
        f"{API}/security-events/",
        headers=headers,
        params={"event_type": "admin.ai_credential.auto_provision_failed"},
    )
    assert failures.status_code == 200, failures.text
    assert failures.json()["data"] == []
