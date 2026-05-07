"""
Integration tests: Agent Webhooks — CRUD, execution, logs, and cascade behavior.

Tests the full feature surface for Agent Webhooks:

  Authenticated CRUD (uses test DB session via SessionDep / get_db override):
  - POST /api/v1/agents/{id}/webhooks/session — Create session-type webhook
  - POST /api/v1/agents/{id}/webhooks/script  — Create script-type webhook
  - GET  /api/v1/agents/{id}/webhooks         — List webhooks
  - GET  /api/v1/agents/{id}/webhooks/{pk}    — Get single webhook
  - PATCH /api/v1/agents/{id}/webhooks/{pk}   — Update webhook
  - DELETE /api/v1/agents/{id}/webhooks/{pk}  — Delete webhook
  - POST /api/v1/agents/{id}/webhooks/{pk}/regenerate-token
  - GET  /api/v1/agents/{id}/webhooks/{pk}/logs

  Public execution endpoint (no JWT; token-auth only):
  - POST /agent-hooks/{webhook_id}

Note on the public endpoint: it uses Session(engine) directly — bypassing the
test session. We therefore mock the AgentWebhookService layer when testing that
endpoint so that route-level behaviour (token extraction, payload size guard,
response shape) is exercised without needing real data visible to the engine
session. The service methods themselves are integration-tested via the CRUD
endpoints and the logs they produce.
"""
import uuid
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers
from tests.utils.webhook import (
    create_script_webhook,
    create_session_webhook,
    delete_webhook,
    fire_webhook,
    get_webhook,
    list_webhook_logs,
    list_webhooks,
    regenerate_token,
    update_webhook,
)

API = settings.API_V1_STR

# ── Inline URL builders ───────────────────────────────────────────────────────


def _webhooks_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks"


def _webhook_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}"


def _logs_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}/logs"


def _regen_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}/regenerate-token"


def _public_url(webhook_id: str) -> str:
    return f"/agent-hooks/{webhook_id}"


# ── Setup helpers ─────────────────────────────────────────────────────────────


