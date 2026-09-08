"""The administrator's half of the invitation lifecycle.

Invite → read → resend → revoke → link, plus the two things about invite that
are easy to get backwards and invisible when you do:

* ``role="admin"`` must imply ``is_superuser=True``. ``create_account`` will
  happily accept ``role="admin", is_superuser=False`` and produce a row that
  breaks the invariant, so the derivation lives in the service and is asserted
  here from the response.
* Every guard on this surface must refuse a *transition*, never a *state*.
  Resend is exactly what fixes an expired invitation and exactly what
  reactivates a revoked one, so refusing either would refuse the only requests
  that need it. Only acceptance is terminal.

The public half — lookup and accept, where every refusal must be the same
byte-identical answer — is in ``tests/api/auth/invitation_enumeration_test.py``
and ``tests/api/auth/invitation_accept_test.py``.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings

# ``app.utils`` is the one app module ``tests/api/`` may import (README Rule 1).
# ``restore_session`` is needed after the deliberate statement failure that
# leaves an interrupted invite behind.
from app.utils import restore_session
from tests.utils.account_provisioning import (
    CHILD_CREDENTIAL_INSERT,
    MEMBERSHIP_LOOKUP,
    failing_sql_statement,
    session_is_usable,
)
from tests.utils.invitation import (
    accept,
    fresh_invitation_limiter,
    get_invitation,
    get_invitation_link,
    invitation_email_patched,
    invite_and_token,
    invite_user,
    jti_of,
    lookup,
    resend_invitation,
    revoke_invitation,
    token_of,
)
from tests.utils.ai_provider_admin import (
    create_provider_credential,
    strand_managed_credential,
)
from tests.utils.managed_ai_credential import (
    get_managed_credential,
    member_user_ids,
)
from tests.utils.query_counter import count_queries
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR

# The owned-credential SELECT in ``_provision``'s prologue — the second half of
# ``AIProvidersService.grant_targets``, which resolves the providers the admin
# ticked into the credentials they grant through. Aimed at by the total-failure
# test below: it runs *before* the per-provider guard, so it is the one
# injection that reaches ``_guarded``'s outer net.
PARENT_CREDENTIAL_LOOKUP = "FROM managed_ai_credential"

# One managed credential refreshed by primary key. Deliberately *not* the
# fragment above, which also matches ``managed_ai_credential_membership`` —
# ``add_members``' own prologue — and so cannot name a statement in
# ``_provision``'s loop. ``add_members`` commits, so from the second granted
# provider onward every attribute read of its credential, ``id`` included, is
# this statement, inside the per-provider net rather than above it.
CREDENTIAL_REFRESH = "WHERE managed_ai_credential.id ="

# This module creates no agents or environments.
NEEDS_AGENT_STUBS = False


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch) -> None:
    """Seam S9: ``RateLimiter._hits`` is a module global that outlives rollback."""
    fresh_invitation_limiter(monkeypatch)


def _login(client: TestClient, email: str, password: str):
    return client.post(
        f"{API}/login/access-token",
        data={"username": email, "password": password},
    )


# ── Creation ───────────────────────────────────────────────────────────


def test_invite_creates_a_pending_passwordless_account_with_a_link(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The wizard's happy path:
      1. Invite an ``agent-user`` with an explicitly ticked provider
      2. The account exists with that role, unconfirmed, not a superuser
      3. The credential was granted through the provisioning service
      4. The invitation reads ``pending`` and the response carries a link
      5. The account cannot be logged into before it is accepted
      6. The link the response carries actually resolves
    """
    # ── Phase 1: A provider the admin ticks explicitly ────────────────
    # The wizard submits **providers** since the provider/credential split: a
    # provider is the key source plus the rule for who gets one, and the
    # credential it owns is where the membership lands.
    parent = create_provider_credential(db, credential_type="anthropic")
    parent_id = parent["record"]["id"]
    provider_id = parent["provider_id"]

    email = random_email()
    body, token = invite_and_token(
        client,
        superuser_token_headers,
        email=email,
        role="agent-user",
        full_name="Invited Person",
        provider_ids=[provider_id],
    )

    # ── Phase 2: The account ──────────────────────────────────────────
    user = body["user"]
    assert user["email"] == email
    assert user["role"] == "agent-user"
    assert user["is_superuser"] is False
    assert user["is_active"] is True
    # Not confirmed: acceptance is what proves control of the address.
    assert user["email_confirmed"] is False
    # ``user_to_public`` is the builder, and it is the only reason ``id`` and
    # ``email`` survive the four commits that precede this projection.
    assert user["id"]

    # ── Phase 3: Provisioning ─────────────────────────────────────────
    assert body["provisioning"]["added_count"] == 1
    assert body["provisioning"]["skipped"] == []
    record = get_managed_credential(client, superuser_token_headers, parent_id)
    assert user["id"] in member_user_ids(record)

    # ── Phase 4: The invitation ───────────────────────────────────────
    invitation = body["invitation"]
    assert invitation["status"] == "pending"
    assert invitation["user_id"] == user["id"]
    assert invitation["send_count"] == 0
    assert invitation["accepted_at"] is None
    assert invitation["revoked_at"] is None
    assert invitation["invited_by_email"] == settings.FIRST_SUPERUSER
    assert body["accept_url"].startswith(f"{settings.FRONTEND_HOST}/accept-invite?")
    assert body["email_sent"] is False

    # The admin projection agrees with the one embedded in the response.
    assert get_invitation(client, superuser_token_headers, user["id"]) == invitation

    # ── Phase 5: The account is not usable yet ────────────────────────
    # No password was ever set, so ``authenticate`` refuses — with the same
    # generic body an unknown address gets.
    blocked = _login(client, email, random_lower_string())
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"] == "Incorrect email or password"

    # ── Phase 6: The link resolves ────────────────────────────────────
    resolved = lookup(client, token)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["valid"] is True
    assert resolved.json()["full_name"] == "Invited Person"


