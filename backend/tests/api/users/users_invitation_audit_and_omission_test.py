"""Who the invite is recorded against, and what a re-invite is allowed to write.

TWO FIXES, ONE THEME: AN ADMINISTRATIVE ACT ON SOMEONE ELSE'S BEHALF
--------------------------------------------------------------------
``POST /users/invite`` acts *on* one account while being performed *by*
another, and both halves of the feature that got this wrong got it wrong the
same way — by letting the acting admin's submission speak for the invitee.

**The audit split.** One invite is two facts: "an account was created for you"
and "you created an account for someone". Writing ``user.invitation.created``
into both feeds records one fact twice, and an admin who onboards fifty people
then has fifty rows in their own feed that read as if fifty accounts had been
created *for them* — indistinguishable from the one that genuinely was. So the
admin's row has a type of its own, ``admin.user_invitation.create``.

The positives here are the cheap half. **The negatives are the fix**: an
implementation that emits both types into both feeds passes every "the row is
there" assertion in this file and fails only the four that say a type is
*absent* from the other feed. ``GET /security-events/`` is self-scoped, which
is what makes absence provable — you read a feed by holding its owner's token,
so a row attributed to the wrong user is simply not in the answer.

**The omission rule.** ``InviteUserRequest`` makes "the admin did not say"
representable (``is_active: bool | None``, and a blank ``full_name``
normalised to ``None`` by a validator), and ``_resume_interrupted`` writes a
field if and only if it is not ``None``. The rule only has teeth on the
adoption path, where a row already exists: re-inviting an account an admin had
deliberately deactivated used to reactivate it, from a wizard that does not
even show an active toggle.

A test that only checks the omission direction is passed by an implementation
that never writes the field at all, so every case below is asserted as a
**pair** — omitted keeps, stated writes.

Wreckage is produced the way it actually happens (see
``is_interrupted_invite``): the invitation INSERT fails at the database after
the account row has already been committed. Each adoption consumes its
account — the adopted row now has an invitation, so a second re-invite of the
same address is refused as an ordinary duplicate — which is why each phase
below makes its own.

The single-credential inactive case, the fresh-account ``is_active: false``
case and the five statuses of the users list are in
``users_invitation_lifecycle_test.py``; the public accept/lookup half is in
``tests/api/auth/``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings

# ``app.utils`` is the one app module ``tests/api/`` may import (README Rule
# 1). ``restore_session`` is needed after a deliberate statement failure,
# exactly as ``tests/api/auth/invitation_accept_test.py`` uses it.
from app.utils import restore_session
from tests.utils.account_provisioning import failing_sql_statement
from tests.utils.invitation import (
    accept,
    fresh_invitation_limiter,
    invite_and_token,
    invite_user,
)
from tests.utils.security_event import events_of_type
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR

# Written out rather than imported from the service: these strings are the
# contract a security feed is read with, and a rename that quietly changes
# them must break a test rather than be followed by one.
EVENT_INVITATION_CREATED = "user.invitation.created"
EVENT_ADMIN_INVITATION_CREATED = "admin.user_invitation.create"

# The invitation row's INSERT — the one statement whose failure leaves an
# account behind with nothing to speak for it.
INVITATION_INSERT = "INSERT INTO user_invitation"

# This module creates no agents or environments.
NEEDS_AGENT_STUBS = False


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch) -> None:
    """Seam S9: the anonymous limiter is a module global, not test state."""
    fresh_invitation_limiter(monkeypatch)


# ── Local helpers ──────────────────────────────────────────────────────


def _find_user(
    client: TestClient, superuser_token_headers: dict[str, str], email: str
) -> dict:
    """The admin list row for ``email``. The account has no token of its own."""
    response = client.get(
        f"{API}/users/", headers=superuser_token_headers, params={"limit": 200}
    )
    assert response.status_code == 200, response.text
    matches = [row for row in response.json()["data"] if row["email"] == email]
    assert matches, f"no account for {email}"
    return matches[0]


def _interrupted_invite(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    *,
    full_name: str | None = None,
) -> dict:
    """Leave behind the wreckage of a half-finished invite, and return its row.

    The account row commits, the invitation INSERT is broken at the database,
    and what is left is the one shape ``is_interrupted_invite`` describes: no
    password, no Google identity, no invitation. That is the only state a
    re-invite adopts rather than refusing as a duplicate.
    """
    email = random_email()
    payload: dict = {"email": email, "role": "agent-user", "send_email": False}
    if full_name is not None:
        payload["full_name"] = full_name

    with failing_sql_statement(
        db, when_statement_contains=INVITATION_INSERT
    ) as injected:
        with pytest.raises(Exception):
            client.post(
                f"{API}/users/invite",
                headers=superuser_token_headers,
                json=payload,
            )
    assert injected["fired"], "the injection never fired — nothing was proved"
    restore_session(db)

    row = _find_user(client, superuser_token_headers, email)
    assert row["invitation_status"] is None, (
        "the wreckage still has an invitation row, so a re-invite would be "
        "refused as a duplicate instead of adopting it"
    )
    return row


def _deactivate(
    client: TestClient, superuser_token_headers: dict[str, str], user_id: str
) -> None:
    response = client.patch(
        f"{API}/users/{user_id}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False


def _accept_and_headers(
    client: TestClient, token: str
) -> dict[str, str]:
    """Redeem an invitation and return the new account's own auth headers.

    The invitee's feed is only readable by the invitee — that is the property
    the negative assertions lean on — so reading it costs an acceptance.
    """
    response = accept(client, token, random_lower_string())
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ── Item 1: two feeds, two facts, two types ────────────────────────────


def test_one_invite_writes_one_row_into_each_feed_and_neither_type_into_the_other(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A single invite, read from both sides:
      1. The acting admin's feed carries exactly one
         ``admin.user_invitation.create``, keyed to the admin
      2. …and **zero** ``user.invitation.created`` — the admin did not have an
         account created for them
      3. The invitee's feed carries exactly one ``user.invitation.created``,
         keyed to the invitee
      4. …and **zero** ``admin.user_invitation.create`` — the invitee did not
         invite anybody
      5. ``details.target_user_id`` names the invitee on *both* rows, which is
         what makes the pair correlatable despite the different types

    2 and 4 are the assertions the fix is for. Before it, one type went to both
    feeds and 1, 3 and 5 all passed.
    """
    admin = client.get(f"{API}/users/me", headers=superuser_token_headers)
    assert admin.status_code == 200, admin.text
    admin_id = admin.json()["id"]

    email = random_email()
    body, token = invite_and_token(
        client, superuser_token_headers, email=email, role="agent-user"
    )
    invitee_id = body["user"]["id"]
    assert invitee_id != admin_id

    # ── Phase 1 + 2: the acting admin's own feed ──────────────────────
    admin_rows = events_of_type(
        client, superuser_token_headers, EVENT_ADMIN_INVITATION_CREATED
    )
    assert len(admin_rows) == 1, admin_rows
    assert admin_rows[0]["user_id"] == admin_id
    assert admin_rows[0]["details"]["target_user_id"] == invitee_id
    assert admin_rows[0]["details"]["actor"] == admin_id

    assert (
        events_of_type(
            client, superuser_token_headers, EVENT_INVITATION_CREATED
        )
        == []
    ), (
        "the admin's feed says an account was created *for them*; an admin "
        "onboarding fifty people cannot tell those rows from the one that was"
    )

    # ── Phase 3 + 4: the invitee's own feed ───────────────────────────
    invitee_headers = _accept_and_headers(client, token)

    invitee_rows = events_of_type(
        client, invitee_headers, EVENT_INVITATION_CREATED
    )
    assert len(invitee_rows) == 1, invitee_rows
    assert invitee_rows[0]["user_id"] == invitee_id
    # ── Phase 5 ───────────────────────────────────────────────────────
    assert invitee_rows[0]["details"]["target_user_id"] == invitee_id

    assert (
        events_of_type(
            client, invitee_headers, EVENT_ADMIN_INVITATION_CREATED
        )
        == []
    ), "the invitee's feed says they performed an administrative act"


