"""Backend tests for the outbound-email gate tied to email_confirmed.

Coverage:
  1. Unconfirmed user does NOT receive system notifications (session_error email
     suppressed by the central gate in notification_service.notify).
  2. Confirmed user DOES receive system notifications.
  3. Unconfirmed user password-recovery email IS sent (gate bypass — recovery always
     allowed regardless of email_confirmed status).

The gate is the single choke point in SystemNotificationService.notify():
  if not EmailConfirmationService.is_outbound_email_allowed(user): return

Tests exercise the gate end-to-end through ActivityService / SystemNotificationService
(the same path used by the session_error notification flow), mirroring the patterns
in test_notification_settings.py.

Note: The superuser is always email_confirmed=True (backfill). All other tests
that use the superuser as owner pass through the gate freely. The tests here
create fresh users so we can control their confirmed/unconfirmed state.
"""
import asyncio
import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.sessions.session import Session as ChatSession
from app.models.agents.agent import Agent
from app.services.events.activity_service import ActivityService
from app.utils import generate_email_confirmation_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import (
    create_random_user_with_headers,
    promote_to_developer,
)
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential


# ── Helpers ──────────────────────────────────────────────────────────────────


def _create_chat_session(db: Session, user_id: uuid.UUID, agent_id: uuid.UUID) -> ChatSession:
    """Create a minimal ChatSession row bypassing the API (mirrors test_notification_settings.py)."""
    session_row = ChatSession(
        user_id=user_id,
        agent_id=agent_id,
        title="Test session",
        status="active",
    )
    db.add(session_row)
    db.commit()
    db.refresh(session_row)
    return session_row


def _confirm_user_email(client: TestClient, email: str) -> None:
    """Confirm a user's email by calling POST /confirm-email/ with a freshly minted token."""
    token = generate_email_confirmation_token(email=email)
    r = client.post(f"{settings.API_V1_STR}/confirm-email/", json={"token": token})
    assert r.status_code == 200, f"Failed to confirm email for {email}: {r.text}"


# ── Scenario 1: Unconfirmed user — notification suppressed ───────────────────


def test_unconfirmed_user_notification_suppressed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    An unconfirmed user does NOT receive session_error notifications.

    The gate in SystemNotificationService.notify() checks
    EmailConfirmationService.is_outbound_email_allowed(user), which returns
    False for email_confirmed=False.

    1. Create unconfirmed user A (fresh signup, email_confirmed=False).
    2. Promote to developer; give an AI credential so agent creation succeeds.
    3. Create agent + chat session owned by A.
    4. Trigger create_error_activity.
    5. Assert send_email NOT called.
    """
    # ── Phase 1-2: create and promote unconfirmed user ────────────────────
    user_a, headers_a = create_random_user_with_headers(client)
    assert user_a["email_confirmed"] is False, "Fresh signup should be unconfirmed"

    promote_to_developer(client, superuser_token_headers, user_a["id"])
    create_random_ai_credential(
        client,
        headers_a,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-unconfirmed-gate",
        name="unconfirmed-gate-cred",
        set_default=True,
    )

    # ── Phase 3: create agent + session ───────────────────────────────────
    agent = create_agent_via_api(client, headers_a, name="UnconfirmedGateAgent")
    agent_id = uuid.UUID(agent["id"])
    owner_id = uuid.UUID(user_a["id"])
    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    # ── Phase 4-5: trigger notification; expect no send ───────────────────
    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        asyncio.run(
            ActivityService.create_error_activity(
                db_session=db,
                session_id=chat_session.id,
                error_message="Gate test — unconfirmed owner",
            )
        )
        drain_tasks()

    assert mock_send.call_count == 0, (
        f"Unconfirmed user should NOT receive notification email. "
        f"Got {mock_send.call_count} send call(s)."
    )


# ── Scenario 2: Confirmed user — notification delivered ──────────────────────


def test_confirmed_user_notification_delivered(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A confirmed user DOES receive session_error notifications.

    1. Create user B (signup → email_confirmed=False).
    2. Confirm their email via POST /confirm-email/.
    3. Verify email_confirmed=True via GET /users/me.
    4. Create agent + session owned by B.
    5. Trigger create_error_activity.
    6. Assert send_email called once to B's address.
    """
    from tests.utils.user import user_authentication_headers

    # ── Phase 1-2: create and confirm user ───────────────────────────────
    user_b, headers_b = create_random_user_with_headers(client)
    assert user_b["email_confirmed"] is False

    _confirm_user_email(client, user_b["email"])

    # ── Phase 3: verify confirmed state ───────────────────────────────────
    me = client.get(f"{settings.API_V1_STR}/users/me", headers=headers_b).json()
    assert me["email_confirmed"] is True

    # ── Phase 4: create agent + session ───────────────────────────────────
    promote_to_developer(client, superuser_token_headers, user_b["id"])
    create_random_ai_credential(
        client,
        headers_b,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-confirmed-gate",
        name="confirmed-gate-cred",
        set_default=True,
    )

    agent = create_agent_via_api(client, headers_b, name="ConfirmedGateAgent")
    agent_id = uuid.UUID(agent["id"])
    owner_id = uuid.UUID(user_b["id"])
    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    # ── Phase 5-6: trigger notification; expect send ──────────────────────
    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        asyncio.run(
            ActivityService.create_error_activity(
                db_session=db,
                session_id=chat_session.id,
                error_message="Gate test — confirmed owner",
            )
        )
        drain_tasks()

    assert mock_send.call_count == 1, (
        f"Confirmed user SHOULD receive notification email. "
        f"Got {mock_send.call_count} send call(s)."
    )
    sent_to = mock_send.call_args.kwargs.get("email_to") or mock_send.call_args.args[0]
    assert sent_to == user_b["email"], (
        f"Email should go to confirmed user B ({user_b['email']}), got {sent_to}"
    )


# ── Scenario 3: Unconfirmed user — password recovery still works ─────────────


def test_unconfirmed_user_password_recovery_not_gated(client: TestClient) -> None:
    """Password recovery bypasses the email_confirmed gate.

    An unconfirmed user must still be able to receive password-reset emails.
    """
    from tests.utils.utils import random_lower_string, random_email

    email = random_email()
    password = random_lower_string()
    r = client.post(f"{settings.API_V1_STR}/users/signup", json={"email": email, "password": password})
    assert r.status_code == 200
    assert r.json()["email_confirmed"] is False

    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
        patch("app.services.users.user_service.send_email", mock_send),
    ):
        r2 = client.post(f"{settings.API_V1_STR}/password-recovery/{email}")

    assert r2.status_code == 200, r2.text
    assert mock_send.call_count == 1, (
        "Password recovery must be sent even for unconfirmed users "
        f"(got {mock_send.call_count} calls)"
    )