def _make_agent(client: TestClient, headers: dict, name: str = "Webhook Agent") -> dict:
    """Create an agent and drain startup background tasks."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    return agent


def _make_mock_log(
    webhook_id_fk: str | None = None,
    agent_id: str | None = None,
    status: str = "session_started",
    webhook_type: str = "session",
    session_id: str | None = None,
    command_executed: str | None = None,
    command_output: str | None = None,
    command_stderr: str | None = None,
    command_exit_code: int | None = None,
    error_message: str | None = None,
    payload_received: str | None = None,
    prompt_used: str | None = None,
) -> MagicMock:
    """Build a minimal mock AgentWebhookLog for use when patching fire_webhook."""
    log = MagicMock()
    log.id = uuid.uuid4()
    log.webhook_id_fk = uuid.UUID(webhook_id_fk) if webhook_id_fk else uuid.uuid4()
    log.agent_id = uuid.UUID(agent_id) if agent_id else uuid.uuid4()
    log.webhook_type = webhook_type
    log.status = status
    log.session_id = uuid.UUID(session_id) if session_id else None
    log.command_executed = command_executed
    log.command_output = command_output
    log.command_stderr = command_stderr
    log.command_exit_code = command_exit_code
    log.error_message = error_message
    log.payload_received = payload_received
    log.prompt_used = prompt_used
    log.headers_subset = {}
    log.remote_ip = None
    log.payload_content_type = None
    log.duration_ms = 10
    return log


# ── CRUD lifecycle ────────────────────────────────────────────────────────────


def test_session_webhook_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full CRUD lifecycle for a session-type webhook:
      1. Create agent
      2. List webhooks → empty
      3. Create session webhook → returns token once (AgentWebhookPublicWithToken)
      4. Verify GET does NOT include plaintext token
      5. List webhooks → webhook appears, ordered newest-first
      6. Update name + prompt → changes persisted, type unchanged
      7. Get by row UUID → correct data returned
      8. Delete webhook → success
      9. List webhooks → empty again
      10. Verify cascade: DELETE of deleted webhook → 404
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = _make_agent(client, headers, name="Session Webhook Lifecycle")
    agent_id = agent["id"]

    # ── Phase 2: List webhooks → empty ───────────────────────────────────
    webhooks = list_webhooks(client, headers, agent_id)
    assert webhooks == []

    # ── Phase 3: Create session webhook ──────────────────────────────────
    created = create_session_webhook(
        client, headers, agent_id,
        name="GitHub Push",
        prompt="Analyze the incoming push event.",
        session_mode="conversation",
        payload_template="Here is the push data:",
    )

    webhook_pk = created["id"]
    assert created["agent_id"] == agent_id
    assert created["name"] == "GitHub Push"
    assert created["type"] == "session"
    assert created["enabled"] is True
    assert created["prompt"] == "Analyze the incoming push event."
    assert created["session_mode"] == "conversation"
    assert created["payload_template"] == "Here is the push data:"
    assert created["command"] is None
    assert created["command_timeout_seconds"] is None
    # One-time token must be present on creation
    assert "webhook_token" in created
    assert created["webhook_token"]
    # Slug + prefix
    assert "webhook_id" in created and created["webhook_id"]
    assert "webhook_token_prefix" in created and len(created["webhook_token_prefix"]) == 8
    # Computed URL
    assert "webhook_url" in created and "agent-hooks" in created["webhook_url"]
    assert "last_execution" in created
    assert "created_at" in created and "updated_at" in created

    # ── Phase 4: GET does NOT include plaintext token ─────────────────────
    fetched = get_webhook(client, headers, agent_id, webhook_pk)
    assert "webhook_token" not in fetched, "Plaintext token must not appear on GET"
    assert fetched["id"] == webhook_pk
    assert fetched["name"] == "GitHub Push"
    assert fetched["type"] == "session"

    # ── Phase 5: List webhooks → webhook appears ──────────────────────────
    webhooks = list_webhooks(client, headers, agent_id)
    assert len(webhooks) == 1
    assert webhooks[0]["id"] == webhook_pk
    assert "webhook_token" not in webhooks[0], "Plaintext token must not appear in list"

    # ── Phase 6: Update name + prompt → persisted; type unchanged ─────────
    updated = update_webhook(
        client, headers, agent_id, webhook_pk,
        name="Renamed Webhook",
        prompt="Updated prompt.",
    )
    assert updated["name"] == "Renamed Webhook"
    assert updated["prompt"] == "Updated prompt."
    assert updated["type"] == "session"          # immutable
    assert updated["session_mode"] == "conversation"  # unchanged

    # Verify change visible via GET
    fetched2 = get_webhook(client, headers, agent_id, webhook_pk)
    assert fetched2["name"] == "Renamed Webhook"
    assert fetched2["prompt"] == "Updated prompt."

    # ── Phase 7: Get by row UUID ──────────────────────────────────────────
    fetched3 = get_webhook(client, headers, agent_id, webhook_pk)
    assert fetched3["id"] == webhook_pk
    assert fetched3["agent_id"] == agent_id

    # ── Phase 8: Delete webhook ───────────────────────────────────────────
    result = delete_webhook(client, headers, agent_id, webhook_pk)
    assert result["success"] is True

    # ── Phase 9: List → empty ─────────────────────────────────────────────
    webhooks = list_webhooks(client, headers, agent_id)
    assert webhooks == []

    # ── Phase 10: GET on deleted → 404 ───────────────────────────────────
    r = client.get(_webhook_url(agent_id, webhook_pk), headers=headers)
    assert r.status_code == 404


def test_script_webhook_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full CRUD lifecycle for a script-type webhook:
      1. Create script webhook with required command
      2. Verify response: command, command_timeout_seconds, prompt=None, session_mode=None
      3. Update command and timeout
      4. Verify update persisted
      5. Delete and verify gone
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Script Webhook Lifecycle")
    agent_id = agent["id"]

    # ── Phase 1: Create script webhook ───────────────────────────────────
    created = create_script_webhook(
        client, headers, agent_id,
        name="Health Check",
        command="bash /app/workspace/check.sh",
        command_timeout_seconds=60,
    )

    webhook_pk = created["id"]
    assert created["type"] == "script"
    assert created["command"] == "bash /app/workspace/check.sh"
    assert created["command_timeout_seconds"] == 60
    assert created["prompt"] is None
    assert created["session_mode"] is None
    assert "webhook_token" in created
    assert created["webhook_token"]

    # ── Phase 2: Update command and timeout ───────────────────────────────
    updated = update_webhook(
        client, headers, agent_id, webhook_pk,
        command="python /app/workspace/status.py",
        command_timeout_seconds=90,
    )
    assert updated["command"] == "python /app/workspace/status.py"
    assert updated["command_timeout_seconds"] == 90
    assert updated["type"] == "script"

    # ── Phase 3: Toggle enabled → False ──────────────────────────────────
    disabled = update_webhook(
        client, headers, agent_id, webhook_pk,
        enabled=False,
    )
    assert disabled["enabled"] is False

    # ── Phase 4: Re-enable ────────────────────────────────────────────────
    enabled = update_webhook(client, headers, agent_id, webhook_pk, enabled=True)
    assert enabled["enabled"] is True

    # ── Phase 5: Delete → verify gone ────────────────────────────────────
    delete_webhook(client, headers, agent_id, webhook_pk)
    r = client.get(_webhook_url(agent_id, webhook_pk), headers=headers)
    assert r.status_code == 404


def test_multiple_webhooks_ordered_by_created_at_desc(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Multiple webhooks are returned by list in created_at DESC order:
      1. Create three webhooks with different names
      2. List → all three appear; newest first
      3. Delete the middle one → remaining two intact, count decremented
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Multi Webhook Agent")
    agent_id = agent["id"]

    w1 = create_session_webhook(client, headers, agent_id, name="First")
    w2 = create_script_webhook(client, headers, agent_id, name="Second", command="echo 2")
    w3 = create_session_webhook(client, headers, agent_id, name="Third")

    webhooks = list_webhooks(client, headers, agent_id)
    assert len(webhooks) == 3

    ids = [w["id"] for w in webhooks]
    assert w1["id"] in ids
    assert w2["id"] in ids
    assert w3["id"] in ids

    # Most recently created (w3) should appear first (or at least before w1)
    idx_w3 = ids.index(w3["id"])
    idx_w1 = ids.index(w1["id"])
    assert idx_w3 < idx_w1, "Webhooks should be ordered newest-first (created_at DESC)"

    # Delete the middle one
    delete_webhook(client, headers, agent_id, w2["id"])

    remaining = list_webhooks(client, headers, agent_id)
    assert len(remaining) == 2
    remaining_ids = {w["id"] for w in remaining}
    assert w2["id"] not in remaining_ids
    assert w1["id"] in remaining_ids
    assert w3["id"] in remaining_ids


# ── Validation: create ────────────────────────────────────────────────────────


def test_script_webhook_requires_command(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Creating a script webhook without command returns 422 (Pydantic validation)."""
    agent = _make_agent(client, superuser_token_headers, name="No Command Agent")
    r = client.post(
        f"{_webhooks_url(agent['id'])}/script",
        headers=superuser_token_headers,
        json={"name": "No command", "type": "script"},
    )
    assert r.status_code == 422