def test_inviting_an_admin_implies_superuser_and_a_confirmed_address(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``role="admin"`` ⇒ ``is_superuser=True`` — derived server-side, D4.

    ``create_account`` treats ``is_superuser=True`` as pinning the role *and*
    auto-confirming the address, and it will also accept the inconsistent
    ``role="admin", is_superuser=False``. ``is_superuser`` is deliberately not
    a request field, so the wizard cannot ask for the broken combination.
    """
    body = invite_user(
        client, superuser_token_headers, email=random_email(), role="admin"
    )
    assert body["user"]["role"] == "admin"
    assert body["user"]["is_superuser"] is True
    # Auto-confirmed by ``create_account``: an unconfirmed superuser has its
    # own notifications gated.
    assert body["user"]["email_confirmed"] is True


def test_invite_refuses_a_duplicate_address_and_an_unknown_role(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Two refusals with two different shapes, and the difference matters.

    A duplicate address is a 400 carrying the message the wizard keys its
    "resend instead?" hint off. An unknown role never reaches the service at
    all: ``InviteUserRequest`` has a field validator, so it is a **422**
    naming the field rather than a 400 naming nothing.
    """
    existing, _ = create_random_user_with_headers(client)

    duplicate = client.post(
        f"{API}/users/invite",
        headers=superuser_token_headers,
        json={"email": existing["email"], "role": "agent-user"},
    )
    assert duplicate.status_code == 400, duplicate.text
    assert "already exists in the system" in duplicate.json()["detail"]

    bad_role = client.post(
        f"{API}/users/invite",
        headers=superuser_token_headers,
        json={"email": random_email(), "role": "wizard"},
    )
    assert bad_role.status_code == 422, bad_role.text


# ── Resend ─────────────────────────────────────────────────────────────


def test_resend_cooldown_is_per_row_and_reports_when_it_lifts(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Resend, with mail actually going out:
      1. Invite with ``send_email`` and prove the patch intercepts
      2. ``last_sent_at`` / ``send_count`` are stamped
      3. An immediate resend is 429 with ``resend_available_at`` and Retry-After
      4. The refusal did not rotate the token — the outstanding link still works
      5. A *different* invitation is unaffected: the cooldown is per row
    """
    # ── Phase 1: One real send, through the right binding ─────────────
    email = random_email()
    with invitation_email_patched() as mock_send:
        body, token = invite_and_token(
            client, superuser_token_headers, email=email, send_email=True
        )
        # Proof the patch intercepts. A target that silently misses would
        # leave this at zero while mailcatcher absorbed the real connection.
        assert mock_send.call_count == 1, "send_email patch did not intercept"
        html = mock_send.call_args.kwargs["html_content"]

    user_id = body["user"]["id"]
    assert body["email_sent"] is True
    # A call-count assertion alone passes on an empty rendered body, so the
    # rendered content is checked too: the mail must carry the *live* link.
    assert body["accept_url"] in html
    assert token in html

    # ── Phase 2: Stamped ──────────────────────────────────────────────
    invitation = get_invitation(client, superuser_token_headers, user_id)
    assert invitation["send_count"] == 1
    assert invitation["last_sent_at"] is not None

    # ── Phase 3: Immediately again ────────────────────────────────────
    with invitation_email_patched() as mock_send:
        throttled = resend_invitation(client, superuser_token_headers, user_id)
        assert mock_send.call_count == 0
    assert throttled.status_code == 429, throttled.text
    detail = throttled.json()["detail"]
    assert detail["code"] == "invitation_resend_cooldown"
    assert detail["resend_available_at"]
    assert int(throttled.headers["Retry-After"]) >= 1

    # ── Phase 4: The refusal acted on nothing ─────────────────────────
    # The cooldown is checked before the rotation precisely so a refused
    # resend leaves the link the recipient may be about to click alone.
    assert lookup(client, token).json()["valid"] is True

    # ── Phase 5: Another invitation is not throttled ──────────────────
    with invitation_email_patched() as mock_send:
        other = invite_user(
            client,
            superuser_token_headers,
            email=random_email(),
            send_email=True,
        )
        assert mock_send.call_count == 1
    assert other["email_sent"] is True


def test_resend_reactivates_a_revoked_invitation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Revocation is reversible by design; resend is the reversal.

    Refusing here would be a state check on a field this request transitions:
    resend *sets* ``revoked_at`` back to null, so "it is revoked" is not a
    reason to refuse it — it is the reason to run it.
    """
    body, first_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    user_id = body["user"]["id"]

    assert revoke_invitation(client, superuser_token_headers, user_id).status_code == 200
    assert get_invitation(client, superuser_token_headers, user_id)["status"] == "revoked"
    assert lookup(client, first_token).json() == {"valid": False}

    with invitation_email_patched() as mock_send:
        revived = resend_invitation(client, superuser_token_headers, user_id)
        assert mock_send.call_count == 1, "send_email patch did not intercept"
        html = mock_send.call_args.kwargs["html_content"]
    assert revived.status_code == 200, revived.text
    assert revived.json()["invitation"]["status"] == "pending"
    assert revived.json()["invitation"]["revoked_at"] is None

    # The revoked link stays dead — reactivation rotated the jti.
    second_token = token_of(revived.json()["accept_url"])
    assert lookup(client, first_token).json() == {"valid": False}
    assert lookup(client, second_token).json()["valid"] is True

    # The mail carries the rotated link and not the dead one. Asserting only
    # that *a* link was rendered would pass on a template still interpolating
    # the previous token, which is the failure S1/S10 describe.
    assert second_token in html
    assert first_token not in html
    assert jti_of(second_token) != jti_of(first_token)


def test_resend_and_revoke_report_missing_invitations_and_accepted_ones(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Superuser context, so the answers are specific:
      1. An account that was never invited → 404 on both
      2. An accepted invitation → 409 on both (the one terminal state)
      3. Revoke is idempotent — revoking twice is a success, not a 409
      4. Revoking an *expired* invitation succeeds: expiry is not transitioned
    """
    # ── Phase 1: Never invited ────────────────────────────────────────
    plain, _ = create_random_user_with_headers(client)
    assert resend_invitation(client, superuser_token_headers, plain["id"]).status_code == 404
    assert revoke_invitation(client, superuser_token_headers, plain["id"]).status_code == 404
    missing = client.get(
        f"{API}/users/{plain['id']}/invitation", headers=superuser_token_headers
    )
    assert missing.status_code == 404, missing.text

    # ── Phase 2: Accepted ─────────────────────────────────────────────
    body, token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    accepted_id = body["user"]["id"]
    assert accept(client, token, random_lower_string()).status_code == 200

    resent = resend_invitation(client, superuser_token_headers, accepted_id)
    assert resent.status_code == 409, resent.text
    revoked = revoke_invitation(client, superuser_token_headers, accepted_id)
    assert revoked.status_code == 409, revoked.text

    # ── Phase 3: Revoke is idempotent ─────────────────────────────────
    pending = invite_user(client, superuser_token_headers, email=random_email())
    pending_id = pending["user"]["id"]
    first = revoke_invitation(client, superuser_token_headers, pending_id)
    second = revoke_invitation(client, superuser_token_headers, pending_id)
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert second.json()["status"] == "revoked"
    # Idempotent means the timestamp is not re-stamped either.
    assert first.json()["revoked_at"] == second.json()["revoked_at"]

    # ── Phase 4: Expiry is not a state revoke transitions ─────────────
    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        expired = invite_user(
            client, superuser_token_headers, email=random_email()
        )
    expired_id = expired["user"]["id"]
    assert get_invitation(client, superuser_token_headers, expired_id)["status"] == "expired"
    late_revoke = revoke_invitation(client, superuser_token_headers, expired_id)
    assert late_revoke.status_code == 200, late_revoke.text
    assert late_revoke.json()["status"] == "revoked"

    # And resend is exactly what repairs an expired invitation.
    repaired = resend_invitation(client, superuser_token_headers, expired_id)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["invitation"]["status"] == "pending"


# ── Link read-out ──────────────────────────────────────────────────────


def test_invitation_link_is_refused_for_every_non_pending_state(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Handing over a dud link is worse than refusing to hand one over.

    A revoked, expired or accepted invitation's link passes signature and
    purpose verification and is then refused by ``accept`` as indistinguishable
    from a forgery. 409 naming the status is what tells the admin to resend.
    """
    body = invite_user(client, superuser_token_headers, email=random_email())
    user_id = body["user"]["id"]
    assert get_invitation_link(client, superuser_token_headers, user_id).status_code == 200

    revoke_invitation(client, superuser_token_headers, user_id)
    refused = get_invitation_link(client, superuser_token_headers, user_id)
    assert refused.status_code == 409, refused.text
    assert "revoked" in refused.json()["detail"]

    never_invited, _ = create_random_user_with_headers(client)
    assert (
        get_invitation_link(
            client, superuser_token_headers, never_invited["id"]
        ).status_code
        == 404
    )


# ── Authorisation ──────────────────────────────────────────────────────


def test_a_non_superuser_gets_403_on_every_invitation_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """§4.3: the same 403 for an id that exists and one that does not.

    ``get_current_active_superuser`` refuses before any lookup runs, so the
    invitation surface cannot be used to enumerate accounts. If a route ever
    resolved the user first, a real id would answer 404-or-409 and a fabricated
    one 404 — a difference an ordinary user could read.
    """
    _, member_headers = create_random_user_with_headers(client)
    real = invite_user(client, superuser_token_headers, email=random_email())
    real_id = real["user"]["id"]
    ghost_id = str(uuid.uuid4())

    responses = []
    for user_id in (real_id, ghost_id):
        responses.append(
            client.get(f"{API}/users/{user_id}/invitation", headers=member_headers)
        )
        responses.append(
            client.post(
                f"{API}/users/{user_id}/invitation/resend", headers=member_headers
            )
        )
        responses.append(
            client.post(
                f"{API}/users/{user_id}/invitation/revoke", headers=member_headers
            )
        )
        responses.append(
            client.get(
                f"{API}/users/{user_id}/invitation/link", headers=member_headers
            )
        )
    responses.append(
        client.post(
            f"{API}/users/invite",
            headers=member_headers,
            json={"email": random_email(), "role": "agent-user"},
        )
    )

    assert [r.status_code for r in responses] == [403] * 9, [
        (r.request.url.path, r.status_code) for r in responses
    ]
    # Byte-identical too: a differently-worded 403 would be its own signal.
    assert len({r.content for r in responses}) == 1


# ── Provisioning cannot fail the invite ────────────────────────────────


def test_a_provisioning_failure_does_not_cost_the_invitation(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """Invariant 2, through the entry point **this phase added**.

    "Account creation never fails because provisioning failed" was already
    covered for ``on_account_created``. ``provision_explicit`` is a second
    entry point, and the entire X1 argument for adding it — rather than
    looping over ``add_members`` in the route — is that it *shares* the outer
    net, the ``_restore_session`` discipline and the skip reporting instead of
    reimplementing them badly. Until now that was asserted only by reading the
    code, which is exactly how the invisible version of this bug ships: the
    invite succeeds, the account exists, and the person signs in with no
    company key and nothing anywhere says so.

    The failures are injected at the database, so what the production stack
    experiences is an **aborted transaction** — the shape where the recovery
    code itself is what throws. A patched collaborator raising ``ValueError``
    would exercise the ``try`` and prove none of that.

    THERE ARE TWO NETS HERE AND THEY REPORT DIFFERENT REASONS
    ---------------------------------------------------------
    Worth stating because the first draft of this test asserted the wrong one
    and the implementation was right:

    * a failure **inside** ``add_members`` — the child ``INSERT`` — is caught
      by ``add_members``' own per-owner handler, which repairs the session and
      returns normally with ``reason="provision_failed"``. ``_provision`` never
      sees an exception at all;
    * a failure in ``add_members``' **prologue** — the existing-members query,
      which runs before its per-owner ``try`` — escapes it, and
      ``_provision``'s per-provider handler catches that as
      ``reason="add_members_failed"``.

    Both are exercised, because a change that collapsed one into the other
    would still leave the invite returning 200 and would be invisible to a
    test that only knew about one.
    """
    parent = create_provider_credential(db, credential_type="anthropic")
    parent_id = parent["record"]["id"]
    provider_id = parent["provider_id"]
    ghost_provider_id = str(uuid.uuid4())

    # ── Net 1: the child insert fails inside add_members ──────────────
    email = random_email()
    with failing_sql_statement(
        db, when_statement_contains=CHILD_CREDENTIAL_INSERT
    ) as injected:
        response = client.post(
            f"{API}/users/invite",
            headers=superuser_token_headers,
            json={
                "email": email,
                "role": "agent-user",
                "provider_ids": [provider_id, ghost_provider_id],
            },
        )
    assert injected["fired"], "the injection never fired — nothing was proved"

    # The invite survived, and everything after provisioning still ran — so
    # the session was genuinely repaired, not merely not-crashed.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == email
    assert body["invitation"]["status"] == "pending"
    assert body["accept_url"]

    # The failure is reported, not raised.
    assert body["provisioning"]["added_count"] == 0
    assert {
        skip["provider_id"]: skip["reason"]
        for skip in body["provisioning"]["skipped"]
    } == {
        provider_id: "provision_failed",
        # Reachable only from the explicit path: an id the admin ticked that
        # no longer names a provider is reported rather than silently dropped,
        # so "you asked for two keys and got none" is visible. The reason
        # string is ``provider_not_found`` — the wizard submits providers now,
        # and a copy map still carrying ``managed_credential_not_found``
        # renders a blank line here.
        ghost_provider_id: "provider_not_found",
    }, body["provisioning"]

    # And the grant genuinely did not land. Without this the test would pass
    # on an implementation that reported a skip it did not actually suffer.
    record = get_managed_credential(client, superuser_token_headers, parent_id)
    assert body["user"]["id"] not in member_user_ids(record)

    # The account is fully usable afterwards.
    assert session_is_usable(db)
    accepted = accept(client, token_of(body["accept_url"]), random_lower_string())
    assert accepted.status_code == 200, accepted.text

    # ── Net 2: add_members itself raises, in its prologue ─────────────
    second_email = random_email()
    with failing_sql_statement(
        db, when_statement_contains=MEMBERSHIP_LOOKUP
    ) as injected:
        second = client.post(
            f"{API}/users/invite",
            headers=superuser_token_headers,
            json={
                "email": second_email,
                "role": "agent-user",
                "provider_ids": [provider_id],
            },
        )
    assert injected["fired"], "the injection never fired — nothing was proved"

    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["invitation"]["status"] == "pending"
    assert second_body["provisioning"]["added_count"] == 0
    assert second_body["provisioning"]["skipped"] == [
        {
            "provider_id": provider_id,
            "reason": "add_members_failed",
        }
    ], second_body["provisioning"]
    assert session_is_usable(db)
    assert (
        accept(
            client, token_of(second_body["accept_url"]), random_lower_string()
        ).status_code
        == 200
    )


def test_the_provisioning_tri_state_grants_exactly_what_the_admin_stated(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``null`` / ``[]`` / a list are three different instructions, on one setup.

    The wizard's ``provider_ids`` is tri-state and the middle state is the one a
    flattened implementation loses: ``None`` means "grant whatever this role
    would have been auto-provisioned anyway", ``[]`` means the admin
    deliberately unticked everything, and a list means exactly those providers.
    Read one at a time they are easy to satisfy by accident — an implementation
    that treats ``[]`` as ``None`` passes any test that only sends ``None``, and
    one that ignores the field entirely passes any test whose provider
    auto-provisions the invited role anyway.

    So all three run against the **same** two providers, and the discriminating
    ingredient is that the second one auto-provisions nobody:

      1. ``None``  ⇒ the auto-provisioning provider only
      2. ``[]``    ⇒ neither, on an account whose role the first provider covers
      3. ``[opt]`` ⇒ the opt-in provider only, and *not* the automatic one —
         "exactly these", not "these as well as the usual"

    The sibling test above covers the same three shapes on a **deactivated**
    invitee, where the answer is the same for all three and the report is what
    differs. This one is the active path, where the grants themselves differ.
    """
    automatic = create_provider_credential(
        db,
        credential_type="anthropic",
        auto_provision_roles=["agent-user"],
    )
    opt_in = create_provider_credential(
        db, credential_type="anthropic", auto_provision_roles=[]
    )

    def members(record_id: str) -> set[str]:
        return member_user_ids(
            get_managed_credential(client, superuser_token_headers, record_id)
        )

    # ── 1: the admin did not say ──────────────────────────────────────
    silent = invite_user(
        client, superuser_token_headers, email=random_email(), role="agent-user"
    )
    assert silent["provisioning"]["added_count"] == 1, silent["provisioning"]
    assert silent["user"]["id"] in members(automatic["record"]["id"])
    assert silent["user"]["id"] not in members(opt_in["record"]["id"])

    # ── 2: the admin unticked everything ──────────────────────────────
    unticked = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        provider_ids=[],
    )
    assert unticked["provisioning"]["added_count"] == 0, unticked["provisioning"]
    assert unticked["provisioning"]["skipped"] == []
    assert unticked["provisioning"]["provisioning_failed"] is False, (
        "an empty list is a choice, not a failure"
    )
    assert unticked["user"]["id"] not in members(automatic["record"]["id"]), (
        "[] was read as 'the usual set' — the admin's unticking was discarded"
    )
    assert unticked["user"]["id"] not in members(opt_in["record"]["id"])

    # ── 3: exactly these, and nothing else ────────────────────────────
    chosen = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        provider_ids=[opt_in["provider_id"]],
    )
    assert chosen["provisioning"]["added_count"] == 1, chosen["provisioning"]
    assert chosen["user"]["id"] in members(opt_in["record"]["id"])
    assert chosen["user"]["id"] not in members(automatic["record"]["id"]), (
        "a stated list was granted *in addition to* the role's automatic set"
    )


def test_a_failure_on_the_second_provider_keeps_what_the_first_one_granted(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A partial failure must report a partial result, not a total one.

    The per-provider handler and the outer net answer differently on purpose:
    the handler records one skip and carries on, the net returns
    ``provisioning_failed=True`` with **empty** lists because it has nothing to
    report per provider. So every statement inside the loop that can touch the
    session has to be inside the handler, or a failure there is promoted from
    "one provider did not land" to "we cannot tell you what happened" — while
    the account is holding the credentials the report just disowned.

    The statement that makes this reachable is not obvious: ``add_members``
    commits, ``expire_on_commit`` is on, and so reading the *second* target's
    ``ManagedAICredential.id`` is a refresh ``SELECT`` rather than an attribute
    read. That is why the injection names the primary-key refresh specifically
    rather than the prologue's lookup: the prologue is the outer net's own case,
    covered by the test above, and its fragment also matches
    ``managed_ai_credential_membership``, which is a different statement in a
    different net.

    ``occurrence=3`` is the second target's refresh — the first two are inside
    the first target's own ``add_members``, which commits per member and reads
    its parent again afterwards. The number is a coordinate rather than a
    contract, and it is safe to write one down here only because the assertions
    say *which* provider was skipped and *with which reason*: an injection that
    drifted onto one of add_members' own statements reports the **first**
    provider, or ``provision_failed`` instead of ``add_members_failed``, and
    this test fails rather than passing on the wrong failure.
    """
    first = create_provider_credential(db, credential_type="anthropic")
    second = create_provider_credential(db, credential_type="anthropic")

    with failing_sql_statement(
        db, when_statement_contains=CREDENTIAL_REFRESH, occurrence=3
    ) as injected:
        body = invite_user(
            client,
            superuser_token_headers,
            email=random_email(),
            role="agent-user",
            provider_ids=[first["provider_id"], second["provider_id"]],
        )
    assert injected["fired"], "the injection never fired — nothing was proved"

    assert body["provisioning"]["provisioning_failed"] is False, (
        "one provider failing was reported as the whole of provisioning "
        "falling over; the grant that did land is invisible in that report"
    )
    assert body["provisioning"]["added_count"] == 1, body["provisioning"]
    assert body["provisioning"]["skipped"] == [
        {
            "provider_id": second["provider_id"],
            "reason": "add_members_failed",
        }
    ], body["provisioning"]

    # And the report matches the database rather than merely being internally
    # consistent: the first grant is real, the second did not happen.
    user_id = body["user"]["id"]
    assert user_id in member_user_ids(
        get_managed_credential(
            client, superuser_token_headers, first["record"]["id"]
        )
    )
    assert user_id not in member_user_ids(
        get_managed_credential(
            client, superuser_token_headers, second["record"]["id"]
        )
    )
    assert session_is_usable(db)


def test_a_provider_with_no_credential_to_grant_through_is_reported(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The second producer of ``provider_not_found``, and the quiet one.

    A ticked id that names no provider at all is the obvious case and is covered
    above. This is the other one: the provider exists, the admin can see it and
    tick it, and it owns no managed credential — so there is no member list to
    add anybody to. That state was reachable through the superseded
    ``/admin/provider-admin-credentials`` route, which wrote a bare ``AIProvider``
    with no credential; Phase 4 deleted it, so no live route produces one any
    more and only rows predating the deletion arise naturally (see
    ``AIProvidersService.owned_credential``, which still resolves ``None`` for
    them). It is reproduced here by clearing the pointer instead — the seam that
    can make one on demand, and now the only one.

    What must not happen is a silent drop. The admin ticked a box and nothing
    came of it; if the report does not say so, the person signs in with one key
    fewer than the wizard implied and nothing anywhere records the difference.
    """
    grantable = create_provider_credential(db, credential_type="anthropic")
    stranded = create_provider_credential(db, credential_type="anthropic")
    strand_managed_credential(db, stranded["record"]["id"])

    body = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        provider_ids=[grantable["provider_id"], stranded["provider_id"]],
    )

    assert body["provisioning"]["added_count"] == 1, body["provisioning"]
    assert body["provisioning"]["skipped"] == [
        {
            "provider_id": stranded["provider_id"],
            "reason": "provider_not_found",
        }
    ], body["provisioning"]
    # The grantable half still landed: a provider that cannot be granted must
    # not cost the request the ones that can.
    assert body["user"]["id"] in member_user_ids(
        get_managed_credential(
            client, superuser_token_headers, grantable["record"]["id"]
        )
    )


def test_a_total_provisioning_failure_is_not_reported_as_granting_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """The two states an empty ``ProvisioningReport`` used to collapse.

    ``_guarded`` catches everything, so a failure in ``_provision``'s
    *prologue* — before any credential has even been selected, which is the
    one place the per-parent guards do not reach — returned an empty report:
    no ``added``, no ``skipped``, only a log line. The wizard rendered that as
    "No AI credentials were granted", byte-identical to what an admin who
    deliberately unticked every box sees. A total failure that looks exactly
    like a deliberate choice is a failure nobody will ever notice.

    Both are driven here, in one test, because the assertion that matters is
    that they *differ* — either one alone passes against an implementation
    that reports both the same way.
    """
    provider_id = create_provider_credential(
        db, credential_type="anthropic"
    )["provider_id"]

    # ── The admin who ticked nothing ──────────────────────────────────
    deliberate = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        provider_ids=[],
    )
    assert deliberate["provisioning"]["added_count"] == 0
    assert deliberate["provisioning"]["skipped"] == []
    assert deliberate["provisioning"]["provisioning_failed"] is False

    # ── Provisioning that fell over before naming a credential ────────
    # Injected at the parent lookup, which runs in the prologue: the
    # per-parent guard has not been entered yet, so this is the outer net's
    # own case and the only one that can produce a report with nothing in it.
    with failing_sql_statement(
        db, when_statement_contains=PARENT_CREDENTIAL_LOOKUP
    ) as injected:
        response = client.post(
            f"{API}/users/invite",
            headers=superuser_token_headers,
            json={
                "email": random_email(),
                "role": "agent-user",
                "provider_ids": [provider_id],
            },
        )
    assert injected["fired"], "the injection never fired — nothing was proved"

    assert response.status_code == 200, response.text
    body = response.json()
    # Invariant 2 still holds: provisioning cannot fail the invite.
    assert body["invitation"]["status"] == "pending"
    assert body["accept_url"]
    assert session_is_usable(db)

    # Same two empty lists as the deliberate case — and now distinguishable.
    assert body["provisioning"]["added_count"] == 0
    assert body["provisioning"]["skipped"] == []
    assert body["provisioning"]["provisioning_failed"] is True


# ── A deactivated invitee ──────────────────────────────────────────────


def test_inviting_a_deactivated_account_grants_nothing_and_mails_nothing(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    Two deliberate decisions in one story, the second a judgment call:
      1. ``_provision`` short-circuits on ``not user.is_active`` — no grants,
         and **no skip events**, because a medium-severity security event per
         provider, in the feed of an account that has done nothing,
         is noise about an outcome that was never in doubt. The *report*
         still names every requested provider with ``user_inactive``: a
         bare empty report is byte-identical to a deliberate grant of
         nothing, and the admin who ticked three boxes must not see the
         screen of the admin who ticked none
      2. ``send`` suppresses the mail rather than the route refusing the
         request — the link is *dormant*, not dead
      3. A suppressed send does not stamp ``last_sent_at``, so it does not arm
         the resend cooldown
      4. Activating the account makes the very same link work

    Point 2 is the judgment call worth pinning. ``is_active`` is checked at
    *redemption*, not at issue, so the pre-create-then-activate flow the field
    exists for keeps working; refusing the invite outright would have removed
    it, and mailing anyway would send a link whose only possible answer is the
    deliberately detail-free "no longer valid" — which the recipient cannot
    tell from a forgery and the admin never sees at all.
    """
    # Auto-provisioning is configured on the provider, which is what makes this
    # credential one an ``agent-user`` invite would pick up automatically.
    parent = create_provider_credential(
        db, credential_type="anthropic", auto_provision_roles=["agent-user"]
    )
    parent_id = parent["record"]["id"]
    provider_id = parent["provider_id"]

    email = random_email()
    with invitation_email_patched() as mock_send:
        body = invite_user(
            client,
            superuser_token_headers,
            email=email,
            role="agent-user",
            provider_ids=[provider_id],
            is_active=False,
            send_email=True,
        )
        # Email is *enabled* in this block, so a zero here is the suppression
        # under test and not the gate short-circuiting above it.
        assert mock_send.call_count == 0, "a dormant invitation was mailed"

    user_id = body["user"]["id"]
    assert body["user"]["is_active"] is False
    assert body["email_sent"] is False
    assert body["accept_url"], "the admin must still get a link to hand over"

    # ── 1: Nothing granted, and the report says so per provider ───────
    assert body["provisioning"]["added_count"] == 0
    assert body["provisioning"]["skipped"] == [
        {"provider_id": provider_id, "reason": "user_inactive"}
    ]
    assert body["provisioning"]["provisioning_failed"] is False, (
        "an inactive account is an outcome, not a provisioning failure"
    )
    record = get_managed_credential(client, superuser_token_headers, parent_id)
    assert user_id not in member_user_ids(record)

    # ── 3: The cooldown was not armed by a mail that never left ───────
    invitation = get_invitation(client, superuser_token_headers, user_id)
    assert invitation["send_count"] == 0
    assert invitation["last_sent_at"] is None
    assert invitation["status"] == "pending"

    # The link is dormant: an inactive account cannot redeem it, and the
    # refusal is the same one a forgery gets.
    token = token_of(body["accept_url"])
    assert lookup(client, token).json() == {"valid": False}

    # ── 4: Activation wakes the same link up ──────────────────────────
    activated = client.patch(
        f"{API}/users/{user_id}",
        headers=superuser_token_headers,
        json={"is_active": True},
    )
    assert activated.status_code == 200, activated.text
    assert lookup(client, token).json()["valid"] is True

    password = random_lower_string()
    accepted = accept(client, token, password)
    assert accepted.status_code == 200, accepted.text

    # ── 1, the observable half: their own feed carries no skip events ──
    # ``GET /security-events/`` is self-scoped, so this is only readable once
    # the person can sign in — which is why it is asserted here rather than at
    # the top. The report above names the skipped credential; this says no
    # event was written for it, which is the half of the short-circuit that
    # the report deliberately does not restate.
    own_headers = {"Authorization": f"Bearer {accepted.json()['access_token']}"}
    feed = client.get(
        f"{API}/security-events/",
        headers=own_headers,
        params={"event_type": "admin.ai_credential.auto_provision_failed"},
    )
    assert feed.status_code == 200, feed.text
    assert feed.json()["data"] == [], feed.json()

    # And no *successful* provisioning was invented either — activation does
    # not retroactively grant; "Apply to existing users" is that path.
    granted = client.get(
        f"{API}/security-events/",
        headers=own_headers,
        params={"event_type": "admin.ai_credential.auto_provision"},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["data"] == []


def test_the_inactive_skip_report_names_each_requested_provider_once(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    The shape of the report the wizard renders, across the four requests an
    admin can actually make against a deactivated invitee:
      1. Three ticked providers ⇒ three skips, every one ``user_inactive``,
         ``added_count == 0``, and **not** ``provisioning_failed`` — an
         inactive account is an outcome, not a failure
      2. The same id twice in one submission ⇒ **one** skip. The active path
         grants once (``add_members`` is idempotent and the ids are deduped),
         so the inactive path must report once; two skips would be the wizard
         showing a provider the admin cannot see twice anywhere else
      3. ``provider_ids: []`` ⇒ no skips. The admin unticked
         everything, so nothing was requested and there is nothing to name
      4. ``provider_ids: null`` ⇒ no skips either, and this is a
         *different* branch from 3 rather than a spelling of it: ``null`` means
         "grant whatever this role would have got automatically", the set is
         not known without the query the short-circuit exists to skip, and the
         report can only ever name credentials it was handed
      5. Still no per-provider security events, with three requested rather
         than the one its sibling test above uses — a per-provider emitter
         would write three rows into the feed of an account that has done
         nothing

    Cases 3 and 4 are the pair a reviewer caught rendering identically in the
    wizard. They *are* identical in the report, and that is the correct
    answer; what this pins is the backend half, so a later change that starts
    naming the automatic set on the inactive path has to break a test.
    """
    providers = [
        create_provider_credential(
            db,
            credential_type="anthropic",
            auto_provision_roles=["agent-user"],
        )["provider_id"]
        for _ in range(3)
    ]

    # ── 1: one skip per requested credential ──────────────────────────
    three = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        provider_ids=providers,
        is_active=False,
    )
    report = three["provisioning"]
    assert report["added_count"] == 0, report
    assert report["provisioning_failed"] is False, report
    assert len(report["skipped"]) == 3, report
    assert {skip["provider_id"] for skip in report["skipped"]} == set(
        providers
    ), report
    assert {skip["reason"] for skip in report["skipped"]} == {"user_inactive"}, (
        report
    )

    # ── 2: a duplicated id is still one skip ──────────────────────────
    duplicated = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        provider_ids=[providers[0], providers[0]],
        is_active=False,
    )
    assert duplicated["provisioning"]["skipped"] == [
        {"provider_id": providers[0], "reason": "user_inactive"}
    ], duplicated["provisioning"]

    # ── 3: nothing ticked, nothing to name ────────────────────────────
    unticked = invite_user(
        client,
        superuser_token_headers,
        email=random_email(),
        role="agent-user",
        provider_ids=[],
        is_active=False,
    )
    assert unticked["provisioning"]["skipped"] == []
    assert unticked["provisioning"]["added_count"] == 0
    assert unticked["provisioning"]["provisioning_failed"] is False

    # ── 4: the automatic set, on the wire as an explicit null ─────────
    # Posted inline rather than through the helper: this phase is about the
    # bytes the wizard sends for "I did not choose a set".
    silent = client.post(
        f"{API}/users/invite",
        headers=superuser_token_headers,
        json={
            "email": random_email(),
            "role": "agent-user",
            "send_email": False,
            "is_active": False,
            "provider_ids": None,
        },
    )
    assert silent.status_code == 200, silent.text
    assert silent.json()["provisioning"]["skipped"] == []
    assert silent.json()["provisioning"]["added_count"] == 0
    assert silent.json()["provisioning"]["provisioning_failed"] is False

    # ── 5: three requested, and still not one event ───────────────────
    activated = client.patch(
        f"{API}/users/{three['user']['id']}",
        headers=superuser_token_headers,
        json={"is_active": True},
    )
    assert activated.status_code == 200, activated.text
    accepted = accept(client, token_of(three["accept_url"]), random_lower_string())
    assert accepted.status_code == 200, accepted.text
    own_headers = {"Authorization": f"Bearer {accepted.json()['access_token']}"}
    failures = client.get(
        f"{API}/security-events/",
        headers=own_headers,
        params={"event_type": "admin.ai_credential.auto_provision_failed"},
    )
    assert failures.status_code == 200, failures.text
    assert failures.json()["data"] == [], failures.json()


# ── The users list ─────────────────────────────────────────────────────


def test_the_users_list_reports_invitation_status_for_a_mixed_page(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    ``GET /users/`` carries ``invitation_status`` for every row:
      1. A pending invitee reads ``pending``
      2. A revoked one reads ``revoked``
      3. An expired one reads ``expired``
      4. An accepted one reads ``accepted``
      5. An account that was never invited reads ``null`` — never invited is
         not a status, and the field defaults so the ten other ``UserPublic``
         producers keep working (D32/S7)

    …and it costs **one** ``user_invitation`` query to say all of that, for a
    page of any size. ``read_users`` builds the map with a single ``IN`` over
    the page's ids rather than a lookup per row, and this is the assertion
    that keeps it that way: the list is the projection this codebase has
    shipped N+1s into before (``_assignment_to_public``, and the
    ``state.unloaded`` shortcut ``_user_public.py``'s own comments call out as
    turning exactly this endpoint into one), and it has since grown a second
    batched lookup. The page is padded well past the five interesting rows so
    a per-row implementation is off by an order of magnitude rather than by
    four.

    The count and the statuses are asserted about the **same request**, on
    purpose. A count alone passes if the feature stops working entirely — a
    ``status_map`` that ran its one query and then dropped the result would
    satisfy it, and every row would read ``null`` — and the statuses alone
    pass a refactor that answers them one user at a time.
    """
    pending = invite_user(client, superuser_token_headers, email=random_email())

    revoked = invite_user(client, superuser_token_headers, email=random_email())
    revoke_invitation(client, superuser_token_headers, revoked["user"]["id"])

    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        expired = invite_user(
            client, superuser_token_headers, email=random_email()
        )

    accepted_body, accepted_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    assert accept(client, accepted_token, random_lower_string()).status_code == 200

    never_invited, _ = create_random_user_with_headers(client)

    # Padding: invited accounts are passwordless, so twenty of them are cheap
    # and every one of them is a row ``status_map`` has to answer for.
    for _ in range(20):
        invite_user(client, superuser_token_headers, email=random_email())

    with count_queries(db, matching="user_invitation") as invitation_queries:
        listed = client.get(
            f"{API}/users/", headers=superuser_token_headers, params={"limit": 200}
        )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["data"]) >= 26, "the page is not the padded one"
    assert len(invitation_queries) == 1, (
        f"{len(invitation_queries)} user_invitation queries for one page of "
        f"users — status_map is meant to be one IN over the page: "
        f"{invitation_queries}"
    )
    by_id = {row["id"]: row for row in listed.json()["data"]}

    expected = {
        pending["user"]["id"]: "pending",
        revoked["user"]["id"]: "revoked",
        expired["user"]["id"]: "expired",
        accepted_body["user"]["id"]: "accepted",
        never_invited["id"]: None,
    }
    for user_id, status in expected.items():
        assert user_id in by_id, f"{user_id} missing from the page"
        assert by_id[user_id]["invitation_status"] == status, (
            f"{user_id}: expected {status}, got "
            f"{by_id[user_id]['invitation_status']}"
        )

    # The seeded superuser was never invited either — the projection must not
    # invent a status for the account that predates the feature.
    superuser_rows = [
        row for row in by_id.values() if row["email"] == settings.FIRST_SUPERUSER
    ]
    assert superuser_rows and superuser_rows[0]["invitation_status"] is None

    # S7: the field is optional, so the single-row producers still answer.
    assert client.get(f"{API}/users/me", headers=superuser_token_headers).status_code == 200
    assert (
        client.post(
            f"{API}/login/test-token", headers=superuser_token_headers
        ).status_code
        == 200
    )


# ── A declined default slot is disclosed, not reported as a failure ─────


#: The statement that fails to leave an *interrupted* invite behind: the
#: account row has committed and the invitation row has not. Written out rather
#: than imported, for the same reason the other injection constants in this file
#: are — it is a contract with the database, and a rename must break a test.
_INVITATION_INSERT = "INSERT INTO user_invitation"


def _interrupted_invite(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> dict:
    """Leave the wreckage of a half-finished invite behind, and return its row.

    The account row commits — **and so does its provisioning**, which is the
    reason this shape is the natural setup here rather than a contrivance:
    ``invite`` provisions before it writes the invitation, so the abandoned
    account already holds whatever its role was automatically granted. That is
    exactly the account a re-invite then runs ``provision_explicit`` against.

    The narrower version of this helper in
    ``users_invitation_audit_and_omission_test.py`` is deliberately not shared:
    that one is about the *adoption* rules and takes a name, this one is about
    what the account was left holding.
    """
    email = random_email()
    with failing_sql_statement(
        db, when_statement_contains=_INVITATION_INSERT
    ) as injected:
        with pytest.raises(Exception):
            client.post(
                f"{API}/users/invite",
                headers=superuser_token_headers,
                json={
                    "email": email,
                    "role": "agent-user",
                    "send_email": False,
                },
            )
    assert injected["fired"], "the injection never fired — nothing was proved"
    restore_session(db)

    listing = client.get(
        f"{API}/users/", headers=superuser_token_headers, params={"limit": 200}
    )
    assert listing.status_code == 200, listing.text
    matches = [
        row for row in listing.json()["data"] if row["email"] == email
    ]
    assert matches, f"no account was left behind for {email}"
    row = matches[0]
    assert row["invitation_status"] is None, (
        "the wreckage carries an invitation row, so the re-invite below would "
        "be refused as a duplicate instead of adopting the account"
    )
    return row


def test_a_provider_whose_wiring_a_reinvited_account_already_holds_is_disclosed_not_failed(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The grant landed; only the SDK wiring did not. Say so, as a note.

    THE PATH IS ORDINARY, WHICH IS WHY THE FIELD EXISTS
    ---------------------------------------------------
    ``InvitationService.invite`` calls ``provision_explicit`` **unconditionally**,
    after the branch that adopts an account which already exists. So a re-invite
    — an interrupted earlier attempt, or the passwordless account a server
    channel created for an inbound sender — runs explicit provisioning against
    somebody who may already hold a default in that mode. **One ticked provider
    is enough.** §5.5's uniqueness rule does not protect this: it guards two
    providers claiming the same ``(role, mode)`` *configuration*, not one
    provider meeting one person's occupied slot.

    Here the first, abandoned attempt granted the account its role's automatic
    provider and claimed the conversation slot for it. The re-invite ticks a
    second provider that also wires conversation, and that provider's wiring is
    the thing that silently did not happen.

    THE TWO NEGATIVES ARE THE POINT
    -------------------------------
    ``provisioning_failed`` must stay False and ``skipped`` must stay empty. A
    skip means "this person did not receive the key"; the opposite happened, and
    rendering it as a skip would report a successful grant as a failure. Both
    are asserted rather than assumed, because a later refactor that folded this
    into ``skipped`` would still satisfy an assertion that only looked at
    ``default_slot_skips``.
    """
    # The role's automatic provider: it wires the conversation SDK default, so
    # the abandoned first attempt leaves the account holding one.
    incumbent = create_provider_credential(
        db,
        name="Incumbent Anthropic",
        credential_type="anthropic",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        set_as_default=True,
    )

    # The provider the admin ticks on the re-invite. It auto-provisions nobody,
    # which is what keeps §5.5's write-time 409 out of this test — that rule
    # would refuse a second provider claiming ``(agent-user, conversation)``,
    # and the whole point here is a collision no configuration check can see.
    latecomer = create_provider_credential(
        db,
        name="Latecomer OpenAI",
        credential_type="openai",
        auto_provision_roles=[],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )

    row = _interrupted_invite(client, superuser_token_headers, db)
    # The control for the whole test: the abandoned attempt really did leave a
    # default behind. Without this, a green run below could mean "the slot was
    # empty and nothing was declined".
    assert member_user_ids(
        get_managed_credential(
            client, superuser_token_headers, incumbent["record"]["id"]
        )
    ) == {row["id"]}, (
        "the interrupted attempt granted nothing, so there is no incumbent "
        "default for the re-invite to collide with"
    )

    reinvited = invite_user(
        client,
        superuser_token_headers,
        email=row["email"],
        role="agent-user",
        is_active=None,
        provider_ids=[latecomer["provider_id"]],
    )
    assert reinvited["adopted_existing_account"] is True, (
        "the account was not adopted, so this test asserts nothing about the "
        "re-invite path"
    )

    provisioning = reinvited["provisioning"]
    assert provisioning["added_count"] == 1, provisioning
    assert provisioning["skipped"] == [], (
        "a declined slot was reported as a skipped grant — the person did "
        "receive the key"
    )
    assert provisioning["provisioning_failed"] is False, (
        "a declined slot set the total-failure flag; nothing fell over"
    )
    assert provisioning["default_slot_skips"] == [
        {
            "provider_id": latecomer["provider_id"],
            "mode": "conversation",
        }
    ], provisioning

    # And the disclosure is true: the account still holds the incumbent's
    # credential in that slot, not the latecomer's.
    detail = client.get(
        f"{API}/users/{row['id']}", headers=superuser_token_headers
    )
    assert detail.status_code == 200, detail.text
    incumbent_members = {
        member["user_id"]: member
        for member in get_managed_credential(
            client, superuser_token_headers, incumbent["record"]["id"]
        )["members"]
    }
    assert (
        detail.json()["default_ai_credential_conversation_id"]
        == incumbent_members[row["id"]]["child_credential_id"]
    ), (
        "the latecomer took the slot after all — then there was nothing to "
        "disclose and this test is asserting a lie"
    )
