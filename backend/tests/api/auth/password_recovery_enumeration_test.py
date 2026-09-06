"""``POST /password-recovery/{email}``: one answer, and what it hides.

REPLACES THE 404 CONTRACT, AND REVERSES THE DECISION BEHIND IT
--------------------------------------------------------------
This file supersedes five tests that pinned the old behaviour:
``test_login.py::test_recovery_password`` and
``::test_recovery_password_user_not_exits``, and, in
``test_email_confirmation.py``, ``::test_password_recovery_cooldown_silent``,
``::test_password_recovery_unknown_email_still_404`` and
``::test_password_recovery_works_for_unconfirmed_user``. Each encoded either
the ``404`` for an unknown address or the old
``"Password recovery email sent"`` body.

The ``404`` was a **deliberate** decision, recorded in the docstring of
``test_password_recovery_unknown_email_still_404`` as "D7 decision: keep 404
for unknown emails on the recovery endpoint; only the new resend-confirmation
endpoint is non-enumerating". **That decision is reversed.** It was made when
recovery was reasoned about on its own, where the argument "the address is
already known to whoever typed it" is at least arguable. It does not survive
what the surface became: an anonymous caller could ask this endpoint about any
address and read the account's existence off the status code, with no rate
limit and no authentication. Zero-touch onboarding then multiplied the
population it discloses — every invited-but-unaccepted account is a real,
active row — and put a second, worse oracle right next to it, so keeping one
open would have made the other's defence pointless. The endpoint now answers
``200`` with one generic body for every address and every outcome.

WHY THE SEND COUNT IS ASSERTED AND NOT THE RESPONSE
---------------------------------------------------
That uniformity is the whole design, and it is also why a response assertion
proves almost nothing here: refusing to send and sending happily produce the
*same* 200 and the same body. Every test below that cares whether mail went
out asserts on ``send_email``'s call count, patched at
``app.services.users.user_service.send_email`` — the binding the service
resolved at import time. ``patch("app.utils.send_email")`` would not intercept
it, and the test container runs mailcatcher, so a live failure in the mail path
is absorbed silently and shows up as green.

WHAT IS STILL OPEN, AND IS NOT THIS FILE'S TO CLOSE
---------------------------------------------------
Response *time*. A real send does a synchronous SMTP round-trip inside the
request, so a first probe of a registered address is measurably slower — and
the 300 s cooldown then makes the second probe fast, so slow-then-fast reads as
"this address exists". Timing is not asserted here because it cannot be
asserted reliably in this harness; it is recorded so the next reader does not
mistake a green file for a closed channel.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings

# Token minting only — the README's named Rule 1 exemption for password-reset
# tests. There is no HTTP surface that hands a caller a reset token.
from app.utils import generate_password_reset_token
from tests.utils.google_oauth import login_with_google, random_google_id
from tests.utils.invitation import (
    RECOVERY_MESSAGE,
    accept,
    assert_one_distinct_response,
    fresh_invitation_limiter,
    invite_and_token,
    recovery_email_patched,
)
from tests.utils.server_config import set_access_policy
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch) -> None:
    """The invitation setup here touches the anonymous routes (seam S9)."""
    fresh_invitation_limiter(monkeypatch)


def _recover(client: TestClient, email: str):
    return client.post(f"{API}/password-recovery/{email}")


def _login(client: TestClient, email: str, password: str):
    return client.post(
        f"{API}/login/access-token",
        data={"username": email, "password": password},
    )


# ── The uniform answer ─────────────────────────────────────────────────


def test_password_recovery_answers_every_address_and_outcome_identically(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Ten situations, one ``(status, body)``:
      1. An address with no account at all
      2. An ordinary account
      3. An account still in the 300 s cooldown
      4. An unconfirmed account (recovery is never gated on confirmation)
      5. A deactivated account
      6. An account with a *revoked* invitation, never claimed
      7. An account with a *pending* invitation
      8. An account claimed through Google
      9. ``send_email`` raising inside the request
     10. An instance with no SMTP configured at all
     11. A non-superuser on a Google-only instance
     12. The superuser on that same instance (the break-glass — still sends)

    Cases 9 and 10 are the regression guards for a widened ``try``: everything
    that runs only for a *known* address — the template render and the cooldown
    commit as much as the SMTP call — sits inside it, because a 500 for a known
    address next to a 200 for an unknown one is the same oracle by another
    route.

    Case 11-12 must not split either. Refusing loudly for a non-superuser would
    identify which addresses belong to administrators, since administrators
    keep the break-glass path.
    """
    responses = []

    # ── 1-5: The ordinary population ──────────────────────────────────
    plain, _ = create_random_user_with_headers(client)
    unconfirmed, _ = create_random_user_with_headers(client)
    assert unconfirmed["email_confirmed"] is False
    inactive, _ = create_random_user_with_headers(client)
    deactivated = client.patch(
        f"{API}/users/{inactive['id']}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert deactivated.status_code == 200, deactivated.text

    with recovery_email_patched():
        responses.append(_recover(client, random_email()))          # 1
        responses.append(_recover(client, plain["email"]))          # 2
        responses.append(_recover(client, plain["email"]))          # 3 cooldown
        responses.append(_recover(client, unconfirmed["email"]))    # 4
        responses.append(_recover(client, inactive["email"]))       # 5

    # ── 6-8: The invited population ───────────────────────────────────
    revoked_body, _ = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    revoked = client.post(
        f"{API}/users/{revoked_body['user']['id']}/invitation/revoke",
        headers=superuser_token_headers,
    )
    assert revoked.status_code == 200, revoked.text

    pending_body, _ = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )

    google_email = random_email()
    invite_and_token(client, superuser_token_headers, email=google_email)
    login_with_google(client, email=google_email, google_id=random_google_id())

    with recovery_email_patched():
        responses.append(_recover(client, revoked_body["user"]["email"]))   # 6
        responses.append(_recover(client, pending_body["user"]["email"]))   # 7
        responses.append(_recover(client, google_email))                    # 8

    # ── 9-10: The mail path itself failing or absent ──────────────────
    exploder, _ = create_random_user_with_headers(client)
    with recovery_email_patched(side_effect=RuntimeError("SMTP is down")):
        responses.append(_recover(client, exploder["email"]))               # 9
    silent, _ = create_random_user_with_headers(client)
    with recovery_email_patched(emails_enabled=False) as never_called:
        responses.append(_recover(client, silent["email"]))                 # 10
        assert never_called.call_count == 0, (
            "emails_enabled short-circuits before the send; a call here means "
            "the gate moved"
        )

    # ── 11-12: Google-only mode, last because it is instance-wide ─────
    google_only_user, _ = create_random_user_with_headers(client)
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
    with recovery_email_patched() as mail:
        responses.append(_recover(client, google_only_user["email"]))       # 11
        assert mail.call_count == 0, "a non-superuser was mailed a reset link"
        responses.append(_recover(client, settings.FIRST_SUPERUSER))        # 12
        assert mail.call_count == 1, "the superuser break-glass stopped working"

    # ── The assertion ─────────────────────────────────────────────────
    assert len(responses) == 12
    assert_one_distinct_response(responses, "POST /password-recovery/{email}")
    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json() == {"message": RECOVERY_MESSAGE}
        assert "detail" not in response.json()


