"""One answer, for every reason. The phase's central risk, proved not asserted.

WHY THE COMPARISONS ARE AGAINST EACH OTHER
------------------------------------------
The caller supplies the token, so any difference between the answer a *forged*
token gets and the answer a *real* one gets is a direct answer to "does this
address have an invited account here". The tests below therefore collect every
rejection into a list and assert the list contains exactly **one** distinct
``(status_code, raw body)``.

They compare the responses to *each other*, never to a hardcoded literal, and
that is deliberate. A future copy-edit to the refusal string is the likely way
this contract breaks; a test written as "each response equals
``{"detail": "This invitation is no longer valid"}``" would be edited in the
same pass and would keep passing while the set split in two. Comparing the set
to itself cannot be repaired by editing a constant.

Raw ``response.content`` bytes, not parsed JSON, for the same reason: key
order, whitespace and a null-versus-absent field are all differences a client
can see and ``==`` on two dicts cannot.

THE ABSENCE OF ``password_accepted``
------------------------------------
The invalid body is ``{"valid": false}`` and ``password_accepted`` must be
**absent**, not ``false``. A field that is always present with a boolean value
is itself a channel: on an instance that allows password auth, ``true`` versus
absent separates a real token from a forged one. That is asserted as
``set(body) == {"valid"}`` so a later "helpful" field addition fails here
rather than shipping.

WHAT IS DELIBERATELY NOT DEFENDED
---------------------------------
Cases 1–3 are refused before any database access; the rest cost one indexed
SELECT. That timing difference separates "forged" from "signed by this server",
which is not a secret — anyone who can produce a server-signed token already
holds one. No constant-time floor is asserted, on purpose.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings

# Token minting only — the README's named Rule 1 exemption. Every one of these
# mints a shape that has no HTTP surface: a cross-purpose token, or a token
# whose ``exp`` deliberately disagrees with its invitation row's.
from app.utils import (
    generate_email_confirmation_token,
    generate_invitation_token,
    generate_password_reset_token,
)
from tests.utils.google_oauth import login_with_google, random_google_id
from tests.utils.invitation import (
    INVITATION_INVALID_DETAIL,
    accept,
    assert_one_distinct_response,
    claims_of,
    foreign_signed_token,
    fresh_invitation_limiter,
    get_invitation,
    invitation_email_patched,
    invite_and_token,
    lookup,
    recovery_email_patched,
)
from tests.utils.server_config import set_access_policy
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch) -> None:
    """Seam S9. Without this the second enumeration test trips the limiter and
    the failure reads like a logic bug rather than shared process state."""
    fresh_invitation_limiter(monkeypatch)


# ── Building the rejection corpus ──────────────────────────────────────


def _unresolvable_tokens(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> list[tuple[str, str]]:
    """``(label, token)`` for every reason a token does not redeem.

    The labels only ever appear in a failure message — the test asserts the
    responses are indistinguishable, so it must not be able to tell them apart
    either.
    """
    cases: list[tuple[str, str]] = []

    # 1-2. Nothing the server ever minted.
    cases.append(("garbage", "not-a-jwt"))
    cases.append(("empty", ""))

    # 3. Right shape, right claims, wrong signing key.
    cases.append(("foreign signature", foreign_signed_token(random_email())))

    # 4-5. Cross-purpose replay, both directions into this endpoint. The
    # reset token is the sharper of the two: it carries no ``purpose`` claim
    # at all, so a verifier that only checked the signature would accept it.
    other_email = random_email()
    cases.append(
        ("email-confirm token", generate_email_confirmation_token(other_email))
    )
    cases.append(
        ("password-reset token", generate_password_reset_token(other_email))
    )

    # 6. Server-signed invite token whose jti is not in the table — the shape
    # a rotated-away link has.
    cases.append(
        (
            "unknown jti",
            generate_invitation_token(
                email=other_email,
                jti=str(uuid.uuid4()),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
    )

    # 7. An expired invitation, with a token whose own ``exp`` is still in the
    # future — otherwise the JWT expiry would refuse it and the row-expiry
    # branch would never run.
    expired_email = random_email()
    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        expired_body, expired_token = invite_and_token(
            client, superuser_token_headers, email=expired_email
        )
    assert (
        get_invitation(
            client, superuser_token_headers, expired_body["user"]["id"]
        )["status"]
        == "expired"
    ), "precondition: the row must actually read expired"
    cases.append(
        (
            "expired invitation",
            generate_invitation_token(
                email=expired_email,
                jti=claims_of(expired_token)["jti"],
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
    )

    # 8. Revoked.
    revoked_body, revoked_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    revoked = client.post(
        f"{API}/users/{revoked_body['user']['id']}/invitation/revoke",
        headers=superuser_token_headers,
    )
    assert revoked.status_code == 200, revoked.text
    cases.append(("revoked invitation", revoked_token))

    # 9. Already accepted — the single-use property, seen from outside.
    accepted_email = random_email()
    _, accepted_token = invite_and_token(
        client, superuser_token_headers, email=accepted_email
    )
    claimed = accept(client, accepted_token, random_lower_string())
    assert claimed.status_code == 200, claimed.text
    cases.append(("accepted invitation", accepted_token))

    # 10. The account was deleted — the row went with it via ON DELETE
    # CASCADE, so this is also the proof that cascade is wired.
    deleted_body, deleted_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    deleted = client.delete(
        f"{API}/users/{deleted_body['user']['id']}",
        headers=superuser_token_headers,
    )
    assert deleted.status_code == 200, deleted.text
    cases.append(("deleted user", deleted_token))

    # 11. The admin corrected the address after inviting; the token's ``sub``
    # no longer describes the person the jti resolves to.
    moved_body, moved_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    moved = client.patch(
        f"{API}/users/{moved_body['user']['id']}",
        headers=superuser_token_headers,
        json={"email": random_email()},
    )
    assert moved.status_code == 200, moved.text
    cases.append(("address mismatch", moved_token))

    # 12. Deactivated: a link mailed to an account that cannot sign in.
    inactive_body, inactive_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    deactivated = client.patch(
        f"{API}/users/{inactive_body['user']['id']}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert deactivated.status_code == 200, deactivated.text
    cases.append(("deactivated user", inactive_token))

    return cases


# ── §4.1 lookup ────────────────────────────────────────────────────────


def test_lookup_answers_every_unresolvable_token_identically(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Twelve reasons, one answer:
      1. Build a token for each reason a lookup can fail
      2. Every response is byte-identical to every other one
      3. The body is exactly ``{"valid": false}`` — no ``password_accepted``
      4. Nothing was written and nothing was mailed on any of them
    """
    # ── Phase 1: The corpus ───────────────────────────────────────────
    cases = _unresolvable_tokens(client, superuser_token_headers)
    assert len(cases) == 12, "the corpus shrank; a reason stopped being covered"

    # A live invitation, so the read-only obligation has something to observe.
    witness_body, witness_token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    witness_id = witness_body["user"]["id"]
    before = get_invitation(client, superuser_token_headers, witness_id)

    # ── Phase 2-4: One answer, no side effects ────────────────────────
    with (
        invitation_email_patched() as invite_mail,
        recovery_email_patched() as recovery_mail,
    ):
        responses = [lookup(client, token) for _, token in cases]
        # The valid path must be side-effect-free too.
        valid = lookup(client, witness_token)

    assert_one_distinct_response(responses, "POST /invitations/lookup")
    for (label, _), response in zip(cases, responses, strict=True):
        assert response.status_code == 200, f"{label}: {response.text}"
        body = response.json()
        assert body == {"valid": False}, f"{label}: {body}"
        # Absent, not false. A field that is always present is a channel.
        assert set(body) == {"valid"}, f"{label} leaked {set(body) - {'valid'}}"
        assert "password_accepted" not in body, label

    assert valid.json()["valid"] is True
    assert valid.json()["password_accepted"] is True

    # ── Phase 4: Read-only ────────────────────────────────────────────
    assert invite_mail.call_count == 0
    assert recovery_mail.call_count == 0
    after = get_invitation(client, superuser_token_headers, witness_id)
    assert after == before, "a lookup wrote to the invitation row"