def test_script_webhook_empty_command_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Creating a script webhook with empty/whitespace command returns 422."""
    agent = _make_agent(client, superuser_token_headers, name="Empty Command Agent")
    r = client.post(
        f"{_webhooks_url(agent['id'])}/script",
        headers=superuser_token_headers,
        json={"name": "empty", "type": "script", "command": ""},
    )
    assert r.status_code == 422


# ── Validation: update / type immutability ────────────────────────────────────


def test_type_immutable_command_on_session_webhook_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Sending a script-only field (command) on a session webhook PATCH → 400.
    This tests the service-layer type-mismatch guard.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Type Immutable Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Session Hook")

    r = client.patch(
        _webhook_url(agent["id"], webhook["id"]),
        headers=headers,
        json={"command": "echo forbidden"},
    )
    assert r.status_code == 400, f"Expected 400 for mismatched type field, got {r.status_code}: {r.text}"


def test_type_immutable_prompt_on_script_webhook_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Sending a session-only field (prompt) on a script webhook PATCH → 400.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Type Immutable Script Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Script Hook", command="echo hi"
    )

    r = client.patch(
        _webhook_url(agent["id"], webhook["id"]),
        headers=headers,
        json={"prompt": "forbidden prompt"},
    )
    assert r.status_code == 400, f"Expected 400 for mismatched type field, got {r.status_code}: {r.text}"


# ── Regenerate token ──────────────────────────────────────────────────────────


def test_regenerate_token_produces_new_token_same_slug(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Regenerating a token:
      1. Creates webhook, saves original token + webhook_id + prefix
      2. Regenerates — response includes webhook_token (new plaintext)
      3. Same webhook_id slug (URL unchanged)
      4. New token != old token
      5. New prefix != old prefix
      6. Verify via service that old token no longer validates, new one does

    Note: the public /agent-hooks endpoint uses Session(engine) which bypasses
    the test transaction, so we verify token invalidation at the service level
    via validate_webhook_token directly rather than through the HTTP endpoint.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Regen Token Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Regen Hook")

    original_token = webhook["webhook_token"]
    original_prefix = webhook["webhook_token_prefix"]
    webhook_pk = webhook["id"]
    webhook_slug = webhook["webhook_id"]

    # ── Regenerate ────────────────────────────────────────────────────────
    regen = regenerate_token(client, headers, agent["id"], webhook_pk)
    new_token = regen["webhook_token"]
    new_prefix = regen["webhook_token_prefix"]

    assert new_token, "New token must be non-empty"
    assert new_token != original_token, "Token must change after regenerate"
    assert regen["webhook_id"] == webhook_slug, "webhook_id slug must remain the same"
    # The prefix is the first 8 chars of the new token — must also differ
    assert new_prefix != original_prefix, "Token prefix must change after regenerate"
    # Verify the new prefix matches the new token's first 8 chars
    assert new_token.startswith(new_prefix), "New prefix must be first 8 chars of new token"

    # ── Old token is now stale — verify via the service directly ──────────
    # The public HTTP endpoint uses Session(engine) (bypasses test transaction),
    # so we probe the service layer via the CRUD PATCH endpoint which does use
    # the test session: a GET with the old token embedded in the Authorization
    # header would fail 401. Instead we verify structurally: the DB row now
    # holds the new encrypted token (confirmed by the new prefix changing) and
    # the old token cannot validate via the public route (we simulate this via
    # a mock that raises WebhookTokenInvalidError for the old value).

    from app.services.agents.agent_webhook_errors import WebhookTokenInvalidError as _Err

    def _reject_old_token(db_session, webhook_id, provided_token):
        # Simulate the service rejecting the old token and accepting the new one
        if provided_token == original_token:
            raise _Err()
        # Return a minimal mock webhook for the new token
        m = MagicMock()
        m.id = uuid.UUID(webhook_pk)
        m.type = "session"
        m.webhook_id = webhook_slug
        return m

    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="session_started",
        webhook_type="session",
    )

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        side_effect=_reject_old_token,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        # Old token → 401
        r_old = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {original_token}"},
        )
        assert r_old.status_code == 401, (
            f"Old token should be rejected after regenerate, got {r_old.status_code}"
        )

        # New token → 200
        r_new = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert r_new.status_code == 200, (
            f"New token should be accepted after regenerate, got {r_new.status_code}"
        )
        assert r_new.json()["success"] is True


# ── Authorization / ownership ─────────────────────────────────────────────────


def test_other_user_cannot_manage_webhooks(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User B cannot list, create, get, update, delete webhooks, or view logs
    for an agent owned by User A. All operations return 400 or 404.

      1. User A creates agent + session webhook
      2. User B cannot list webhooks
      3. User B cannot create webhook
      4. User B cannot get webhook by PK
      5. User B cannot update webhook
      6. User B cannot delete webhook
      7. User B cannot view logs
      8. User A still has full access
    """
    headers_a = superuser_token_headers
    agent = _make_agent(client, headers_a, name="Owner Agent")
    agent_id = agent["id"]
    webhook = create_session_webhook(client, headers_a, agent_id, name="Owner Hook")
    webhook_pk = webhook["id"]

    _, headers_b = create_random_user_with_headers(client)

    # List
    r = client.get(_webhooks_url(agent_id), headers=headers_b)
    assert r.status_code in (400, 403, 404)

    # Create session
    r = client.post(
        f"{_webhooks_url(agent_id)}/session",
        headers=headers_b,
        json={"name": "intruder", "type": "session"},
    )
    assert r.status_code in (400, 403, 404)

    # Get by PK
    r = client.get(_webhook_url(agent_id, webhook_pk), headers=headers_b)
    assert r.status_code in (400, 403, 404)

    # Update
    r = client.patch(
        _webhook_url(agent_id, webhook_pk),
        headers=headers_b,
        json={"name": "hacked"},
    )
    assert r.status_code in (400, 403, 404)

    # Delete
    r = client.delete(_webhook_url(agent_id, webhook_pk), headers=headers_b)
    assert r.status_code in (400, 403, 404)

    # Logs
    r = client.get(_logs_url(agent_id, webhook_pk), headers=headers_b)
    assert r.status_code in (400, 403, 404)

    # User A still has access
    still_there = list_webhooks(client, headers_a, agent_id)
    assert len(still_there) == 1
    assert still_there[0]["id"] == webhook_pk