# ── What the uniform answer is hiding ──────────────────────────────────


def test_recovery_sends_once_for_a_registered_address_and_never_for_an_unknown_one(
    client: TestClient
) -> None:
    """The send count is the only observable difference, and it must be right.

    Also pins the cooldown: a second request inside the window answers exactly
    the same and sends nothing, which is what makes the endpoint safe to
    hammer and is also why the count is asserted rather than the body.
    """
    user, _ = create_random_user_with_headers(client)

    with recovery_email_patched() as mail:
        first = _recover(client, user["email"])
        assert mail.call_count == 1, "the patch did not intercept the send"
        # Proof the interception is real and not an empty render.
        assert mail.call_args.kwargs["email_to"] == user["email"]
        assert mail.call_args.kwargs["html_content"]

        second = _recover(client, user["email"])
        assert mail.call_count == 1, "the cooldown did not suppress the resend"

        stranger = _recover(client, random_email())
        assert mail.call_count == 1, "an unregistered address was mailed"

    assert_one_distinct_response(
        [first, second, stranger], "registered vs cooldown vs unknown"
    )


def test_recovery_is_refused_for_an_unclaimed_account_whose_invitation_ended(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Revocation has to mean the account cannot be claimed.

    An invited account is a real, active row with no password from the moment
    it is created. Without this rule, revoking an invitation stops the *link*
    working and nothing else: the person the admin just un-invited types their
    address into "forgot password", gets a genuine reset link, sets a password
    and is in — while the admin's list still reads ``revoked``.

    A *pending* invitation is the control. It must still receive recovery, or
    "I never got the invite email" would have no exit at all. The responses are
    identical across all three, so the count is the assertion.
    """
    revoked_body, _ = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )
    client.post(
        f"{API}/users/{revoked_body['user']['id']}/invitation/revoke",
        headers=superuser_token_headers,
    )

    with patch.object(settings, "INVITATION_EXPIRE_DAYS", 0):
        expired_body, _ = invite_and_token(
            client, superuser_token_headers, email=random_email()
        )

    pending_body, _ = invite_and_token(
        client, superuser_token_headers, email=random_email()
    )

    with recovery_email_patched() as mail:
        revoked_response = _recover(client, revoked_body["user"]["email"])
        assert mail.call_count == 0, "a revoked invitee was mailed a reset link"
        expired_response = _recover(client, expired_body["user"]["email"])
        assert mail.call_count == 0, "an expired invitee was mailed a reset link"
        pending_response = _recover(client, pending_body["user"]["email"])
        assert mail.call_count == 1, (
            "a pending invitee must still be able to use forgot-password"
        )

    assert_one_distinct_response(
        [revoked_response, expired_response, pending_response],
        "revoked vs expired vs pending invitee",
    )

    # And the refusal is not decorative: a reset token minted for the revoked
    # address is refused by ``POST /reset-password/`` too, with the same body a
    # forged token gets.
    blocked = client.post(
        f"{API}/reset-password/",
        json={
            "new_password": random_lower_string(),
            "token": generate_password_reset_token(
                email=revoked_body["user"]["email"]
            ),
        },
    )
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"] == "Invalid token"


def test_recovery_still_reaches_an_account_that_was_claimed_through_google(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The regression guard for the fix that just landed.

    An invitation accepted through Google leaves the row ``accepted`` with
    ``hashed_password`` still null. Gating the refusal on the password column
    alone caught those accounts: they read ``accepted`` in the admin list, asked
    for a reset, and silently got nothing — with no way to learn that
    ``POST /users/me/set-password`` while signed in was the only route to a
    password. The predicate asks whether the account was ever *claimed*
    (a password hash **or** a linked Google identity), so it expires on its own
    the moment someone signs in.

    The accepted-with-a-password case is here too, as the other half of
    "claimed accounts are permanently exempt".
    """
    # ── Claimed through Google ────────────────────────────────────────
    google_email = random_email()
    invite_and_token(client, superuser_token_headers, email=google_email)
    login_with_google(client, email=google_email, google_id=random_google_id())

    # ── Claimed with a password ───────────────────────────────────────
    password_email = random_email()
    _, token = invite_and_token(
        client, superuser_token_headers, email=password_email
    )
    chosen = random_lower_string()
    assert accept(client, token, chosen).status_code == 200

    with recovery_email_patched() as mail:
        google_response = _recover(client, google_email)
        assert mail.call_count == 1, (
            "a Google-accepted invitee was refused password recovery"
        )
        password_response = _recover(client, password_email)
        assert mail.call_count == 2, (
            "an account in daily use lost recovery to its own accepted invitation"
        )

    assert_one_distinct_response(
        [google_response, password_response], "google- vs password-claimed"
    )
    # The password account is genuinely usable, so this is not a vacuous pass.
    assert _login(client, password_email, chosen).status_code == 200