# ── §4.2 accept ────────────────────────────────────────────────────────


def test_accept_answers_every_rejection_identically_including_policy(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The same twelve, plus the one the design exists to fold in:
      1. Build the corpus while password auth is on
      2. Also invite a live, pending **non-superuser**
      3. Turn password auth off, so that live token is refused by *policy*
      4. All thirteen answer with one identical (status, body): 400, one detail
      5. None of them left a password behind

    Case 13 is inside the same set on purpose. Ruling Q1 removed the 403
    branch entirely: a real token on a Google-only instance answering 403 while
    a forged one answers 400 would be a direct oracle, and worse than the
    password-recovery one because the caller controls the input. Asserting it
    in a separate test would let the two drift apart, which is exactly the
    failure being guarded against.
    """
    # ── Phase 1-2: Everything that needs password auth on ─────────────
    cases = _unresolvable_tokens(client, superuser_token_headers)
    live_email = random_email()
    live_body, live_token = invite_and_token(
        client, superuser_token_headers, email=live_email, role="agent-user"
    )
    password = random_lower_string()

    # ── Phase 3: Google-only ──────────────────────────────────────────
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
    assert lookup(client, live_token).json()["password_accepted"] is False

    # ── Phase 4: One answer ───────────────────────────────────────────
    cases = [*cases, ("live token, password auth off", live_token)]
    responses = [accept(client, token, password) for _, token in cases]

    assert_one_distinct_response(responses, "POST /invitations/accept")
    for (label, _), response in zip(cases, responses, strict=True):
        assert response.status_code == 400, f"{label}: {response.text}"
        assert response.json() == {"detail": INVITATION_INVALID_DETAIL}, label

    # ── Phase 5: No password was written by any of them ───────────────
    blocked = client.post(
        f"{API}/login/access-token",
        data={"username": live_email, "password": password},
    )
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"] == "Incorrect email or password"
    assert (
        get_invitation(client, superuser_token_headers, live_body["user"]["id"])[
            "accepted_at"
        ]
        is None
    )


def test_a_too_short_password_is_a_422_about_the_password(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The one refusal that is allowed to be specific, and why.

    ``AcceptInvitationRequest`` bounds the password at 8–128, so a short one is
    a 422 from the request schema before the token is looked at. That is not an
    oracle: the answer does not depend on whether the token is real, which this
    asserts directly by sending the same short password with a forged token.
    """
    _, token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    real = accept(client, token, "short")
    forged = accept(client, foreign_signed_token(random_email()), "short")
    assert real.status_code == 422, real.text
    assert forged.status_code == 422, forged.text
    assert real.content == forged.content

    # The invitation survived the malformed attempt.
    assert lookup(client, token).json()["valid"] is True


# ── Cross-purpose replay, all six directions ───────────────────────────


def test_no_token_is_accepted_by_a_flow_it_was_not_minted_for(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Six directions across three purposes, and the same-purpose control:
      1. invite → reset-password  (the account-takeover vector; load-bearing)
      2. invite → confirm-email
      3. reset  → invitations/lookup + accept
      4. reset  → confirm-email
      5. confirm → invitations/lookup + accept
      6. confirm → reset-password
      7. Each token still works in its own flow

    Direction 1 was live. Password-reset tokens carry no ``purpose`` claim and
    never will (links already in inboxes), so its verifier cannot *require*
    one — it refuses any token that carries one instead. Without that refusal
    an invitee could replay their invite link at ``POST /reset-password/``:
    revocation would stop nothing, resend's jti rotation would invalidate
    nothing, and a leaked invite link would be a week-long takeover primitive.
    """
    # Three separate accounts, so no flow can succeed by accident on state
    # another flow created.
    invited_email = random_email()
    _, invite_token = invite_and_token(
        client, superuser_token_headers, email=invited_email
    )
    reset_user, _ = create_random_user_with_headers(client)
    reset_token = generate_password_reset_token(email=reset_user["email"])
    confirm_user, _ = create_random_user_with_headers(client)
    confirm_token = generate_email_confirmation_token(confirm_user["email"])

    # ── 1-2: The invite token elsewhere ───────────────────────────────
    replayed_reset = client.post(
        f"{API}/reset-password/",
        json={"new_password": random_lower_string(), "token": invite_token},
    )
    assert replayed_reset.status_code == 400, replayed_reset.text
    assert replayed_reset.json()["detail"] == "Invalid token"
    replayed_confirm = client.post(
        f"{API}/confirm-email/", json={"token": invite_token}
    )
    assert replayed_confirm.status_code == 400, replayed_confirm.text

    # ── 3-4: The reset token elsewhere ────────────────────────────────
    assert lookup(client, reset_token).json() == {"valid": False}
    assert accept(client, reset_token, random_lower_string()).status_code == 400
    assert (
        client.post(f"{API}/confirm-email/", json={"token": reset_token}).status_code
        == 400
    )

    # ── 5-6: The confirmation token elsewhere ─────────────────────────
    assert lookup(client, confirm_token).json() == {"valid": False}
    assert accept(client, confirm_token, random_lower_string()).status_code == 400
    replayed_confirm_as_reset = client.post(
        f"{API}/reset-password/",
        json={"new_password": random_lower_string(), "token": confirm_token},
    )
    assert replayed_confirm_as_reset.status_code == 400, replayed_confirm_as_reset.text
    assert replayed_confirm_as_reset.json()["detail"] == "Invalid token"

    # ── 7: The control. Without this the test passes on a build where
    # every verifier is broken and refuses everything. ────────────────
    assert (
        client.post(f"{API}/confirm-email/", json={"token": confirm_token}).status_code
        == 200
    )
    own_reset = client.post(
        f"{API}/reset-password/",
        json={"new_password": random_lower_string(), "token": reset_token},
    )
    assert own_reset.status_code == 200, own_reset.text
    assert lookup(client, invite_token).json()["valid"] is True
    assert accept(client, invite_token, random_lower_string()).status_code == 200


# ── Rate limiting ──────────────────────────────────────────────────────


def test_the_anonymous_invitation_routes_are_rate_limited_per_caller(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Both routes share one budget, because both cost an indexed SELECT.

    The limit is patched down rather than exercised at its real value: the
    point is the ``429`` + ``Retry-After`` shape and the fact that both
    endpoints draw on the same bucket, not the number 30.
    """
    _, token = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )

    with patch.object(settings, "INVITATION_RATE_LIMIT_PER_MIN", 3):
        assert lookup(client, token).status_code == 200
        assert lookup(client, token).status_code == 200
        # The third request is spent on the *other* endpoint, proving the
        # budget is shared rather than per-route.
        assert accept(client, "not-a-jwt", random_lower_string()).status_code == 400
        throttled = lookup(client, token)

    assert throttled.status_code == 429, throttled.text
    assert throttled.json()["detail"] == "Rate limit exceeded"
    assert int(throttled.headers["Retry-After"]) >= 1

    # And the reset fixture means the next test does not inherit this bucket.
    assert lookup(client, token).status_code == 200
