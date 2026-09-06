"""Redeeming an invitation, by every door, and the ways redemption goes wrong.

THE ONE ASSERTION THIS FILE EXISTS FOR
--------------------------------------
``lookup.password_accepted == (accept actually succeeds)``, over the whole
(``password_auth_enabled``, ``is_superuser``) matrix. Phase 3's characteristic
failure mode is two implementations of one policy question drifting apart —
it happened four times during the phase — and the only test that cannot be
fooled by it is one that asserts the two answers *are the same function*.
Anything weaker (checking the lookup against a literal, checking accept against
a literal) passes for both halves of a drifted pair.

``auth_hint`` is proven inert in the same test: it decides which method the
page leads with and nothing else. A hint that could enable or disable a method
would be that second implementation.

THE STALE-PENDING HAZARD
------------------------
``mark_accepted_if_pending`` is non-raising by contract — an invitation write
that fails must never cost someone an authentication they already completed —
so a *claimed* account carrying a ``pending`` invitation is a reachable,
documented, tolerated state. What must not be reachable is redeeming an
invitation into it: that would set a new password on an account already in use
and mint a session token on the premise that a redeeming account cannot have a
second factor. Both directions (password-claimed and Google-claimed) are
covered below, and each asserts the **password is unchanged afterwards** — a
400 alone would also be returned by an implementation that overwrote the
password and then failed on something else.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings

# ``app.utils`` is the one app module ``tests/api/`` may import for token
# minting (README Rule 1). Both helpers here mint a *server-signed* token the
# tests then present to a route — there is no HTTP surface that produces
# either shape on demand.
from app.utils import (
    generate_invitation_token,
    generate_password_reset_token,
    restore_session,
)
from tests.utils.account_provisioning import (
    failing_sql_statement,
    session_is_usable,
)
from tests.utils.google_oauth import (
    google_callback,
    login_with_google,
    random_google_id,
)
from tests.utils.invitation import (
    INVITATION_INVALID_DETAIL,
    accept,
    assert_one_distinct_response,
    claims_of,
    fresh_invitation_limiter,
    get_invitation,
    get_invitation_link,
    invite_and_token,
    lookup,
    resend_invitation,
    revoke_invitation,
    token_of,
)
from tests.utils.server_config import set_access_policy
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR

# The UPDATE ``mark_accepted_if_pending`` issues, and the only one either hook
# site makes against this table. Aimed at by the two X3 tests below.
INVITATION_UPDATE = "UPDATE user_invitation"

# The invitation row's INSERT. Aimed at to reproduce the one state
# ``is_interrupted_invite`` describes: a committed account with no
# invitation to speak for it.
INVITATION_INSERT = "INSERT INTO user_invitation"


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch) -> None:
    """Seam S9 — the anonymous limiter is a module global, not test state."""
    fresh_invitation_limiter(monkeypatch)


def _login(client: TestClient, email: str, password: str):
    return client.post(
        f"{API}/login/access-token",
        data={"username": email, "password": password},
    )


def _disable_password_auth(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Put the instance into Google-only mode, the way an operator would.

    The admin endpoint refuses the switch unless Google OAuth is configured
    *and* some administrator could still sign in with it, so a Google login on
    the superuser's address auto-links first. Same shape as
    ``access_policy_gating_test.py``.
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


# ── The single predicate ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("password_auth_enabled", "role", "auth_hint"),
    [
        # The hint is varied across the matrix on purpose: it must not move
        # the answer in any cell.
        (True, "agent-user", "any"),
        (True, "agent-user", "google"),
        (True, "admin", "any"),
        (False, "agent-user", "any"),
        (False, "agent-user", "password"),
        (False, "admin", "any"),
    ],
)
def test_lookup_password_accepted_equals_whether_accept_succeeds(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    password_auth_enabled: bool,
    role: str,
    auth_hint: str,
) -> None:
    """``password_accepted`` is not a hint about the answer — it *is* the answer.

    One predicate, resolved once server-side
    (``AccessPolicyService.is_password_auth_allowed``), surfaced on the lookup
    and enforced identically on accept. Two implementations cannot drift apart
    if a test asserts they are the same function, so the assertion is written
    as an equivalence and never as two independent expectations.

    The superuser rows are the break-glass: an invited administrator keeps the
    password path even on a Google-only instance, because a broken Google
    configuration must not be able to lock every administrator out.

    ``auth_hint`` is varied inside the same test rather than beside it. It is
    presentational — it decides which method the page leads with — so
    ``google`` on a password instance must still permit a password, and
    ``password`` on a Google-only instance must still refuse a non-superuser.
    """
    email = random_email()
    password = random_lower_string()
    body, token = invite_and_token(
        client,
        superuser_token_headers,
        email=email,
        role=role,
        auth_hint=auth_hint,
    )

    if not password_auth_enabled:
        _disable_password_auth(client, superuser_token_headers)

    # ── The lookup's answer ───────────────────────────────────────────
    looked_up = lookup(client, token)
    assert looked_up.status_code == 200, looked_up.text
    projection = looked_up.json()
    assert projection["valid"] is True
    claimed = projection["password_accepted"]
    # ``password_auth_enabled`` is deliberately absent from this projection so
    # the client cannot recombine it with anything.
    assert "password_auth_enabled" not in projection

    # ── What the endpoint actually does ───────────────────────────────
    accepted = accept(client, token, password)
    succeeded = accepted.status_code == 200

    # The whole point of the file.
    assert claimed == succeeded, (
        f"lookup said password_accepted={claimed} but accept "
        f"{'succeeded' if succeeded else 'failed'} "
        f"(policy={password_auth_enabled}, role={role}, hint={auth_hint}): "
        f"{accepted.text}"
    )

    # And it is the predicate the README's break-glass decision describes —
    # asserted separately so a matrix where lookup and accept agreed on the
    # *wrong* answer still fails.
    assert claimed is (password_auth_enabled or role == "admin"), (
        f"auth_hint={auth_hint} moved the answer, or the predicate changed"
    )

    if succeeded:
        # A usable token, of the same shape ``POST /login/access-token`` mints.
        payload = accepted.json()
        assert payload["kind"] == "token", payload
        me = client.post(
            f"{API}/login/test-token",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        )
        assert me.status_code == 200, me.text
        assert me.json()["email"] == email
    else:
        # The refusal is the same detail-free 400 a forged token gets, and it
        # left the account exactly as it was: no password was written.
        assert accepted.status_code == 400, accepted.text
        assert accepted.json()["detail"] == INVITATION_INVALID_DETAIL
        blocked = _login(client, email, password)
        assert blocked.status_code == 400, blocked.text
        assert blocked.json()["detail"] == "Incorrect email or password"
        assert (
            get_invitation(client, superuser_token_headers, body["user"]["id"])[
                "accepted_at"
            ]
            is None
        )


# ── Accepting by password ──────────────────────────────────────────────


def test_accept_by_password_claims_the_account_once_and_only_once(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The ordinary journey, and its terminal state:
      1. Invite, look the token up, accept with a password and a full name
      2. The response is the ``LoginToken`` arm of ``LoginResponse``
      3. The address is confirmed — clicking the link proved control of it
      4. The invitation reads ``accepted``
      5. The person can log in with the password they chose
      6. The same token is now inert: a second accept is the generic 400
      7. So is a lookup of it — accepted is indistinguishable from forged
    """
    email = random_email()
    password = random_lower_string()
    body, token = invite_and_token(
        client, superuser_token_headers, email=email
    )
    user_id = body["user"]["id"]

    # ── Phase 1-2: Accept ─────────────────────────────────────────────
    accepted = accept(client, token, password, full_name="Chosen Name")
    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["kind"] == "token"
    headers = {"Authorization": f"Bearer {payload['access_token']}"}

    # ── Phase 3: Confirmed by acceptance ──────────────────────────────
    me = client.post(f"{API}/login/test-token", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email_confirmed"] is True
    assert me.json()["full_name"] == "Chosen Name"

    # ── Phase 4: The row is terminal ──────────────────────────────────
    invitation = get_invitation(client, superuser_token_headers, user_id)
    assert invitation["status"] == "accepted"
    assert invitation["accepted_at"] is not None

    # ── Phase 5: The password works ───────────────────────────────────
    assert _login(client, email, password).status_code == 200

    # ── Phase 6-7: Single use ─────────────────────────────────────────
    replay = accept(client, token, random_lower_string())
    assert replay.status_code == 400, replay.text
    assert replay.json()["detail"] == INVITATION_INVALID_DETAIL
    assert lookup(client, token).json() == {"valid": False}
    # And the replay did not overwrite the password it failed to set.
    assert _login(client, email, password).status_code == 200


def test_revoked_expired_and_mismatched_invitations_cannot_be_accepted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Three refusals that must each leave the account unclaimed.

    Revocation and expiry are the two states an admin can produce; the address
    mismatch is the independent second check that makes ``jti`` resolution
    safe — the token resolves the row, and the address on it must still
    describe the same person, so an admin who corrects a typo'd address
    invalidates the link that went to the wrong inbox.
    """
    password = random_lower_string()

    # ── Revoked ───────────────────────────────────────────────────────
    revoked_email = random_email()
    revoked_body, revoked_token = invite_and_token(
        client, superuser_token_headers, email=revoked_email
    )
    client.post(
        f"{API}/users/{revoked_body['user']['id']}/invitation/revoke",
        headers=superuser_token_headers,
    )
    refused = accept(client, revoked_token, password)
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == INVITATION_INVALID_DETAIL
    assert _login(client, revoked_email, password).status_code == 400

    # ── Expired ───────────────────────────────────────────────────────
    # Driven through the setting rather than by writing the row: the API is
    # the only surface these tests touch, and a zero-day expiry produces a row
    # whose ``expires_at`` is already in the past by the time it is read.
    expired_email = random_email()
    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        expired_body, _ = invite_and_token(
            client, superuser_token_headers, email=expired_email
        )
    # Re-minted with a future ``exp`` so the *row's* expiry is what refuses
    # this, not the JWT's — otherwise the branch under test never runs and the
    # test would pass on an implementation that ignored ``expires_at``.
    live_token_for_expired_row = generate_invitation_token(
        email=expired_email,
        jti=claims_of(token_of(expired_body["accept_url"]))["jti"],
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert (
        get_invitation(
            client, superuser_token_headers, expired_body["user"]["id"]
        )["status"]
        == "expired"
    )
    stale = accept(client, live_token_for_expired_row, password)
    assert stale.status_code == 400, stale.text
    assert stale.json()["detail"] == INVITATION_INVALID_DETAIL
    assert _login(client, expired_email, password).status_code == 400

    # ── Address changed after the invite ──────────────────────────────
    moved_email = random_email()
    moved_body, moved_token = invite_and_token(
        client, superuser_token_headers, email=moved_email
    )
    corrected = random_email()
    patched = client.patch(
        f"{API}/users/{moved_body['user']['id']}",
        headers=superuser_token_headers,
        json={"email": corrected},
    )
    assert patched.status_code == 200, patched.text
    mismatched = accept(client, moved_token, password)
    assert mismatched.status_code == 400, mismatched.text
    assert mismatched.json()["detail"] == INVITATION_INVALID_DETAIL
    assert _login(client, corrected, password).status_code == 400


# ── The case-folding this path depends on ──────────────────────────────


def test_a_mixed_case_invite_address_is_acceptable_end_to_end(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A mixed-case invite address survives storage, lookup, and acceptance.

    WHY THIS TEST EXISTS
    --------------------
    Address *storage* on this platform is lowercase — every account-creation
    path goes through ``create_account``, which normalises with
    ``.strip().lower()``. Address *lookup* elsewhere is case-sensitive:
    ``get_user_by_email`` compares with ``==`` against a case-sensitive
    ``VARCHAR`` (no citext, no case-insensitive collation). Those two facts
    are a live, known defect on the paths that pair them, and fixing it is
    deliberately deferred to its own work item.

    The invite path is safe from that defect **by construction**, and this
    test is the only thing pinning that:

    * ``invite`` passes the submitted address straight through and lets
      ``create_account`` be the single authority on normalisation, so the
      stored row is lowercase no matter what the wizard was handed;
    * the token's ``sub`` is minted from the **stored** ``user.email``, never
      from the request body, so the claim already carries the canonical form;
    * ``_resolve`` finds the invitation **by ``jti``** — ``get_user_by_email``
      is not on this path at all, which is what makes the case-sensitivity
      defect unreachable here;
    * the independent second check folds both sides
      (``token_email.strip().lower() != user.email.strip().lower()``), so a
      legacy mixed-case row cannot mint an invitation nobody can accept.

    THE REFACTOR THIS GUARDS AGAINST
    --------------------------------
    Routing invitation resolution through ``get_user_by_email`` — looking the
    account up by the token's ``sub`` instead of by ``jti`` — reads as a
    simplification and would leave every other test in this suite green. It
    would also make this path inherit the case-sensitivity defect, and it is
    a natural thing for whoever eventually *fixes* that defect to do. So
    would replacing the folded comparison above with a bare ``!=``. Either
    change breaks this test and nothing else.

    The story:
      1. Invite a deliberately mixed-case address
      2. The stored account is the lowercased form
      3. The token's ``sub`` is the stored form, not the submitted one
      4. Looking the token up answers ``valid`` — not the generic panel
      5. Accepting with a password succeeds and returns a working token
      6. The invitation is terminal, and the password logs in

    The converse — an admin *genuinely changing* the address invalidates the
    outstanding link, which is that same second check doing its job — is the
    "Address changed after the invite" phase of
    ``test_revoked_expired_and_mismatched_invitations_cannot_be_accepted``
    above. The pair is the point: fold the case, do not fold the person.
    """
    # ``EmailStr`` lowercases the domain on its own; the local part is what
    # actually reaches the service with its case intact, so the mixed case
    # here is deliberately in both halves.
    submitted = f"Mixed.Case.{random_lower_string()}@Example.COM"
    stored = submitted.lower()
    password = random_lower_string()

    # ── Phase 1-2: Invite → the row is normalised ─────────────────────
    body, token = invite_and_token(
        client, superuser_token_headers, email=submitted
    )
    user_id = body["user"]["id"]
    assert body["user"]["email"] == stored, (
        "create_account is the single normalisation authority; invite must "
        "not store the address the wizard submitted"
    )

    # ── Phase 3: The claim comes from the row, not the request ────────
    assert claims_of(token)["sub"] == stored, (
        "the token is minted from user.email after the commit; a sub equal "
        f"to {submitted!r} would mean it was minted from the request body"
    )

    # ── Phase 4: Lookup resolves it ───────────────────────────────────
    looked_up = lookup(client, token)
    assert looked_up.status_code == 200, looked_up.text
    panel = looked_up.json()
    assert panel["valid"] is True, (
        "a mixed-case invite must not land on the generic invalid panel"
    )
    # The generic refusal is ``{"valid": False}`` and nothing else, so the
    # presence of the masked address is a second, independent way of saying
    # this resolved rather than merely answering 200.
    assert panel["email_masked"]
    assert panel["password_accepted"] is True

    # ── Phase 5: Accept ───────────────────────────────────────────────
    accepted = accept(client, token, password)
    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["kind"] == "token"
    me = client.post(
        f"{API}/login/test-token",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == stored

    # ── Phase 6: Terminal, and the password works ─────────────────────
    invitation = get_invitation(client, superuser_token_headers, user_id)
    assert invitation["status"] == "accepted"
    assert invitation["accepted_at"] is not None
    assert _login(client, stored, password).status_code == 200


# ── jti rotation ───────────────────────────────────────────────────────


def test_resend_rotates_the_jti_and_reading_the_link_does_not(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Seam S10: rotation is the whole security argument for resend.

    Verification resolves the invitation **by ``jti``**, never by address. If
    it resolved by address, "rotating the jti" would invalidate nothing and a
    revoked-then-resent invitation's original link would still work.

    ``GET /users/{id}/invitation/link`` must *not* rotate, for the opposite
    reason: an admin reading the link out over the phone would otherwise kill
    the email the person is about to click. Byte-identity of ``accept_url``
    across two calls is not asserted — ``generate_invitation_token`` stamps an
    ``nbf`` at second resolution, so two calls that straddle a second boundary
    differ in bytes while rotating nothing. The claims are compared instead,
    which is the property that actually matters, plus the observable one: the
    first token still resolves after the second read.
    """
    email = random_email()
    body, first_token = invite_and_token(
        client, superuser_token_headers, email=email
    )
    user_id = body["user"]["id"]

    # ── Reading the link twice rotates nothing ────────────────────────
    link_one = get_invitation_link(client, superuser_token_headers, user_id)
    link_two = get_invitation_link(client, superuser_token_headers, user_id)
    assert link_one.status_code == 200 and link_two.status_code == 200
    claims_one = claims_of(token_of(link_one.json()["accept_url"]))
    claims_two = claims_of(token_of(link_two.json()["accept_url"]))
    for claim in ("jti", "sub", "exp", "purpose"):
        assert claims_one[claim] == claims_two[claim], claim
    # The jti is the one already on the row — the emailed link is untouched.
    assert claims_one["jti"] == claims_of(first_token)["jti"]
    assert lookup(client, first_token).json()["valid"] is True

    # ── Resend rotates ────────────────────────────────────────────────
    # The cooldown is armed off ``last_sent_at``, which only a real send
    # stamps; nothing was mailed above, so this resend is allowed.
    resent = resend_invitation(client, superuser_token_headers, user_id)
    assert resent.status_code == 200, resent.text
    second_token = token_of(resent.json()["accept_url"])
    assert claims_of(second_token)["jti"] != claims_of(first_token)["jti"]

    # The old link is dead — and dead the same way a forgery is.
    assert lookup(client, first_token).json() == {"valid": False}
    dead = accept(client, first_token, random_lower_string())
    assert dead.status_code == 400, dead.text
    assert dead.json()["detail"] == INVITATION_INVALID_DETAIL

    # The new one works, and the account was not claimed by the dead attempt.
    assert lookup(client, second_token).json()["valid"] is True
    assert accept(client, second_token, random_lower_string()).status_code == 200


# ── Accepting through the other doors ──────────────────────────────────


def test_signing_in_with_google_settles_the_invitation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The Google door: the auto-link is the acceptance.

    Nothing about the invite token is involved — the person never clicked it.
    Afterwards the invite link must be inert, because the account is now
    claimed even though no password was ever set.
    """
    email = random_email()
    body, token = invite_and_token(
        client, superuser_token_headers, email=email
    )
    user_id = body["user"]["id"]

    headers = login_with_google(
        client, email=email, google_id=random_google_id()
    )
    me = client.post(f"{API}/login/test-token", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email

    invitation = get_invitation(client, superuser_token_headers, user_id)
    assert invitation["status"] == "accepted"

    # The unused invite link is now indistinguishable from a forgery.
    assert lookup(client, token).json() == {"valid": False}
    spent = accept(client, token, random_lower_string())
    assert spent.status_code == 400, spent.text


# ── The Google door is a CLAIM door, and it is gated like one ──────────
#
# Three doors lead into a never-claimed invited account: the invite link, the
# password-recovery pair, and the Google auto-link. The first two were gated
# from the start; the third was not, and "revocation means the account cannot
# be claimed" was therefore false for anyone who could sign in with Google as
# that address. ``InvitationService.claim_refused`` is now the one predicate
# all three ask.
#
# The tests below are written against the *doors*, not the predicate: a unit
# test of ``claim_refused`` passes just as happily when a door forgets to call
# it, which is precisely the bug that was here.


def test_the_google_door_refuses_a_revoked_invitation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Revoked, then "Sign in with Google" — the door that used to be open.

    The account is real, active and passwordless from the moment of the
    invite, so before the gate the callback found it by address, auto-linked
    the Google identity onto it and returned a token: 200, signed in, with the
    admin's list still reading ``revoked``.

    The second half is the other side of the same rule and is why the test
    does not stop at the 403 — resend re-arms the invitation and the identical
    Google login then succeeds. A gate that refused every invited account
    would pass the first half and break the feature's headline flow.
    """
    email = random_email()
    body, token = invite_and_token(
        client, superuser_token_headers, email=email
    )
    user_id = body["user"]["id"]
    assert revoke_invitation(
        client, superuser_token_headers, user_id
    ).status_code == 200
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "revoked"
    )

    refused = google_callback(
        client, email=email, google_id=random_google_id()
    )
    assert refused.status_code == 403, refused.text
    assert "access_token" not in refused.json()

    # The revocation stands, and nothing was written on the way out.
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "revoked"
    )
    # The old link is still a dud — the refusal did not re-arm anything.
    assert lookup(client, token).json() == {"valid": False}

    # ── The account was not claimed, and the proof is the lookup ──────
    # ``_resolve`` answers ``valid=false`` for an account someone has since
    # claimed. A fresh link reading ``valid=true`` therefore says the row
    # still has neither a password nor a Google identity: the refused
    # callback wrote no ``google_id``.
    revived = resend_invitation(client, superuser_token_headers, user_id)
    assert revived.status_code == 200, revived.text
    new_token = token_of(revived.json()["accept_url"])
    assert lookup(client, new_token).json()["valid"] is True

    # ── And the headline flow is intact ───────────────────────────────
    headers = login_with_google(
        client, email=email, google_id=random_google_id()
    )
    me = client.post(f"{API}/login/test-token", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "accepted"
    )


def test_the_google_door_refuses_an_expired_admin_invitation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Expired is the worse half, and the reason the fix is a predicate.

    Nothing deactivates an account when an offer merely lapses — there is no
    sweeper — so the alternative design (revocation sets ``is_active=False``)
    closes nothing here at all. An ``admin`` invitation that expired
    unanswered left a **superuser** account claimable through Google forever.

    Asserted on an admin invite on purpose: the thing the vulnerability handed
    out was not a session, it was a superuser session.
    """
    email = random_email()
    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        body, _ = invite_and_token(
            client, superuser_token_headers, email=email, role="admin"
        )
    user_id = body["user"]["id"]
    assert body["user"]["is_superuser"] is True
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "expired"
    )

    refused = google_callback(
        client, email=email, google_id=random_google_id()
    )
    assert refused.status_code == 403, refused.text
    assert "access_token" not in refused.json()

    # Expiry is not transitioned by the refusal, and the account is untouched.
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "expired"
    )
    revived = resend_invitation(client, superuser_token_headers, user_id)
    assert revived.status_code == 200, revived.text
    assert (
        lookup(client, token_of(revived.json()["accept_url"])).json()["valid"]
        is True
    )


def test_the_google_claim_refusal_is_the_answer_a_stranger_gets(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The refusal must not become the oracle the rest of this surface avoids.

    A refusal shape of its own would say "this address has an account here,
    and its invitation is not pending" — to an anonymous caller, on the one
    endpoint that reports an outcome rather than a silent 200. So the gate
    raises the *registration* refusal instead: on an invite-only instance a
    revoked invitee, an expired invitee and an address with no account at all
    get one byte-identical answer.

    Compared against each other rather than a literal, for the reason
    :func:`assert_one_distinct_response` documents.
    """
    set_access_policy(
        client, superuser_token_headers, registration_mode="invite_only"
    )

    revoked_email = random_email()
    revoked_body, _ = invite_and_token(
        client, superuser_token_headers, email=revoked_email
    )
    revoke_invitation(
        client, superuser_token_headers, revoked_body["user"]["id"]
    )

    expired_email = random_email()
    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        invite_and_token(
            client, superuser_token_headers, email=expired_email
        )

    responses = [
        google_callback(
            client, email=revoked_email, google_id=random_google_id()
        ),
        google_callback(
            client, email=expired_email, google_id=random_google_id()
        ),
        google_callback(
            client, email=random_email(), google_id=random_google_id()
        ),
    ]
    assert responses[0].status_code == 403, responses[0].text
    assert_one_distinct_response(responses, "google claim refusal")


def _find_user(
    client: TestClient, superuser_token_headers: dict[str, str], email: str
) -> dict | None:
    """The admin list row for ``email``, or ``None``.

    Through the list route rather than a database read, because
    ``invitation_status`` is resolved there and is the thing being asserted.
    """
    response = client.get(
        f"{API}/users/", headers=superuser_token_headers, params={"limit": 200}
    )
    assert response.status_code == 200, response.text
    for row in response.json()["data"]:
        if row["email"] == email:
            return row
    return None


def test_google_still_links_an_unclaimed_account_with_no_invitation_row(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """The population the gate is one line away from locking out forever.

    ``claim_refused`` answers **no** when the account carries no invitation
    row at all, and that clause is not a formality: it is what keeps two real
    groups of unclaimed accounts able to sign in. The wreckage of an invite
    that committed the account and then failed before writing its invitation
    (:meth:`InvitationService.is_interrupted_invite`), and the passwordless row
    a server channel creates for an inbound sender — both have neither a
    password nor a Google identity and neither has an invitation.

    Change that clause to "refuse", or reorder it above the ``is_unclaimed``
    check, and every one of those people is told "this server is invite-only"
    forever — while all three tests above stay green, because every account in
    them *has* an invitation row.

    The wreckage is produced the way it actually happens: the invitation INSERT
    fails at the database, after the account row has already been committed.
    """
    email = random_email()
    with failing_sql_statement(
        db, when_statement_contains=INVITATION_INSERT
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

    # The account is there, unclaimed, with no invitation to speak for it.
    row = _find_user(client, superuser_token_headers, email)
    assert row is not None, "the account row did not survive the failed invite"
    assert row.get("invitation_status") is None
    orphaned = client.get(
        f"{API}/users/{row['id']}/invitation", headers=superuser_token_headers
    )
    assert orphaned.status_code == 404, orphaned.text

    # And Google claims it, exactly as it must.
    headers = login_with_google(
        client, email=email, google_id=random_google_id()
    )
    me = client.post(f"{API}/login/test-token", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email


def test_a_claimed_account_still_links_google_whatever_its_invitation_says(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The gate expires on its own, and this is the assertion that says so.

    ``claim_refused`` checks "claimed" **first**, so an account in daily use is
    permanently exempt however its invitation reads. Adding a Google identity
    to an account that already has a password is an ordinary, supported thing;
    a gate that read the invitation before the row would have taken it away
    from everyone who ever accepted one.
    """
    email = random_email()
    password = random_lower_string()
    body, token = invite_and_token(
        client, superuser_token_headers, email=email
    )
    user_id = body["user"]["id"]
    assert accept(client, token, password).status_code == 200
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "accepted"
    )

    headers = login_with_google(
        client, email=email, google_id=random_google_id()
    )
    me = client.post(f"{API}/login/test-token", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email
    # Both ways in still work: the link did not replace the password.
    assert _login(client, email, password).status_code == 200


def test_resetting_the_password_settles_the_invitation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The "I never got the invite, I'll use forgot-password" door.

    An invited account is a real, active row, so recovery works for it while
    the invitation is ``pending``. Completing the reset proves control of the
    address just as clicking the invite link would, so it settles the row —
    and the route still answers with its ordinary 200.
    """
    email = random_email()
    body, token = invite_and_token(
        client, superuser_token_headers, email=email
    )
    user_id = body["user"]["id"]
    new_password = random_lower_string()

    reset = client.post(
        f"{API}/reset-password/",
        json={
            "new_password": new_password,
            "token": generate_password_reset_token(email=email),
        },
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"message": "Password updated successfully"}

    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "accepted"
    )
    assert _login(client, email, new_password).status_code == 200
    # The invite link is spent even though it was never used.
    assert lookup(client, token).json() == {"valid": False}


# ── X3: bookkeeping must never cost an authentication ──────────────────


def test_a_failed_invitation_write_does_not_fail_the_google_login(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """X3, hook site one — and the ``oauth.py`` blanket ``except`` behind it.

    The callback's body is wrapped in ``except Exception → 400 "OAuth error"``.
    A hook placed inside that net turns an invitation-bookkeeping failure into
    a **failed login for an account Google just vouched for**. The failure is
    injected at the database, not at a patched collaborator, because the
    interesting shape is an *aborted transaction*: a plain Python exception
    exercises the ``try`` and proves nothing about whether ``restore_session``
    runs first in the handler.
    """
    email = random_email()
    body, _ = invite_and_token(client, superuser_token_headers, email=email)
    user_id = body["user"]["id"]

    with failing_sql_statement(
        db, when_statement_contains=INVITATION_UPDATE
    ) as injected:
        response = google_callback(
            client, email=email, google_id=random_google_id()
        )

    assert injected["fired"], "the injection never fired — nothing was proved"
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "token"
    # The session survived: ``restore_session`` ran before anything in the
    # handler touched an expired ORM attribute.
    assert session_is_usable(db)

    # The token is real, and usable.
    me = client.post(
        f"{API}/login/test-token",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert me.status_code == 200, me.text

    # The tolerated outcome: a signed-in person and a stale ``pending`` row
    # the admin can see and resend.
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "pending"
    )


def test_a_failed_invitation_write_does_not_fail_the_password_reset(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """X3, hook site two — and the ``login.py`` ``ValueError → 404`` net.

    ``reset_password``'s handler maps an unrecognised ``ValueError`` to a
    **404**, so a bookkeeping failure raised inside it would answer a
    successful password reset with "the user does not exist". The hook is
    placed after that block *and* is non-raising; this asserts the observable
    consequence of both.
    """
    email = random_email()
    body, _ = invite_and_token(client, superuser_token_headers, email=email)
    user_id = body["user"]["id"]
    new_password = random_lower_string()

    with failing_sql_statement(
        db, when_statement_contains=INVITATION_UPDATE
    ) as injected:
        reset = client.post(
            f"{API}/reset-password/",
            json={
                "new_password": new_password,
                "token": generate_password_reset_token(email=email),
            },
        )

    assert injected["fired"], "the injection never fired — nothing was proved"
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"message": "Password updated successfully"}
    assert session_is_usable(db)

    # The password committed *before* the hook, so it survived the repair.
    assert _login(client, email, new_password).status_code == 200
    assert (
        get_invitation(client, superuser_token_headers, user_id)["status"]
        == "pending"
    )


# ── Stale pending cannot be re-claimed ─────────────────────────────────


def test_a_claimed_account_carrying_a_pending_invitation_cannot_be_reclaimed(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """The password-overwrite vector, reached the only way it is reachable.

    ``_resolve`` refuses on ``is_unclaimed(user)``, a fact about the *row*
    rather than about the invitation, precisely because the invitation can be
    wrong: the two hooks that settle it are non-raising and leave it
    ``pending`` when they fail. Without that check a live invite link is a
    password-overwrite primitive against an account already in use — and the
    accept route mints a session token with no second-factor branch, on the
    premise that a redeeming account cannot have enrolled one.

    Both claiming doors are covered, and each asserts the password afterwards.
    A 400 alone would also be returned by an implementation that overwrote the
    password and then failed on something later.
    """
    # ── Door one: claimed by a password reset ─────────────────────────
    reset_email = random_email()
    reset_body, reset_token = invite_and_token(
        client, superuser_token_headers, email=reset_email
    )
    owners_password = random_lower_string()
    with failing_sql_statement(
        db, when_statement_contains=INVITATION_UPDATE
    ) as injected:
        reset = client.post(
            f"{API}/reset-password/",
            json={
                "new_password": owners_password,
                "token": generate_password_reset_token(email=reset_email),
            },
        )
    assert injected["fired"]
    assert reset.status_code == 200, reset.text
    assert (
        get_invitation(
            client, superuser_token_headers, reset_body["user"]["id"]
        )["status"]
        == "pending"
    ), "precondition: the row must still read pending for this to be the case"

    attackers_password = random_lower_string()
    hijack = accept(client, reset_token, attackers_password)
    assert hijack.status_code == 400, hijack.text
    assert hijack.json()["detail"] == INVITATION_INVALID_DETAIL
    # The password is intact — this is the assertion, not the 400.
    assert _login(client, reset_email, owners_password).status_code == 200
    assert _login(client, reset_email, attackers_password).status_code == 400

    # ── Door two: claimed through Google ──────────────────────────────
    google_email = random_email()
    google_body, google_token = invite_and_token(
        client, superuser_token_headers, email=google_email
    )
    with failing_sql_statement(
        db, when_statement_contains=INVITATION_UPDATE
    ) as injected:
        callback = google_callback(
            client, email=google_email, google_id=random_google_id()
        )
    assert injected["fired"]
    assert callback.status_code == 200, callback.text
    assert (
        get_invitation(
            client, superuser_token_headers, google_body["user"]["id"]
        )["status"]
        == "pending"
    )

    intruders_password = random_lower_string()
    blocked = accept(client, google_token, intruders_password)
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"] == INVITATION_INVALID_DETAIL
    # No password was written onto the Google-only account.
    assert _login(client, google_email, intruders_password).status_code == 400