def test_two_invites_by_one_admin_are_two_admin_rows_and_one_row_each(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The counting property, which one invite cannot show:
      1. Two invites by the same admin ⇒ **two** ``admin.user_invitation.create``
         rows in that admin's feed, naming the two different invitees
      2. Each invitee's feed carries exactly **one** ``user.invitation.created``
         — their own, never the other person's
      3. Neither feed accumulates four rows of one type, which is what a single
         shared event type would produce here
    """
    admin_id = client.get(
        f"{API}/users/me", headers=superuser_token_headers
    ).json()["id"]

    first_body, first_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    second_body, second_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    first_id = first_body["user"]["id"]
    second_id = second_body["user"]["id"]

    # ── Phase 1: two rows, one per invite, in the admin's feed ────────
    admin_rows = events_of_type(
        client, superuser_token_headers, EVENT_ADMIN_INVITATION_CREATED
    )
    assert len(admin_rows) == 2, admin_rows
    assert {row["user_id"] for row in admin_rows} == {admin_id}
    assert {row["details"]["target_user_id"] for row in admin_rows} == {
        first_id,
        second_id,
    }
    assert (
        events_of_type(
            client, superuser_token_headers, EVENT_INVITATION_CREATED
        )
        == []
    )

    # ── Phase 2 + 3: one row each, and only their own ─────────────────
    for token, own_id, other_id in (
        (first_token, first_id, second_id),
        (second_token, second_id, first_id),
    ):
        headers = _accept_and_headers(client, token)
        rows = events_of_type(client, headers, EVENT_INVITATION_CREATED)
        assert len(rows) == 1, rows
        assert rows[0]["user_id"] == own_id
        assert rows[0]["details"]["target_user_id"] == own_id
        assert rows[0]["details"]["target_user_id"] != other_id
        assert (
            events_of_type(client, headers, EVENT_ADMIN_INVITATION_CREATED)
            == []
        )


# ── Item 2: omission never overwrites persistent account state ─────────


def test_a_re_invite_writes_a_field_if_and_only_if_the_admin_stated_it(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The adoption path, both directions of the rule, on both optional fields:
      1. A **deactivated** adopted account, re-invited with no ``is_active``
         key at all, stays deactivated
      2. The same account shape, re-invited with ``is_active: true``, *is*
         reactivated — the rule is omission-only, not never-write
      3. ``full_name`` omitted keeps the name the row already carried
      4. ``full_name: "   "`` keeps it too — a blank name is an omission
         spelled differently, normalised at the edge by a model validator
      5. ``full_name: "New Name"`` is written

    Phase 1 is the case the fix exists for and the only one that was ever
    broken in production: ``is_active`` was ``bool = True``, so re-inviting an
    account an administrator had deliberately deactivated silently reactivated
    it — from a wizard that does not show an active toggle at all.

    Phases 2 and 5 are what stop the other four from being satisfied by an
    implementation that simply never writes either field.

    Every phase re-invites a *fresh* piece of wreckage: adoption gives the row
    an invitation, after which the same address is an ordinary duplicate.
    """
    # ── Phase 1: omission does not reactivate ─────────────────────────
    row = _interrupted_invite(
        client, superuser_token_headers, db, full_name="Original Name"
    )
    _deactivate(client, superuser_token_headers, row["id"])

    silent = invite_user(
        client,
        superuser_token_headers,
        email=row["email"],
        role="agent-user",
        is_active=None,
    )
    assert silent["adopted_existing_account"] is True, (
        "the account was not adopted, so this phase asserts nothing about "
        "the adoption path"
    )
    assert silent["user"]["id"] == row["id"]
    assert silent["user"]["is_active"] is False, (
        "re-inviting reactivated an account an administrator had deliberately "
        "deactivated"
    )
    # And it is the persisted row that says so, not only the response.
    assert (
        _find_user(client, superuser_token_headers, row["email"])["is_active"]
        is False
    )

    # ── Phase 2: a stated activation IS written ───────────────────────
    stated_row = _interrupted_invite(client, superuser_token_headers, db)
    _deactivate(client, superuser_token_headers, stated_row["id"])

    stated = invite_user(
        client,
        superuser_token_headers,
        email=stated_row["email"],
        role="agent-user",
        is_active=True,
    )
    assert stated["adopted_existing_account"] is True
    assert stated["user"]["is_active"] is True, (
        "an admin who explicitly ticked active was ignored — the rule is "
        "'omission does not write', not 'this field is never written'"
    )

    # ── Phase 3: an omitted name survives ─────────────────────────────
    named = _interrupted_invite(
        client, superuser_token_headers, db, full_name="Original Name"
    )
    kept = invite_user(
        client,
        superuser_token_headers,
        email=named["email"],
        role="agent-user",
    )
    assert kept["adopted_existing_account"] is True
    assert kept["user"]["full_name"] == "Original Name", (
        "an admin who typed only an address erased the name the row carried"
    )

    # ── Phase 4: a blank name is an omission ──────────────────────────
    blank = _interrupted_invite(
        client, superuser_token_headers, db, full_name="Original Name"
    )
    unchanged = invite_user(
        client,
        superuser_token_headers,
        email=blank["email"],
        role="agent-user",
        full_name="   ",
    )
    assert unchanged["adopted_existing_account"] is True
    assert unchanged["user"]["full_name"] == "Original Name", (
        "whitespace reached the write site instead of normalising to None"
    )

    # ── Phase 5: a stated name IS written ─────────────────────────────
    renamed_row = _interrupted_invite(
        client, superuser_token_headers, db, full_name="Original Name"
    )
    renamed = invite_user(
        client,
        superuser_token_headers,
        email=renamed_row["email"],
        role="agent-user",
        full_name="New Name",
    )
    assert renamed["adopted_existing_account"] is True
    assert renamed["user"]["full_name"] == "New Name"
    assert (
        _find_user(client, superuser_token_headers, renamed_row["email"])[
            "full_name"
        ]
        == "New Name"
    )


def test_a_silent_invite_creates_an_active_account_and_null_is_not_a_422(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The schema's half of the same rule, on a fresh account:
      1. Neither ``is_active`` nor ``full_name`` stated ⇒ an active account
         with no name — ``create_account`` owns that default, and making the
         field nullable must not have moved it
      2. An explicit ``"is_active": null`` is *accepted* (200, active), because
         ``None`` is a value the type admits and means the same thing as the
         omission the wizard sends
      3. ``role`` is still required — the field was not swept into the same
         change, and a submission without one is a 422 naming it

    The fresh-account ``is_active: false`` case is asserted in
    ``users_invitation_lifecycle_test.py::
    test_inviting_a_deactivated_account_grants_nothing_and_mails_nothing``,
    where the dormant link and the suppressed mail are asserted with it.
    """
    # ── Phase 1: stated nothing ───────────────────────────────────────
    silent = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        is_active=None,
    )
    assert silent["user"]["is_active"] is True
    assert silent["user"]["full_name"] is None
    assert silent["adopted_existing_account"] is False

    # ── Phase 2: an explicit null, on the wire ────────────────────────
    explicit_null = client.post(
        f"{API}/users/invite",
        headers=superuser_token_headers,
        json={
            "email": random_email(),
            "role": "agent-user",
            "send_email": False,
            "is_active": None,
            "full_name": None,
        },
    )
    assert explicit_null.status_code == 200, explicit_null.text
    assert explicit_null.json()["user"]["is_active"] is True

    # ── Phase 3: role is not optional ─────────────────────────────────
    no_role = client.post(
        f"{API}/users/invite",
        headers=superuser_token_headers,
        json={"email": random_email(), "send_email": False},
    )
    assert no_role.status_code == 422, no_role.text
    assert any(
        "role" in str(error.get("loc", ())) for error in no_role.json()["detail"]
    ), no_role.json()

    # A *garbage* role is refused too, but that one is already asserted in
    # ``users_invitation_lifecycle_test.py``; what is new here is that making
    # two sibling fields optional did not make this one optional as well.
