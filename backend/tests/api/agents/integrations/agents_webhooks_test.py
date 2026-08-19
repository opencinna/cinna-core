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

The public endpoint uses ``create_session()`` (patched to the test session in
the agents conftest), so it operates on the same rolled-back transaction as the
CRUD endpoints. We therefore hit the real public endpoint with the real token
and verify the real ``AgentWebhookLog`` rows it produces (via the logs API) —
no service-layer mocking. Script-type fires patch only the agent-env connector's
``exec_command`` (the one true external boundary — there is no real container in
tests); session-type fires run the real session/message creation path.
"""
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers
from tests.utils.webhook import (
    create_script_webhook,
    create_session_webhook,
    delete_webhook,
    get_webhook,
    list_webhook_logs,
    list_webhooks,
    regenerate_token,
    update_webhook,
)

API = settings.API_V1_STR

# Patch target for the agent-env connector used by script-type webhook fires.
_EXEC_TARGET = (
    "app.services.environments.agent_env_connector.agent_env_connector.exec_command"
)


def _stub_exec_command(
    *, exit_code: int = 0, stdout: str = "", stderr: str = ""
) -> AsyncMock:
    """Build an AsyncMock standing in for ``agent_env_connector.exec_command``.

    Script-type webhook fires shell out to the agent environment over HTTP;
    there is no real container in tests, so this is the single external boundary
    we replace. Everything else (token validation, log creation, env resolution)
    runs for real against the test transaction.
    """
    return AsyncMock(
        return_value={"exit_code": exit_code, "stdout": stdout, "stderr": stderr}
    )

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
    Regenerating a token (verified end-to-end against the real public endpoint):
      1. Create webhook, save original token + webhook_id + prefix
      2. Regenerate — response includes webhook_token (new plaintext)
      3. Same webhook_id slug (URL unchanged)
      4. New token != old token; new prefix != old prefix
      5. Fire the real public endpoint with the OLD token → 401 (rejected)
      6. Fire the real public endpoint with the NEW token → 200, real log row
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Regen Token Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Regen Hook", command="echo hi"
    )

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
    assert new_prefix != original_prefix, "Token prefix must change after regenerate"
    assert new_token.startswith(new_prefix), "New prefix must be first 8 chars of new token"

    # ── Old token is rejected by the real public endpoint → 401 ───────────
    r_old = client.post(
        _public_url(webhook_slug),
        headers={"Authorization": f"Bearer {original_token}"},
    )
    assert r_old.status_code == 401, (
        f"Old token should be rejected after regenerate, got {r_old.status_code}: {r_old.text}"
    )

    # ── New token is accepted → 200 and produces a real log row ───────────
    with patch(_EXEC_TARGET, _stub_exec_command(exit_code=0, stdout="ok\n")):
        r_new = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {new_token}"},
        )
    assert r_new.status_code == 200, (
        f"New token should be accepted after regenerate, got {r_new.status_code}: {r_new.text}"
    )
    assert r_new.json()["success"] is True
    new_log_id = r_new.json()["log_id"]
    assert new_log_id is not None

    # The fire is recorded as a real AgentWebhookLog visible via the logs API.
    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    assert any(log["id"] == new_log_id for log in logs), (
        "The successful fire with the new token must appear in the webhook logs"
    )


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
    are accepted by the real public endpoint and each produces a real log row.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Dual Auth Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Dual Auth Hook", command="echo hi"
    )
    webhook_slug = webhook["webhook_id"]
    token = webhook["webhook_token"]
    webhook_pk = webhook["id"]

    with patch(_EXEC_TARGET, _stub_exec_command(exit_code=0, stdout="ok\n")):
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

    # Both fires produced distinct real log rows.
    log_ids = {r1.json()["log_id"], r2.json()["log_id"]}
    assert None not in log_ids
    assert len(log_ids) == 2, "Each fire must produce a distinct log row"
    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    persisted = {log["id"] for log in logs}
    assert log_ids <= persisted, "Both fire log rows must be persisted and visible via the logs API"


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
    POST /agent-hooks/{id} on a real, existing webhook with the WRONG token → 401.

    The real service decrypts the stored Fernet ciphertext and timing-safe
    compares it against the provided token; a mismatch raises
    WebhookTokenInvalidError, which the route maps to 401. No mocking — the
    wrong token is rejected for real, and no log row is created.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Token Mismatch Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Mismatch Hook")
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    r = client.post(
        _public_url(webhook_slug),
        headers={"Authorization": "Bearer wrongtoken123"},
    )
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()

    # A rejected token never fires the webhook — no log row exists.
    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    assert logs == [], "A token mismatch must not create a webhook log row"


# ── Public endpoint — execution outcomes (real fire, log verified via API) ───


def test_public_endpoint_session_type_success_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A real session-type webhook fire creates a session and a real log row:
      { "success": true, "webhook_type": "session", "log_id": "<uuid>" }
    The log is then visible via the logs API with status "session_started".
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Session Fire Agent")
    webhook = create_session_webhook(client, headers, agent["id"], name="Session Fire")
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    r = client.post(
        _public_url(webhook_slug),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        content=b'{"event": "push"}',
    )
    drain_tasks()

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["webhook_type"] == "session"
    log_id = body["log_id"]
    assert log_id is not None

    # The fire created a real log row reachable via the logs API.
    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    log = next((l for l in logs if l["id"] == log_id), None)
    assert log is not None, "Session fire must persist a real log row"
    assert log["status"] == "session_started"
    assert log["webhook_type"] == "session"
    assert log["session_id"] is not None, "A session-started log references its session"


def test_public_endpoint_script_type_exit0_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A real script-type webhook fire (exit 0) records a "success" log row:
      { "success": true, "webhook_type": "script", "log_id": "<uuid>" }
    Only ``exec_command`` (the container boundary) is stubbed.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Script Fire Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Script Fire", command="echo ok"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    with patch(_EXEC_TARGET, _stub_exec_command(exit_code=0, stdout="ok\n")):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["webhook_type"] == "script"
    log_id = body["log_id"]
    assert log_id is not None

    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    log = next((l for l in logs if l["id"] == log_id), None)
    assert log is not None
    assert log["status"] == "success"
    assert log["command_exit_code"] == 0
    assert log["command_output"] == "ok\n"


def test_public_endpoint_script_nonzero_exit_returns_200_with_log(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Script-type webhook with non-zero exit: HTTP is still 200 (caller gets a
    receipt) and the real log row has status "script_error" with the captured
    stderr and exit code.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="NonZero Exit Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="NonZero Exit", command="exit 1"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    with patch(
        _EXEC_TARGET,
        _stub_exec_command(exit_code=1, stderr="error: command failed\n"),
    ):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["webhook_type"] == "script"
    log_id = body["log_id"]
    assert log_id is not None

    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    log = next((l for l in logs if l["id"] == log_id), None)
    assert log is not None
    assert log["status"] == "script_error"
    assert log["command_exit_code"] == 1
    assert log["command_stderr"] == "error: command failed\n"


def test_public_endpoint_error_status_still_returns_200(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Infrastructure error (script webhook with no active environment) still
    returns HTTP 200 with a real log row whose status is "error". The agent's
    environment is deactivated via the env-management API so ``_fire_script``
    hits the "no active environment" branch for real.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Error Status Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Error Status", command="echo hi"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    # Tear down the active environment so the script fire finds no env.
    # delete_environment clears agent.active_environment_id, so
    # get_active_environment() returns None and _fire_script logs "error".
    # The active env id is assigned during the (drained) startup background
    # task, so re-fetch the agent to read it.
    env_id = get_agent(client, headers, agent["id"]).get("active_environment_id")
    assert env_id, "Agent must start with an active environment"
    r_del = client.delete(f"{API}/environments/{env_id}", headers=headers)
    assert r_del.status_code in (200, 204), f"Env teardown failed: {r_del.text}"

    r = client.post(
        _public_url(webhook_slug),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    log_id = body["log_id"]
    assert log_id is not None

    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    log = next((l for l in logs if l["id"] == log_id), None)
    assert log is not None
    assert log["status"] == "error"
    assert log["error_message"] is not None


# Unit tests for AgentWebhookService.filter_headers (header allowlist) and
# _assemble_session_prompt (prompt assembly) live in
# tests/unit/test_agent_webhook_helpers.py. This file covers the API-observable
# public-endpoint fire flow.


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
    Two successive fires of the same webhook produce two independent, real log
    rows (distinct ids, both persisted and visible via the logs API). No mocking
    of the service — only the script ``exec_command`` boundary is stubbed.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Concurrent Fires Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Concurrent", command="echo hi"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    with patch(_EXEC_TARGET, _stub_exec_command(exit_code=0, stdout="hi\n")):
        r1 = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    log_id_1 = r1.json()["log_id"]
    log_id_2 = r2.json()["log_id"]
    assert log_id_1 and log_id_2
    assert log_id_1 != log_id_2, "Each fire must produce a distinct log id"

    # Both fires are persisted as independent rows reachable via the logs API.
    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    persisted_ids = {log["id"] for log in logs}
    assert {log_id_1, log_id_2} <= persisted_ids, (
        "Both independent fire log rows must be persisted and visible via the logs API"
    )
    assert len([l for l in logs if l["id"] in {log_id_1, log_id_2}]) == 2


# ── Public endpoint response has log_id from service ─────────────────────────


def test_public_endpoint_response_includes_log_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The public endpoint's 200 response includes the log_id of the real log row
    it just created, so callers can correlate the invocation with the log entry
    visible in the UI. The returned id resolves to an actual row via the logs API.
    """
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Log ID Test Agent")
    webhook = create_script_webhook(
        client, headers, agent["id"], name="Log ID Test", command="echo hi"
    )
    token = webhook["webhook_token"]
    webhook_slug = webhook["webhook_id"]
    webhook_pk = webhook["id"]

    with patch(_EXEC_TARGET, _stub_exec_command(exit_code=0, stdout="hi\n")):
        r = client.post(
            _public_url(webhook_slug),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200, r.text
    log_id = r.json()["log_id"]
    assert log_id is not None

    logs = list_webhook_logs(client, headers, agent["id"], webhook_pk)
    assert any(log["id"] == log_id for log in logs), (
        "The response log_id must correspond to a real persisted log row"
    )
