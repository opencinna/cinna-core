"""Backend tests for the User Email Confirmation feature.

Coverage:
  1. Signup creates an unconfirmed user — email_confirmed=False visible on UserPublic.
  2. Confirm-email happy path — valid token flips email_confirmed=True; idempotent on
     second confirm.
  3. Confirm-email error paths — invalid token → 400; password-reset token cannot
     confirm (purpose mismatch → 400); unknown user → 404; inactive is 400/403.
  4. Public resend-confirmation — non-enumerating: unknown email returns generic success
     (no 404); known unconfirmed email returns generic success; second call within
     cooldown still returns success (silent rate-limit).
  5. Authenticated resend-confirmation (POST /users/me/resend-confirmation):
       - Already-confirmed user returns generic "already confirmed" success.
       - Unconfirmed user gets resend_available_at in response.
       - Second call within cooldown is silently accepted (no error, no new email).
  6. Password-recovery cooldown — second POST /password-recovery/{email} within 300 s
     silently skips the send but returns the same success; unknown email still 404.
  7. Superuser (seeded) has email_confirmed=True (backfill invariant).
  8. Newly signup-created user has email_confirmed=False.
"""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.utils import generate_email_confirmation_token, generate_password_reset_token
from tests.utils.user import create_random_user_with_headers, user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

_BASE = settings.API_V1_STR

