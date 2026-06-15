"""Backend tests for the System Notifications / session-error email feature.

Coverage:
  1. GET /notification-settings/ → one item per catalog type, defaults applied
     when the user has no stored rows (session_error defaults to email_enabled=True).
  2. PUT /notification-settings/{type} upserts; a second PUT updates the SAME row
     (no duplicate); GET reflects the new value; unknown type → 404.
  3. Auth / ownership scoping: endpoints require auth; each user only sees their
     own settings.
  4. emails_enabled=True + send_email mocked:
     a. create_error_activity() → exactly one send to the owner.
     b. handle_session_state_updated(state=error) → one send.
  5. Preference disabled (email_enabled=False) → no send; the error_occurred
     Activity is still created.
  6. emails_enabled=False (SMTP_HOST=None) → no send, no exception; activity
     still created.
  7. Throttle: second error for the SAME session within dedup TTL → no second
     send; per-user rate cap (> MAX_PER_WINDOW across distinct sessions) suppresses
     further sends.
  8. send_email raising → swallowed; Activity still created; call does not raise.
  9. Recipient resolves from Session.user_id (owner) — mocked send went to the
     owner's email, not a different user.

Note: Tests that exercise the notification dispatch call ActivityService static
methods directly (create_error_activity / handle_session_state_updated) with a
real db session and a real Session row, as directed by the test-infra pointers.
This avoids a full streaming-environment setup while exercising the actual code path.
"""
import asyncio
import uuid
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.sessions.session import Session as ChatSession
from app.models.agents.agent import Agent
from app.utils import generate_email_confirmation_token
from app.services.events.activity_service import ActivityService
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
    user_authentication_headers,
    promote_to_developer,
)
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential


_BASE = f"{settings.API_V1_STR}/notification-settings"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_settings(client: TestClient, headers: dict) -> dict:
    """GET /notification-settings/ and return the parsed body."""
    r = client.get(f"{_BASE}/", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _put_setting(
    client: TestClient,
    headers: dict,
    notification_type: str,
    email_enabled: bool,
) -> dict:
    """PUT /notification-settings/{type} and return the parsed body."""
    r = client.put(
        f"{_BASE}/{notification_type}",
        headers=headers,
        json={"email_enabled": email_enabled},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_chat_session(db: Session, user_id: uuid.UUID, agent_id: uuid.UUID) -> ChatSession:
    """Create a minimal Session row for a given owner + agent.

    We bypass the API here because we need a bare Session object to pass
    directly into ActivityService static methods. No streaming setup is needed.
    """
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


# ── Scenario 1: GET returns catalog defaults when no rows exist ──────────────


def test_get_notification_settings_returns_catalog_defaults(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    GET /notification-settings/ returns one item per catalog type with defaults
    applied when the user has no stored preference rows.
    1. Call GET — response has one item per catalog type.
    2. session_error item has email_enabled=True (catalog default).
    3. Each item has label, description, notification_type fields.
    4. Unauthenticated request is rejected.
    """
    # ── Phase 1: GET returns catalog items ────────────────────────────────
    data = _get_settings(client, superuser_token_headers)
    assert "data" in data
    items = data["data"]
    assert len(items) >= 1, "Expected at least one catalog type"

    types = [item["notification_type"] for item in items]
    assert "session_error" in types

    # ── Phase 2: session_error defaults to email_enabled=True ─────────────
    session_error_item = next(i for i in items if i["notification_type"] == "session_error")
    assert session_error_item["email_enabled"] is True
    assert "label" in session_error_item
    assert "description" in session_error_item

    # ── Phase 3: Unauthenticated request is rejected ───────────────────────
    r_unauth = client.get(f"{_BASE}/")
    assert r_unauth.status_code in (401, 403)


# ── Scenario 2: PUT upserts; second PUT updates same row; unknown type → 404 ─


def test_put_notification_setting_upsert_and_unknown_type(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    PUT /notification-settings/{type} upserts; second PUT updates the same row;
    unknown type → 404; GET reflects the latest value.
    1. PUT session_error with email_enabled=False → returns updated item.
    2. GET → reflects False.
    3. PUT session_error again with email_enabled=True → same row updated (no dup).
    4. GET → reflects True.
    5. PUT with unknown type → 404.
    6. PUT requires auth.
    """
    # ── Phase 1: First PUT (disable) ──────────────────────────────────────
    updated = _put_setting(client, superuser_token_headers, "session_error", False)
    assert updated["notification_type"] == "session_error"
    assert updated["email_enabled"] is False

    # ── Phase 2: GET reflects disabled ────────────────────────────────────
    data = _get_settings(client, superuser_token_headers)
    se_item = next(i for i in data["data"] if i["notification_type"] == "session_error")
    assert se_item["email_enabled"] is False

    # ── Phase 3: Second PUT (re-enable) updates same row ──────────────────
    re_enabled = _put_setting(client, superuser_token_headers, "session_error", True)
    assert re_enabled["email_enabled"] is True

    # ── Phase 4: GET reflects enabled ─────────────────────────────────────
    data2 = _get_settings(client, superuser_token_headers)
    se_item2 = next(i for i in data2["data"] if i["notification_type"] == "session_error")
    assert se_item2["email_enabled"] is True

    # ── Phase 5: PUT unknown type → 404 ───────────────────────────────────
    r_unknown = client.put(
        f"{_BASE}/not_a_real_type",
        headers=superuser_token_headers,
        json={"email_enabled": True},
    )
    assert r_unknown.status_code == 404

    # ── Phase 6: PUT requires auth ────────────────────────────────────────
    r_unauth = client.put(f"{_BASE}/session_error", json={"email_enabled": False})
    assert r_unauth.status_code in (401, 403)


# ── Scenario 3: Auth scoping — users only see/set their own settings ─────────


def test_notification_settings_auth_scoping(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Each user sees their own settings, isolated from other users.
    1. Create user A and user B.
    2. A disables session_error.
    3. B's GET still shows the catalog default (enabled=True).
    4. B enables session_error (no-op from default).
    5. A's GET still shows disabled.
    """
    # ── Phase 1: Create two users ─────────────────────────────────────────
    user_a, headers_a = create_random_user_with_headers(client)
    user_b, headers_b = create_random_user_with_headers(client)

    # ── Phase 2: A disables session_error ─────────────────────────────────
    _put_setting(client, headers_a, "session_error", False)

    # ── Phase 3: B still sees the default (True) ──────────────────────────
    data_b = _get_settings(client, headers_b)
    se_b = next(i for i in data_b["data"] if i["notification_type"] == "session_error")
    assert se_b["email_enabled"] is True

    # ── Phase 4: B sets session_error to True (explicit) ──────────────────
    _put_setting(client, headers_b, "session_error", True)

    # ── Phase 5: A's setting is still False ───────────────────────────────
    data_a = _get_settings(client, headers_a)
    se_a = next(i for i in data_a["data"] if i["notification_type"] == "session_error")
    assert se_a["email_enabled"] is False


# ── Scenario 4a: emails_enabled + mocked send → create_error_activity sends ─


def test_create_error_activity_sends_email_to_owner(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    With emails_enabled patched True and send_email mocked:
    create_error_activity() dispatches exactly one email to the session owner.
    1. Create an agent + session row for the superuser.
    2. Call create_error_activity with an error message.
    3. Drain tasks (picks up the notification offload).
    4. Assert send_email called once with the owner's email.
    5. Assert an error_occurred activity was created (verifiable via activities API).
    """
    # ── Phase 1: create agent via API, build session row directly ─────────
    agent = create_agent_via_api(client, superuser_token_headers, name="ErrorAgent")
    agent_id = uuid.UUID(agent["id"])

    r_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner = r_me.json()
    owner_email = owner["email"]
    owner_id = uuid.UUID(owner["id"])

    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    # ── Phase 2: call create_error_activity with emails_enabled ───────────
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
                error_message="Test stream failure",
            )
        )
        # ── Phase 3: drain offloaded send tasks ───────────────────────────
        drain_tasks()

    # ── Phase 4: exactly one send to the owner ────────────────────────────
    assert mock_send.call_count == 1, (
        f"Expected 1 send_email call, got {mock_send.call_count}. "
        f"Calls: {mock_send.call_args_list}"
    )
    sent_to = mock_send.call_args.kwargs.get("email_to") or mock_send.call_args.args[0]
    assert sent_to == owner_email

    # ── Phase 5: activity was created (GET activities API) ────────────────
    r_activities = client.get(
        f"{settings.API_V1_STR}/activities/",
        headers=superuser_token_headers,
    )
    assert r_activities.status_code == 200
    activities = r_activities.json().get("data", [])
    error_activities = [a for a in activities if a["activity_type"] == "error_occurred"]
    assert len(error_activities) >= 1


# ── Scenario 4b: handle_session_state_updated(state=error) sends ─────────────


def test_handle_session_state_updated_error_sends_email(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    handle_session_state_updated with state=error dispatches one email.
    1. Create agent + session row.
    2. Call handle_session_state_updated with state=error.
    3. Drain tasks.
    4. Assert one email sent to the owner.
    """
    # ── Phase 1: create agent + session ──────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="ErrorAgent2")
    agent_id = uuid.UUID(agent["id"])

    r_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner = r_me.json()
    owner_email = owner["email"]
    owner_id = uuid.UUID(owner["id"])

    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    # ── Phase 2: call handler ────────────────────────────────────────────
    mock_send = MagicMock(return_value=None)
    event_data = {
        "model_id": str(chat_session.id),
        "user_id": str(owner_id),
        "meta": {
            "session_id": str(chat_session.id),
            "state": "error",
            "summary": "Agent declared an error state",
        },
    }
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        asyncio.run(ActivityService.handle_session_state_updated(event_data))
        # ── Phase 3: drain ────────────────────────────────────────────────
        drain_tasks()

    # ── Phase 4: one send to the owner ────────────────────────────────────
    assert mock_send.call_count == 1
    sent_to = mock_send.call_args.kwargs.get("email_to") or mock_send.call_args.args[0]
    assert sent_to == owner_email


# ── Scenario 5: Preference disabled → no send; activity still created ───────


def test_preference_off_suppresses_email_but_activity_created(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    When the user disables session_error notifications, no email is sent,
    but the error_occurred activity is still created.
    1. Disable session_error via PUT.
    2. create_error_activity → drain.
    3. Assert send_email NOT called.
    4. Assert activity IS created.
    """
    # ── Phase 1: disable the preference ──────────────────────────────────
    _put_setting(client, superuser_token_headers, "session_error", False)

    # ── Phase 2: create agent + session ──────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="PrefOffAgent")
    agent_id = uuid.UUID(agent["id"])

    r_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner = r_me.json()
    owner_id = uuid.UUID(owner["id"])

    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    # ── Phase 3: trigger error activity ───────────────────────────────────
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
                error_message="Preference-off error",
            )
        )
        drain_tasks()

    # ── Phase 4: no email ─────────────────────────────────────────────────
    assert mock_send.call_count == 0, (
        f"Expected no send, got {mock_send.call_count} calls"
    )

    # ── Phase 5: activity still created ───────────────────────────────────
    r_activities = client.get(
        f"{settings.API_V1_STR}/activities/",
        headers=superuser_token_headers,
    )
    assert r_activities.status_code == 200
    activities = r_activities.json().get("data", [])
    assert any(a["activity_type"] == "error_occurred" for a in activities)


# ── Scenario 6: emails_enabled=False → no send, no raise; activity created ───


def test_emails_disabled_suppresses_send_no_raise(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    When SMTP_HOST is None (emails_enabled=False), the send is skipped and no
    exception propagates. The error_occurred Activity is still created.
    1. Create agent + session.
    2. create_error_activity with SMTP_HOST=None.
    3. Drain tasks (no tasks to send).
    4. send_email is NOT called.
    5. Activity IS created.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="EmailsOffAgent")
    agent_id = uuid.UUID(agent["id"])

    r_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner = r_me.json()
    owner_id = uuid.UUID(owner["id"])

    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", None),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        # Must not raise
        asyncio.run(
            ActivityService.create_error_activity(
                db_session=db,
                session_id=chat_session.id,
                error_message="SMTP disabled",
            )
        )
        drain_tasks()

    assert mock_send.call_count == 0, "send_email must not be called when emails_enabled=False"

    r_activities = client.get(
        f"{settings.API_V1_STR}/activities/",
        headers=superuser_token_headers,
    )
    assert r_activities.status_code == 200
    activities = r_activities.json().get("data", [])
    assert any(a["activity_type"] == "error_occurred" for a in activities)


# ── Scenario 7: Throttle — dedup + per-user rate cap ─────────────────────────


def test_throttle_dedup_and_rate_cap(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    Throttle tests:
    a. A second error for the SAME session within the dedup TTL → no second send.
    b. Per-user rate cap (> MAX_PER_WINDOW sessions) → further sends suppressed.

    Uses the SystemNotificationService directly to tightly control the throttle
    state without creating many activity rows.
    """
    import app.services.notifications.notification_service as ns
    from app.services.notifications.notification_catalog import NotificationType
    from app.services.notifications.notification_service import SystemNotificationService

    agent = create_agent_via_api(client, superuser_token_headers, name="ThrottleAgent")
    agent_id = uuid.UUID(agent["id"])

    r_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner = r_me.json()
    owner_id = uuid.UUID(owner["id"])

    # ── Phase 1: First call — should send ────────────────────────────────
    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)
    session_id_str = str(chat_session.id)

    context = {
        "project_name": settings.PROJECT_NAME,
        "agent_name": "ThrottleAgent",
        "session_title": "Test session",
        "session_id": session_id_str,
        "error_text": "First error",
        "link": f"{settings.FRONTEND_HOST}/sessions/{session_id_str}",
    }

    mock_send = MagicMock(return_value=None)
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        asyncio.run(
            SystemNotificationService.notify(
                db,
                user_id=owner_id,
                notification_type=NotificationType.SESSION_ERROR,
                context=context,
            )
        )
        drain_tasks()

    assert mock_send.call_count == 1, "First error for a session should send"
    mock_send.reset_mock()

    # ── Phase 2: Second call for SAME session → dedup suppresses ─────────
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        asyncio.run(
            SystemNotificationService.notify(
                db,
                user_id=owner_id,
                notification_type=NotificationType.SESSION_ERROR,
                context=context,  # same session_id
            )
        )
        drain_tasks()

    assert mock_send.call_count == 0, "Dedup should suppress a repeat for the same session"

    # ── Phase 3: Fill the per-user rate window ────────────────────────────
    # Send MAX_PER_WINDOW notifications for distinct sessions, which fills the window.
    from app.services.notifications.notification_service import MAX_PER_WINDOW

    # Clear dedup state so each new session passes the dedup check.
    ns._dedup_seen.clear()
    ns._user_window.clear()

    mock_send.reset_mock()
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        for i in range(MAX_PER_WINDOW):
            distinct_context = {**context, "session_id": str(uuid.uuid4())}
            asyncio.run(
                SystemNotificationService.notify(
                    db,
                    user_id=owner_id,
                    notification_type=NotificationType.SESSION_ERROR,
                    context=distinct_context,
                )
            )
            drain_tasks()

        assert mock_send.call_count == MAX_PER_WINDOW, (
            f"Expected {MAX_PER_WINDOW} sends before cap; got {mock_send.call_count}"
        )
        mock_send.reset_mock()

        # One more → rate-capped
        extra_context = {**context, "session_id": str(uuid.uuid4())}
        asyncio.run(
            SystemNotificationService.notify(
                db,
                user_id=owner_id,
                notification_type=NotificationType.SESSION_ERROR,
                context=extra_context,
            )
        )
        drain_tasks()

    assert mock_send.call_count == 0, "Rate cap should suppress sends beyond MAX_PER_WINDOW"


# ── Scenario 8: send_email raising → swallowed; activity still created ───────


def test_send_email_exception_swallowed(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    When send_email raises an exception inside _async_send, the error is caught
    and swallowed. The originating create_error_activity call does not raise,
    and the Activity is still created.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="SmtpErrorAgent")
    agent_id = uuid.UUID(agent["id"])

    r_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner = r_me.json()
    owner_id = uuid.UUID(owner["id"])

    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    mock_send = MagicMock(side_effect=Exception("SMTP connection refused"))
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "noreply@example.com"),
        patch("app.services.notifications.notification_service.send_email", mock_send),
    ):
        # Must not raise
        asyncio.run(
            ActivityService.create_error_activity(
                db_session=db,
                session_id=chat_session.id,
                error_message="SMTP will fail",
            )
        )
        drain_tasks()  # _async_send runs here, exception is swallowed

    # send_email was called (then it raised, but that was swallowed)
    assert mock_send.call_count == 1

    # Activity was still created
    r_activities = client.get(
        f"{settings.API_V1_STR}/activities/",
        headers=superuser_token_headers,
    )
    assert r_activities.status_code == 200
    activities = r_activities.json().get("data", [])
    assert any(a["activity_type"] == "error_occurred" for a in activities)


# ── Scenario 9: Recipient resolves from Session.user_id (owner), not others ──


def test_email_goes_to_session_owner_not_other_user(
    client: TestClient,
    superuser_token_headers: dict,
    db: Session,
) -> None:
    """
    The notification recipient is always Session.user_id (the session owner).
    Create two users; owner A creates a session; verify the email goes to A, not B.
    1. Create user A (session owner).
    2. Create user B.
    3. A creates an agent + session.
    4. create_error_activity → drain.
    5. Email goes to A's address; B's address is not in any send call.
    """
    # ── Phase 1: create two users ─────────────────────────────────────────
    user_a, headers_a = create_random_user_with_headers(client)
    user_b, headers_b = create_random_user_with_headers(client)

    # Confirm user A's email so the notification gate lets their email through.
    # User B remains unconfirmed — they should NOT receive email (the test's point).
    token_a = generate_email_confirmation_token(email=user_a["email"])
    r_confirm = client.post(
        f"{settings.API_V1_STR}/confirm-email/", json={"token": token_a}
    )
    assert r_confirm.status_code == 200, (
        f"Email confirmation for owner failed: {r_confirm.text}"
    )

    # Promote user A so they can create agents, and give them an AI credential
    promote_to_developer(client, superuser_token_headers, user_a["id"])
    create_random_ai_credential(
        client,
        headers_a,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-owner-key",
        name="user-a-credential",
        set_default=True,
    )

    # ── Phase 2: A creates agent; build session in db ──────────────────────
    agent = create_agent_via_api(client, headers_a, name="OwnerTestAgent")
    agent_id = uuid.UUID(agent["id"])
    owner_id = uuid.UUID(user_a["id"])

    chat_session = _create_chat_session(db, user_id=owner_id, agent_id=agent_id)

    # ── Phase 3: trigger error with emails enabled ────────────────────────
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
                error_message="Owner resolution check",
            )
        )
        drain_tasks()

    # ── Phase 4: email went to A, not B ───────────────────────────────────
    assert mock_send.call_count == 1
    sent_to = mock_send.call_args.kwargs.get("email_to") or mock_send.call_args.args[0]
    assert sent_to == user_a["email"], (
        f"Expected email to owner A ({user_a['email']}), got {sent_to}"
    )
    assert sent_to != user_b["email"], "Email must not go to user B"