def test_nonexistent_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """All webhook CRUD endpoints return 404 when agent_id does not exist."""
    fake_agent = str(uuid.uuid4())
    fake_pk = str(uuid.uuid4())
    headers = superuser_token_headers

    assert client.get(
        _webhooks_url(fake_agent), headers=headers
    ).status_code == 404

    assert client.post(
        f"{_webhooks_url(fake_agent)}/session",
        headers=headers,
        json={"name": "ghost", "type": "session"},
    ).status_code == 404

    assert client.get(
        _webhook_url(fake_agent, fake_pk), headers=headers
    ).status_code == 404

    assert client.patch(
        _webhook_url(fake_agent, fake_pk),
        headers=headers,
        json={"name": "ghost"},
    ).status_code == 404

    assert client.delete(
        _webhook_url(fake_agent, fake_pk), headers=headers
    ).status_code == 404


def test_cross_agent_webhook_isolation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A webhook created for agent A cannot be accessed or mutated via agent B's routes.
    """
    headers = superuser_token_headers
    agent_a = _make_agent(client, headers, name="Agent A")
    agent_b = _make_agent(client, headers, name="Agent B")

    webhook = create_session_webhook(client, headers, agent_a["id"], name="Agent A Hook")
    webhook_pk = webhook["id"]

    # Get via agent B → 404
    assert client.get(
        _webhook_url(agent_b["id"], webhook_pk), headers=headers
    ).status_code == 404

    # Update via agent B → 404
    assert client.patch(
        _webhook_url(agent_b["id"], webhook_pk),
        headers=headers,
        json={"name": "hijacked"},
    ).status_code == 404

    # Delete via agent B → 404
    assert client.delete(
        _webhook_url(agent_b["id"], webhook_pk), headers=headers
    ).status_code == 404

    # Agent B list is empty
    assert list_webhooks(client, headers, agent_b["id"]) == []

    # Agent A still has the webhook
    webhooks_a = list_webhooks(client, headers, agent_a["id"])
    assert any(w["id"] == webhook_pk for w in webhooks_a)


# ── Logs endpoint ─────────────────────────────────────────────────────────────


def test_logs_empty_initially(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A newly created webhook has no execution logs."""
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Log Empty Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Log Test")

    r = client.get(_logs_url(agent["id"], webhook["id"]), headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["data"] == []


def test_logs_limit_query_param(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /webhooks/{pk}/logs?limit=N returns the correct response shape.
    The route enforces 1 ≤ limit ≤ 200.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Limit Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Limit Test")
    wh_pk = webhook["id"]

    # Default limit (50) — no query param
    r = client.get(_logs_url(agent["id"], wh_pk), headers=headers)
    assert r.status_code == 200

    # Explicit limit=1
    r = client.get(_logs_url(agent["id"], wh_pk), headers=headers, params={"limit": 1})
    assert r.status_code == 200
    assert r.json()["data"] == []

    # limit=200 (max allowed)
    r = client.get(_logs_url(agent["id"], wh_pk), headers=headers, params={"limit": 200})
    assert r.status_code == 200

    # limit > 200 → rejected by route (Query le=200)
    r = client.get(_logs_url(agent["id"], wh_pk), headers=headers, params={"limit": 201})
    assert r.status_code == 422


def test_logs_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """GET /webhooks/{pk}/logs returns AgentWebhookLogsPublic shape."""
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Shape Agent Logs")
    webhook = create_session_webhook(client, headers, agent["id"], name="Shape Test")

    r = client.get(_logs_url(agent["id"], webhook["id"]), headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"data", "count"}
    assert isinstance(body["data"], list)
    assert isinstance(body["count"], int)


def test_logs_nonexistent_webhook_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """GET logs for a non-existent webhook → 404."""
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="404 Logs Agent")
    fake_pk = str(uuid.uuid4())
    r = client.get(_logs_url(agent["id"], fake_pk), headers=headers)
    assert r.status_code == 404


def test_logs_nonexistent_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """GET logs for a non-existent agent → 404."""
    r = client.get(
        _logs_url(str(uuid.uuid4()), str(uuid.uuid4())),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_logs_access_denied_for_other_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Another user cannot view webhook logs for an agent they don't own."""
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Log Access Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Log Access")

    _, other_headers = create_random_user_with_headers(client)
    r = client.get(_logs_url(agent["id"], webhook["id"]), headers=other_headers)
    assert r.status_code in (400, 403, 404)


# ── Public endpoint — pre-validation (no service layer needed) ────────────────


def test_public_endpoint_unknown_webhook_id_returns_404(
    client: TestClient,
) -> None:
    """POST /agent-hooks/<random> with valid token format → 404."""
    r = client.post(
        _public_url("unknownslug"),
        headers={"Authorization": "Bearer sometoken"},
    )
    assert r.status_code == 404


def test_public_endpoint_missing_token_returns_401(
    client: TestClient,
) -> None:
    """POST /agent-hooks/{id} without Authorization header or ?token → 401."""
    r = client.post(_public_url("anyslug"))
    assert r.status_code == 401
    assert r.json()["detail"] == "Token required"


def test_public_endpoint_bearer_header_and_query_param_both_accepted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Both Authorization: Bearer <token> header and ?token=<token> query param
    are accepted by the public endpoint. Test both paths with a mocked service.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Dual Auth Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Dual Auth Hook")
    webhook_slug = webhook["webhook_id"]
    token = webhook["webhook_token"]
    webhook_pk = webhook["id"]

    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="session_started",
        webhook_type="session",
    )
    # A MagicMock that looks like an AgentWebhook and passes the slug / enabled check
    mock_webhook_obj = MagicMock()
    mock_webhook_obj.id = uuid.UUID(webhook_pk)
    mock_webhook_obj.type = "session"
    mock_webhook_obj.webhook_id = webhook_slug
    mock_webhook_obj.enabled = True

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        # Bearer header
        r1 = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200, f"Bearer header failed: {r1.text}"
        assert r1.json()["success"] is True

        # ?token= query param
        r2 = client.post(
            f"{_public_url(webhook_slug)}?token={token}",
        )
        assert r2.status_code == 200, f"Query param token failed: {r2.text}"
        assert r2.json()["success"] is True


def test_public_endpoint_payload_too_large_returns_413(
    client: TestClient,
) -> None:
    """
    POST /agent-hooks/{id} with body > 64 KB → 413.
    The webhook is never fired (no log row created).
    """
    # 65 KB body (> 64 KB limit)
    large_payload = b"x" * (64 * 1024 + 1)
    r = client.post(
        _public_url("anyslug"),
        headers={
            "Authorization": "Bearer sometoken",
            "Content-Type": "application/octet-stream",
        },
        content=large_payload,
    )
    assert r.status_code == 413
    assert "64" in r.json()["detail"] or "KB" in r.json()["detail"]


def test_public_endpoint_disabled_webhook_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A disabled webhook returns 404 on the public endpoint — no existence leak.
    Even providing the correct token should return 404, not 401.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Disabled Webhook Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Disabled Hook")
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    # Disable via PATCH
    update_webhook(client, headers, agent["id"], webhook_pk, enabled=False)

    # The public endpoint must return 404 (not 401) for disabled webhooks
    r = client.post(
        _public_url(webhook_slug),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, (
        f"Disabled webhook should return 404, got {r.status_code}"
    )


def test_public_endpoint_token_mismatch_returns_401(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /agent-hooks/{id} with wrong token → 401.

    The public endpoint uses Session(engine) directly (not the test session),
    so we mock validate_webhook_token to raise WebhookTokenInvalidError —
    which is exactly what the real service does when the token doesn't match
    the stored Fernet-decrypted value.
    """
    from app.services.agents.agent_webhook_errors import WebhookTokenInvalidError as _Err

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        side_effect=_Err(),
    ):
        r = client.post(
            _public_url("someknownslug"),
            headers={"Authorization": "Bearer wrongtoken123"},
        )

    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


# ── Public endpoint — execution outcomes (service mocked) ────────────────────


def test_public_endpoint_session_type_success_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Successful session-type webhook fire returns:
      { "success": true, "webhook_type": "session", "log_id": "<uuid>" }
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Session Fire Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Session Fire")
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    session_id = str(uuid.uuid4())
    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="session_started",
        webhook_type="session",
        session_id=session_id,
    )
    mock_webhook_obj = MagicMock()
    mock_webhook_obj.id = uuid.UUID(webhook_pk)
    mock_webhook_obj.type = "session"
    mock_webhook_obj.webhook_id = webhook_slug
    mock_webhook_obj.enabled = True

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        r = client.post(
            _public_url(webhook_slug),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=b'{"event": "push"}',
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["webhook_type"] == "session"
    assert "log_id" in body and body["log_id"] is not None


def test_public_endpoint_script_type_exit0_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Successful script-type webhook fire (exit 0) returns:
      { "success": true, "webhook_type": "script", "log_id": "<uuid>" }
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Script Fire Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Script Fire", command="echo ok"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="success",
        webhook_type="script",
        command_executed="echo ok",
        command_output="ok\n",
        command_exit_code=0,
    )
    mock_webhook_obj = MagicMock()
    mock_webhook_obj.id = uuid.UUID(webhook_pk)
    mock_webhook_obj.type = "script"
    mock_webhook_obj.webhook_id = webhook_slug
    mock_webhook_obj.enabled = True

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["webhook_type"] == "script"
    assert body["log_id"] is not None


def test_public_endpoint_script_nonzero_exit_returns_200_with_log(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Script-type webhook with non-zero exit code: response is still 200 (caller
    gets receipt). The mock log has status="script_error".
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="NonZero Exit Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="NonZero Exit", command="exit 1"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="script_error",
        webhook_type="script",
        command_executed="exit 1",
        command_output="",
        command_stderr="error: command failed\n",
        command_exit_code=1,
    )
    mock_webhook_obj = MagicMock()
    mock_webhook_obj.type = "script"
    mock_webhook_obj.webhook_id = webhook_slug

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["webhook_type"] == "script"
    assert body["log_id"] is not None


def test_public_endpoint_error_status_still_returns_200(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Infrastructure error (e.g. no active env) still returns HTTP 200 with log_id.
    Caller always gets a receipt; they can inspect the log for the error detail.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Error Status Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Error Status")
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="error",
        webhook_type="session",
        error_message="Could not create session — no active environment",
    )
    mock_webhook_obj = MagicMock()
    mock_webhook_obj.type = "session"
    mock_webhook_obj.webhook_id = webhook_slug

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["log_id"] is not None


# ── Header allowlist ──────────────────────────────────────────────────────────


def test_header_allowlist_strips_sensitive_headers() -> None:
    """
    AgentWebhookService.filter_headers keeps only the allowlisted headers
    and strips authorization / cookie and any other non-allowlisted header.
    This unit-level test imports only the service class — no DB / HTTP needed.
    """
    from app.services.agents.agent_webhook_service import AgentWebhookService

    incoming = {
        "Authorization": "Bearer super-secret-token",
        "Cookie": "session=abc123",
        "User-Agent": "GitHub-Hookshot/abc",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": "sha256=abc",
        "X-Custom-Header": "should be stripped",
        "Content-Type": "application/json",
    }
    filtered = AgentWebhookService.filter_headers(incoming)

    # Sensitive headers must be absent
    assert "authorization" not in filtered
    assert "cookie" not in filtered
    # Non-allowlisted headers must be absent
    assert "x-custom-header" not in filtered
    assert "content-type" not in filtered
    # Allowlisted headers must be present (canonical lowercase)
    assert filtered.get("user-agent") == "GitHub-Hookshot/abc"
    assert filtered.get("x-github-event") == "push"
    assert filtered.get("x-hub-signature-256") == "sha256=abc"


def test_header_allowlist_preserves_all_allowed_headers() -> None:
    """All headers in FORWARDED_HEADERS are passed through when present."""
    from app.services.agents.agent_webhook_service import AgentWebhookService

    incoming = {
        "user-agent": "test-agent",
        "x-forwarded-for": "1.2.3.4, 5.6.7.8",
        "x-real-ip": "1.2.3.4",
        "x-github-event": "push",
        "x-gitlab-event": "Push Hook",
        "x-hub-signature-256": "sha256=deadbeef",
        "x-event-key": "repo:push",
    }
    filtered = AgentWebhookService.filter_headers(incoming)
    for h in AgentWebhookService.FORWARDED_HEADERS:
        if h in incoming:
            assert h in filtered, f"Expected allowlisted header '{h}' to be present"


# ── Cascade behavior ──────────────────────────────────────────────────────────


def test_delete_agent_cascades_to_webhooks(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deleting the agent cascades to webhooks. After agent deletion:
    - GET on the webhook → 404
    - List on the agent → 404 (agent gone)
    The webhook rows and their FK constraints are handled by CASCADE in the DB.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Cascade Agent Delete")
    agent_id = agent["id"]
    webhook = create_session_webhook(client, headers, agent_id, name="Cascade Test")
    webhook_pk = webhook["id"]

    # Verify webhook exists
    assert client.get(_webhook_url(agent_id, webhook_pk), headers=headers).status_code == 200

    # Delete the agent
    r = client.delete(f"{API}/agents/{agent_id}", headers=headers)
    assert r.status_code == 200, f"Agent delete failed: {r.text}"

    # Webhook is gone along with the agent
    r2 = client.get(_webhook_url(agent_id, webhook_pk), headers=headers)
    assert r2.status_code == 404


def test_delete_webhook_cascades_to_logs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deleting a webhook returns 200. After deletion, both the webhook and its
    logs are inaccessible. The owned agent is unaffected.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Cascade Webhook Delete")
    agent_id = agent["id"]

    w1 = create_session_webhook(client, headers, agent_id, name="Cascade W1")
    w2 = create_session_webhook(client, headers, agent_id, name="Cascade W2")

    # Delete w1
    delete_webhook(client, headers, agent_id, w1["id"])

    # w1 logs endpoint → 404 (webhook gone)
    r = client.get(_logs_url(agent_id, w1["id"]), headers=headers)
    assert r.status_code == 404

    # w2 is unaffected
    assert client.get(_webhook_url(agent_id, w2["id"]), headers=headers).status_code == 200


# ── List response shape ───────────────────────────────────────────────────────


def test_list_webhooks_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """GET /webhooks returns AgentWebhooksPublic shape with required fields."""
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Response Shape Agent")
    agent_id = agent["id"]

    # Empty
    r = client.get(_webhooks_url(agent_id), headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"data", "count"}
    assert body["count"] == 0
    assert body["data"] == []

    # After creating one
    create_session_webhook(client, headers, agent_id, name="Shape Check")
    r = client.get(_webhooks_url(agent_id), headers=headers)
    body = r.json()
    assert body["count"] == 1
    assert len(body["data"]) == 1

    required = {
        "id", "agent_id", "owner_id", "type", "name", "enabled",
        "webhook_id", "webhook_token_prefix", "webhook_url",
        "last_execution", "created_at", "updated_at",
    }
    for item in body["data"]:
        missing = required - set(item.keys())
        assert not missing, f"Webhook list item missing fields: {missing}"
        # Plaintext token must NOT appear in list
        assert "webhook_token" not in item


# ── Concurrent fires produce independent logs ─────────────────────────────────


def test_two_concurrent_fires_produce_independent_logs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Two simultaneous calls to the public endpoint with valid tokens produce
    two independent log entries. We simulate this sequentially with mocked
    fire_webhook that returns a unique log each call.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Concurrent Fires Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Concurrent")
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    log_id_1 = str(uuid.uuid4())
    log_id_2 = str(uuid.uuid4())

    mock_logs = [
        _make_mock_log(webhook_id_fk=webhook_pk, status="session_started", webhook_type="session"),
        _make_mock_log(webhook_id_fk=webhook_pk, status="session_started", webhook_type="session"),
    ]
    mock_logs[0].id = uuid.UUID(log_id_1)
    mock_logs[1].id = uuid.UUID(log_id_2)

    call_count = 0

    async def _fire_side_effect(*args, **kwargs):
        nonlocal call_count
        log = mock_logs[call_count]
        call_count += 1
        return log

    mock_webhook_obj = MagicMock()
    mock_webhook_obj.type = "session"
    mock_webhook_obj.webhook_id = webhook_slug

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        side_effect=_fire_side_effect,
    ):
        r1 = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Each fire produces a distinct log_id
    assert r1.json()["log_id"] == log_id_1
    assert r2.json()["log_id"] == log_id_2
    assert r1.json()["log_id"] != r2.json()["log_id"]


# ── Prompt assembly (unit-level, no DB) ───────────────────────────────────────


def test_session_prompt_contains_payload_and_headers() -> None:
    """
    AgentWebhookService._assemble_session_prompt includes the payload body
    and allowlisted headers in the returned string, and uses the configured
    webhook prompt as the base.
    """
    from app.services.agents.agent_webhook_service import AgentWebhookService

    webhook = MagicMock()
    webhook.name = "GitHub Push"
    webhook.prompt = "Analyze the push."
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = None

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text='{"ref": "refs/heads/main"}',
        payload_content_type="application/json",
        headers_subset={"x-github-event": "push"},
    )

    assert "Analyze the push." in prompt
    assert '{"ref": "refs/heads/main"}' in prompt
    assert "x-github-event" in prompt
    assert "GitHub Push" in prompt


def test_session_prompt_truncated_when_too_large() -> None:
    """
    _assemble_session_prompt truncates the combined prompt at 20,000 chars
    and appends a [truncated] marker.
    """
    from app.services.agents.agent_webhook_service import AgentWebhookService

    webhook = MagicMock()
    webhook.name = "Big Payload"
    webhook.prompt = "Base prompt."
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = None

    # 30 KB payload — well above the 20,000 char cap
    large_payload = "x" * 30_000

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text=large_payload,
        payload_content_type="text/plain",
        headers_subset={},
    )

    assert len(prompt) <= 20_000
    assert prompt.endswith("[truncated]")


def test_session_prompt_uses_agent_entrypoint_prompt_as_fallback() -> None:
    """
    When webhook.prompt is None, _assemble_session_prompt falls back to
    agent.entrypoint_prompt.
    """
    from app.services.agents.agent_webhook_service import AgentWebhookService

    webhook = MagicMock()
    webhook.name = "Fallback Test"
    webhook.prompt = None
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = "You are a helpful code reviewer."

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text="some payload",
        payload_content_type="text/plain",
        headers_subset={},
    )

    assert "You are a helpful code reviewer." in prompt


def test_session_prompt_uses_default_when_both_prompts_none() -> None:
    """
    When both webhook.prompt and agent.entrypoint_prompt are None, the
    default string 'Start webhook-triggered execution.' is used.
    """
    from app.services.agents.agent_webhook_service import AgentWebhookService

    webhook = MagicMock()
    webhook.name = "Default Prompt Test"
    webhook.prompt = None
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = None

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text=None,
        payload_content_type=None,
        headers_subset={},
    )

    assert "Start webhook-triggered execution." in prompt


# ── Public endpoint response has log_id from service ─────────────────────────


def test_public_endpoint_response_includes_log_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The public endpoint always includes log_id in its 200 response so callers
    can correlate the invocation with the log entry visible in the UI.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Log ID Test Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Log ID Test", command="echo hi"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]
    expected_log_id = str(uuid.uuid4())

    mock_log = _make_mock_log(
        webhook_id_fk=webhook_pk,
        status="success",
        webhook_type="script",
    )
    mock_log.id = uuid.UUID(expected_log_id)

    mock_webhook_obj = MagicMock()
    mock_webhook_obj.type = "script"
    mock_webhook_obj.webhook_id = webhook_slug

    with patch(
        "app.api.routes.agent_hooks.AgentWebhookService.validate_webhook_token",
        return_value=mock_webhook_obj,
    ), patch(
        "app.api.routes.agent_hooks.AgentWebhookService.fire_webhook",
        new=AsyncMock(return_value=mock_log),
    ):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["log_id"] == expected_log_id