# This module creates no agents or environments — opt out of the heavy stubs
# loaded by the users conftest to keep this file fast.
NEEDS_AGENT_STUBS = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _signup(client: TestClient, email: str | None = None, password: str | None = None) -> dict:
    """Create a user via the public signup API and return body + stashed password."""
    email = email or random_email()
    password = password or random_lower_string()
    r = client.post(f"{_BASE}/users/signup", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    body["_password"] = password
    return body


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    r = client.post(f"{_BASE}/login/access-token", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{_BASE}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario 1 & 7: signup unconfirmed; superuser confirmed ──────────────────


def test_superuser_is_email_confirmed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Seeded superuser has email_confirmed=True (backfill invariant)."""
    me = _me(client, superuser_token_headers)
    assert me["email_confirmed"] is True
    # email_confirmed_at may or may not be populated depending on migration path
    assert "email_confirmed_at" in me


def test_signup_user_is_unconfirmed(client: TestClient) -> None:
    """A freshly signed-up user has email_confirmed=False on UserPublic."""
    user = _signup(client)
    assert user["email_confirmed"] is False
    assert user.get("email_confirmed_at") is None

    # Verify the field persists on GET /users/me
    headers = _login(client, user["email"], user["_password"])
    me = _me(client, headers)
    assert me["email_confirmed"] is False


# ── Scenario 2: Confirm-email happy path ──────────────────────────────────────


def test_confirm_email_happy_path(client: TestClient) -> None:
    """
    Full confirm-email flow:
    1. Sign up → email_confirmed=False.
    2. POST /confirm-email/ with a valid token → 200 + success message.
    3. GET /users/me → email_confirmed=True; email_confirmed_at is set.
    4. POST /confirm-email/ again (idempotent) → 200 (no error).
    """
    # ── Phase 1: signup → unconfirmed ─────────────────────────────────────
    user = _signup(client)
    headers = _login(client, user["email"], user["_password"])
    me = _me(client, headers)
    assert me["email_confirmed"] is False

    # ── Phase 2: confirm with valid token ─────────────────────────────────
    token = generate_email_confirmation_token(email=user["email"])
    r = client.post(f"{_BASE}/confirm-email/", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "message" in body
    assert "confirmed" in body["message"].lower()

    # ── Phase 3: GET /users/me reflects confirmed ─────────────────────────
    me2 = _me(client, headers)
    assert me2["email_confirmed"] is True
    assert me2["email_confirmed_at"] is not None

    # ── Phase 4: idempotent — second confirm is still 200 ─────────────────
    token2 = generate_email_confirmation_token(email=user["email"])
    r2 = client.post(f"{_BASE}/confirm-email/", json={"token": token2})
    assert r2.status_code == 200, r2.text


# ── Scenario 3: Confirm-email error paths ─────────────────────────────────────


def test_confirm_email_invalid_token(client: TestClient) -> None:
    """Invalid token string → 400 with detail 'Invalid token'."""
    r = client.post(f"{_BASE}/confirm-email/", json={"token": "this.is.garbage"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Invalid token"


def test_confirm_email_password_reset_token_rejected(client: TestClient) -> None:
    """A password-reset token must NOT be accepted as an email-confirmation token.

    The purpose claim distinguishes them. The confirmation verifier requires
    purpose='email_confirm'; a reset token carries no purpose → rejected → 400.
    """
    user = _signup(client)
    # Generate a password-RESET token (no purpose claim)
    reset_token = generate_password_reset_token(email=user["email"])
    r = client.post(f"{_BASE}/confirm-email/", json={"token": reset_token})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Invalid token"


def test_confirm_email_unknown_user_token(client: TestClient) -> None:
    """Token for a non-existent email → 404."""
    token = generate_email_confirmation_token(email="nobody@nowhere-test.example.com")
    r = client.post(f"{_BASE}/confirm-email/", json={"token": token})
    assert r.status_code == 404, r.text


# ── Scenario 4: Public resend-confirmation — non-enumerating ─────────────────


def test_public_resend_confirmation_unknown_email_returns_success(client: TestClient) -> None:
    """POST /resend-confirmation/{email} with an unknown email returns generic success.

    Must NOT return 404 (enumeration oracle).
    """
    unknown_email = f"ghost-{random_lower_string()}@nowhere-test.example.com"
    r = client.post(f"{_BASE}/resend-confirmation/{unknown_email}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "message" in body
    # Generic message — does not reveal existence
    assert "email" in body["message"].lower() or "confirmation" in body["message"].lower()


def test_public_resend_confirmation_known_unconfirmed_email(client: TestClient) -> None:
    """POST /resend-confirmation/{email} for a known unconfirmed user returns generic success.

    We patch send_email so no real SMTP is attempted.
    """
    user = _signup(client)

    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.users.email_confirmation_service.send_email") as mock_send,
    ):
        r = client.post(f"{_BASE}/resend-confirmation/{user['email']}")

    assert r.status_code == 200, r.text
    assert "message" in r.json()


def test_public_resend_confirmation_already_confirmed_is_silent(client: TestClient) -> None:
    """POST /resend-confirmation/{email} for an already-confirmed user returns generic success
    (no error, no oracle — same response as unconfirmed).
    """
    user = _signup(client)
    # Confirm the email
    token = generate_email_confirmation_token(email=user["email"])
    client.post(f"{_BASE}/confirm-email/", json={"token": token})

    r = client.post(f"{_BASE}/resend-confirmation/{user['email']}")
    assert r.status_code == 200, r.text


def test_public_resend_confirmation_cooldown_is_silent(client: TestClient) -> None:
    """Two public resend requests within the cooldown window both return 200.

    The second request is silently rate-limited (no email sent, no error exposed).

    NOTE: In the test environment mailcatcher SMTP is active, so signup itself
    sends the confirmation email (force=True) and stamps last_confirmation_email_sent_at.
    To capture that send and correctly reason about the cooldown, the signup is
    performed inside the patch block. Signup produces exactly 1 send; both
    subsequent resend calls are within the 300 s cooldown and are suppressed.
    Total send count == 1 (signup only, both resends suppressed).
    """
    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.users.email_confirmation_service.send_email", mock_send),
    ):
        user = _signup(client)

        r1 = client.post(f"{_BASE}/resend-confirmation/{user['email']}")
        assert r1.status_code == 200, r1.text

        # Second request immediately — still in cooldown
        r2 = client.post(f"{_BASE}/resend-confirmation/{user['email']}")
        assert r2.status_code == 200, r2.text

    # Signup sent exactly 1 email; both resend calls were suppressed by the
    # cooldown that signup's send established. Total == 1.
    assert mock_send.call_count == 1, (
        f"Expected exactly 1 send (signup); both resends should be cooldown-suppressed. "
        f"Got {mock_send.call_count}"
    )


# ── Scenario 5: Authenticated resend-confirmation ────────────────────────────


def test_authenticated_resend_already_confirmed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """POST /users/me/resend-confirmation for an already-confirmed user returns success.

    The superuser is always confirmed; the response should say so.
    """
    r = client.post(f"{_BASE}/users/me/resend-confirmation", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "message" in body
    assert body["resend_available_at"] is None
    assert "already confirmed" in body["message"].lower() or "confirmed" in body["message"].lower()


def test_authenticated_resend_unconfirmed_returns_resend_available_at(
    client: TestClient,
) -> None:
    """POST /users/me/resend-confirmation for an unconfirmed user returns resend_available_at.

    1. Sign up (unconfirmed) — this sends the initial confirmation email and stamps
       last_confirmation_email_sent_at so the resend cooldown is immediately active.
    2. POST /users/me/resend-confirmation → 200; resend_available_at is set (because
       the signup send established the cooldown timestamp).
    3. A second call within the cooldown still returns 200 (no error, no new email).

    NOTE: In the test environment mailcatcher SMTP is active, so signup itself
    sends the confirmation email (force=True). Signup and login are placed inside
    the patch block so the signup send is captured by mock_send. Total send count
    after signup + two resend attempts == 1 (signup only; both resends suppressed
    by the 300 s cooldown that signup established).
    """
    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.users.email_confirmation_service.send_email", mock_send),
    ):
        # ── Phase 1: signup + login ───────────────────────────────────────
        user = _signup(client)
        headers = _login(client, user["email"], user["_password"])

        # ── Phase 2: first resend ─────────────────────────────────────────
        r1 = client.post(f"{_BASE}/users/me/resend-confirmation", headers=headers)
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert "message" in body1
        # resend_available_at must be set — the signup send stamped the cooldown
        # timestamp, so there is a known future time before a new send is allowed.
        assert body1["resend_available_at"] is not None, (
            "Expected resend_available_at to be set (signup stamped cooldown)"
        )

        # ── Phase 3: second call within cooldown → still 200, no new email ─
        r2 = client.post(f"{_BASE}/users/me/resend-confirmation", headers=headers)
        assert r2.status_code == 200, r2.text

    # Signup sent exactly 1 email; both resend calls were cooldown-suppressed.
    assert mock_send.call_count == 1, (
        f"Expected 1 send (signup only); both resends should be cooldown-suppressed. "
        f"Got {mock_send.call_count}"
    )


def test_authenticated_resend_requires_auth(client: TestClient) -> None:
    """POST /users/me/resend-confirmation without auth is rejected."""
    r = client.post(f"{_BASE}/users/me/resend-confirmation")
    assert r.status_code in (401, 403)


# ── Scenario 6: Password-recovery cooldown ────────────────────────────────────


def test_password_recovery_cooldown_silent(client: TestClient) -> None:
    """Second POST /password-recovery/{email} within 300 s skips the send silently.

    Both calls return 200 with the same success message. The caller cannot tell
    whether an email was actually sent.
    """
    user = _signup(client)
    email = user["email"]

    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
        patch("app.services.users.user_service.send_email", mock_send),
    ):
        r1 = client.post(f"{_BASE}/password-recovery/{email}")
        assert r1.status_code == 200, r1.text
        assert r1.json() == {"message": "Password recovery email sent"}

        # Second call immediately (still in cooldown)
        r2 = client.post(f"{_BASE}/password-recovery/{email}")
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"message": "Password recovery email sent"}

    # Only one actual send
    assert mock_send.call_count == 1, (
        f"Expected 1 send; cooldown should suppress second. Got {mock_send.call_count}"
    )


def test_password_recovery_unknown_email_still_404(client: TestClient) -> None:
    """POST /password-recovery/{email} for an unknown email still returns 404.

    This preserves the pre-existing behavior (D7 decision: keep 404 for unknown
    emails on the recovery endpoint; only the new resend-confirmation endpoint is
    non-enumerating).
    """
    unknown = f"ghost-{random_lower_string()}@nowhere-test.example.com"
    r = client.post(f"{_BASE}/password-recovery/{unknown}")
    assert r.status_code == 404, r.text


def test_password_recovery_works_for_unconfirmed_user(client: TestClient) -> None:
    """An unconfirmed user can still trigger password recovery (gate bypass).

    Recovery must never be blocked by email_confirmed=False.
    """
    user = _signup(client)
    assert user["email_confirmed"] is False

    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
        patch("app.services.users.user_service.send_email", mock_send),
    ):
        r = client.post(f"{_BASE}/password-recovery/{user['email']}")

    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Password recovery email sent"}
    assert mock_send.call_count == 1
