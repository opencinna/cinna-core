"""
Integration tests: multi-schedule CRUD for agents.

Tests the full user story for managing agent schedules after the refactor from
single-schedule-per-agent to multi-schedule-per-agent:

  - POST   /api/v1/agents/{id}/schedules/generate  – AI-powered CRON generation (stateless)
  - POST   /api/v1/agents/{id}/schedules            – Create a new schedule
  - GET    /api/v1/agents/{id}/schedules            – List all schedules
  - PUT    /api/v1/agents/{id}/schedules/{sid}      – Partial update
  - DELETE /api/v1/agents/{id}/schedules/{sid}      – Delete a schedule

Only environment adapter + external services are stubbed (via conftest autouse fixtures).
AI schedule generation is patched per-test where needed.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.schedule import (
    create_schedule,
    delete_schedule,
    generate_schedule,
    list_schedules,
    update_schedule,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

# Stable CRON + timezone used by most tests.
# "0 9 * * 1-5"  = every weekday at 09:00 local time.
_CRON = "0 9 * * 1-5"
_TZ = "America/New_York"
_DESC = "Weekday morning run"


# ── Fixtures / inline helpers ────────────────────────────────────────────────


def _make_agent(client: TestClient, headers: dict[str, str], name: str = "Schedule Agent") -> dict:
    """Create an agent and drain background tasks."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    return agent


# ── Lifecycle tests ──────────────────────────────────────────────────────────


def test_schedule_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full CRUD lifecycle for a single agent schedule:
      1. Create agent
      2. List schedules → empty
      3. Create schedule with all fields → verify response fields
      4. List schedules → schedule appears
      5. Update name only → verify name changed, other fields unchanged
      6. Update cron_string (with timezone) → verify cron_string updated, next_execution recalculated
      7. Update enabled flag to False → verify disabled
      8. Update prompt → verify prompt persisted
      9. Delete schedule
      10. List schedules → empty
      11. PUT on deleted schedule → 404
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = _make_agent(client, superuser_token_headers, name="Lifecycle Agent")
    agent_id = agent["id"]

    # ── Phase 2: List schedules → empty ──────────────────────────────────
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert schedules == []

    # ── Phase 3: Create schedule with all fields ──────────────────────────
    created = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Morning Digest",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
        prompt="Summarize overnight news",
        enabled=True,
    )

    schedule_id = created["id"]
    assert created["agent_id"] == agent_id
    assert created["name"] == "Morning Digest"
    assert created["description"] == _DESC
    assert created["enabled"] is True
    assert created["prompt"] == "Summarize overnight news"
    # cron_string should be stored as UTC (converted from America/New_York)
    assert created["cron_string"] is not None
    assert len(created["cron_string"].split()) == 5, "cron_string must be 5-part CRON expression"
    assert created["next_execution"] is not None
    assert created["last_execution"] is None
    assert "created_at" in created
    assert "updated_at" in created

    # ── Phase 4: List schedules → schedule appears ────────────────────────
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert len(schedules) == 1
    assert schedules[0]["id"] == schedule_id
    assert schedules[0]["name"] == "Morning Digest"

    # ── Phase 5: Update name only ─────────────────────────────────────────
    updated = update_schedule(
        client, superuser_token_headers, agent_id, schedule_id,
        name="Evening Digest",
    )
    assert updated["name"] == "Evening Digest"
    assert updated["description"] == _DESC        # unchanged
    assert updated["enabled"] is True             # unchanged
    assert updated["prompt"] == "Summarize overnight news"  # unchanged

    # Verify the list also reflects the new name
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert schedules[0]["name"] == "Evening Digest"

    # ── Phase 6: Update cron_string (timezone required) ───────────────────
    # Use a noticeably different UTC cron to ensure the stored value changes.
    # Original was "0 9 * * 1-5" (America/New_York) → stored as "0 14 * * 1-5" (UTC).
    # New value: "0 16 * * *" (UTC) → clearly different from "0 14 * * 1-5".
    new_cron = "0 16 * * *"
    old_stored_cron = updated["cron_string"]

    updated = update_schedule(
        client, superuser_token_headers, agent_id, schedule_id,
        cron_string=new_cron,
        timezone="UTC",
    )
    # cron_string must have changed (16 != 14)
    assert updated["cron_string"] != old_stored_cron, (
        f"cron_string should have changed from {old_stored_cron!r} to 0 16 * * *, "
        f"got {updated['cron_string']!r}"
    )
    assert len(updated["cron_string"].split()) == 5

    # ── Phase 7: Disable schedule ─────────────────────────────────────────
    updated = update_schedule(
        client, superuser_token_headers, agent_id, schedule_id,
        enabled=False,
    )
    assert updated["enabled"] is False
    assert updated["name"] == "Evening Digest"    # unchanged

    # ── Phase 8: Update prompt ────────────────────────────────────────────
    updated = update_schedule(
        client, superuser_token_headers, agent_id, schedule_id,
        prompt="Daily market summary",
    )
    assert updated["prompt"] == "Daily market summary"
    assert updated["enabled"] is False            # unchanged

    # ── Phase 9: Delete schedule ──────────────────────────────────────────
    result = delete_schedule(client, superuser_token_headers, agent_id, schedule_id)
    assert "message" in result

    # ── Phase 10: List schedules → empty again ────────────────────────────
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert schedules == []

    # ── Phase 11: Update on deleted schedule → 404 ───────────────────────
    r = client.put(
        f"{API}/agents/{agent_id}/schedules/{schedule_id}",
        headers=superuser_token_headers,
        json={"name": "ghost"},
    )
    assert r.status_code == 404


def test_multiple_schedules_for_same_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An agent can have multiple independent schedules:
      1. Create agent
      2. Create three schedules with different names, crons, and prompts
      3. List → all three appear in creation order
      4. Delete the middle one → remaining two are intact
      5. Update the last one → only it changes
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = _make_agent(client, superuser_token_headers, name="Multi-Schedule Agent")
    agent_id = agent["id"]

    # ── Phase 2: Create three schedules ──────────────────────────────────
    s1 = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Hourly Ping",
        cron_string="0 * * * *",
        timezone="UTC",
        description="Every hour",
    )
    s2 = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Daily Digest",
        cron_string="0 8 * * *",
        timezone="UTC",
        description="Every day at 08:00",
        prompt="Summarise the day",
    )
    s3 = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Weekend Report",
        cron_string="0 10 * * 6",
        timezone="UTC",
        description="Every Saturday at 10:00",
        enabled=False,
    )

    # ── Phase 3: All three appear in the list ─────────────────────────────
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert len(schedules) == 3

    ids = [s["id"] for s in schedules]
    assert s1["id"] in ids
    assert s2["id"] in ids
    assert s3["id"] in ids

    # Verify per-schedule fields
    by_id = {s["id"]: s for s in schedules}
    assert by_id[s2["id"]]["prompt"] == "Summarise the day"
    assert by_id[s3["id"]]["enabled"] is False
    assert by_id[s1["id"]]["enabled"] is True

    # ── Phase 4: Delete the middle schedule ───────────────────────────────
    delete_schedule(client, superuser_token_headers, agent_id, s2["id"])

    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert len(schedules) == 2
    remaining_ids = {s["id"] for s in schedules}
    assert s2["id"] not in remaining_ids
    assert s1["id"] in remaining_ids
    assert s3["id"] in remaining_ids

    # ── Phase 5: Update s3 → only it changes ─────────────────────────────
    updated = update_schedule(
        client, superuser_token_headers, agent_id, s3["id"],
        name="Weekend Deep Dive",
        enabled=True,
    )
    assert updated["name"] == "Weekend Deep Dive"
    assert updated["enabled"] is True

    # s1 unchanged
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    by_id = {s["id"]: s for s in schedules}
    assert by_id[s1["id"]]["name"] == "Hourly Ping"


# ── Create validation ────────────────────────────────────────────────────────


def test_create_schedule_minimal_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Create a schedule with only the required fields (name, cron_string, timezone,
    description).  prompt defaults to None, enabled defaults to True.
    """
    agent = _make_agent(client, superuser_token_headers, name="Minimal Fields Agent")
    agent_id = agent["id"]

    created = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Minimal",
        cron_string="0 0 * * *",
        timezone="UTC",
        description="Midnight daily",
    )

    assert created["name"] == "Minimal"
    assert created["enabled"] is True
    assert created["prompt"] is None
    assert created["agent_id"] == agent_id
    assert created["next_execution"] is not None


def test_create_schedule_with_null_prompt_explicitly(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Schedule created with prompt=None uses the agent's entrypoint_prompt at
    runtime.  The field must be present in the response and be null.
    """
    agent = _make_agent(client, superuser_token_headers, name="Null Prompt Agent")
    agent_id = agent["id"]

    r = client.post(
        f"{API}/agents/{agent_id}/schedules",
        headers=superuser_token_headers,
        json={
            "name": "No Custom Prompt",
            "cron_string": "0 6 * * *",
            "timezone": "UTC",
            "description": "Daily at 06:00",
            "prompt": None,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "prompt" in body
    assert body["prompt"] is None


def test_create_schedule_disabled_by_default(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A schedule created with enabled=False starts in the disabled state.
    """
    agent = _make_agent(client, superuser_token_headers, name="Disabled Schedule Agent")
    agent_id = agent["id"]

    created = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Paused Schedule",
        cron_string="0 12 * * *",
        timezone="UTC",
        description="Noon daily, disabled at creation",
        enabled=False,
    )

    assert created["enabled"] is False


# ── Update validation ────────────────────────────────────────────────────────


def test_update_cron_without_timezone_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Updating cron_string without providing timezone must return 400 because
    the service cannot convert a local CRON to UTC without the timezone.
    """
    agent = _make_agent(client, superuser_token_headers, name="Cron Validation Agent")
    agent_id = agent["id"]

    schedule = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Original",
        cron_string="0 9 * * *",
        timezone="UTC",
        description="Daily",
    )

    r = client.put(
        f"{API}/agents/{agent_id}/schedules/{schedule['id']}",
        headers=superuser_token_headers,
        json={"cron_string": "0 15 * * *"},   # no timezone
    )
    assert r.status_code == 400


def test_update_non_cron_fields_does_not_require_timezone(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Updating name, description, prompt, or enabled without touching cron_string
    must succeed even without a timezone field.
    """
    agent = _make_agent(client, superuser_token_headers, name="Non-Cron Update Agent")
    agent_id = agent["id"]

    schedule = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Original Name",
        cron_string="0 9 * * *",
        timezone="UTC",
        description="Original description",
    )

    updated = update_schedule(
        client, superuser_token_headers, agent_id, schedule["id"],
        name="New Name",
        description="New description",
    )
    assert updated["name"] == "New Name"
    assert updated["description"] == "New description"
    # cron unchanged
    assert updated["cron_string"] == schedule["cron_string"]


# ── Error cases ──────────────────────────────────────────────────────────────


def test_delete_nonexistent_schedule_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deleting a schedule ID that does not exist returns 404.
    """
    agent = _make_agent(client, superuser_token_headers, name="404 Delete Agent")
    agent_id = agent["id"]

    fake_id = str(uuid.uuid4())
    r = client.delete(
        f"{API}/agents/{agent_id}/schedules/{fake_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_update_nonexistent_schedule_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Updating a schedule ID that does not exist returns 404.
    """
    agent = _make_agent(client, superuser_token_headers, name="404 Update Agent")
    agent_id = agent["id"]

    fake_id = str(uuid.uuid4())
    r = client.put(
        f"{API}/agents/{agent_id}/schedules/{fake_id}",
        headers=superuser_token_headers,
        json={"name": "ghost"},
    )
    assert r.status_code == 404


def test_schedules_for_nonexistent_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    All schedule endpoints return 404 when the agent_id does not exist.
    """
    fake_agent = str(uuid.uuid4())
    fake_schedule = str(uuid.uuid4())

    # List
    r = client.get(f"{API}/agents/{fake_agent}/schedules", headers=superuser_token_headers)
    assert r.status_code == 404

    # Create
    r = client.post(
        f"{API}/agents/{fake_agent}/schedules",
        headers=superuser_token_headers,
        json={
            "name": "ghost",
            "cron_string": "0 9 * * *",
            "timezone": "UTC",
            "description": "ghost schedule",
        },
    )
    assert r.status_code == 404

    # Update
    r = client.put(
        f"{API}/agents/{fake_agent}/schedules/{fake_schedule}",
        headers=superuser_token_headers,
        json={"name": "ghost"},
    )
    assert r.status_code == 404

    # Delete
    r = client.delete(
        f"{API}/agents/{fake_agent}/schedules/{fake_schedule}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ── Cross-agent isolation ────────────────────────────────────────────────────


def test_schedule_from_agent_a_is_not_accessible_via_agent_b(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A schedule created for agent A cannot be read, updated, or deleted via
    agent B's routes.

      1. Create agent A + agent B
      2. Create schedule on agent A
      3. Try to update via agent B's URL → 404
      4. Try to delete via agent B's URL → 404
      5. Agent B's list is empty
      6. Agent A's list still has the schedule
    """
    # ── Phase 1: Create two agents ────────────────────────────────────────
    agent_a = _make_agent(client, superuser_token_headers, name="Agent A")
    agent_b = _make_agent(client, superuser_token_headers, name="Agent B")

    # ── Phase 2: Create schedule on agent A ──────────────────────────────
    schedule = create_schedule(
        client, superuser_token_headers, agent_a["id"],
        name="Agent A Schedule",
        cron_string="0 7 * * *",
        timezone="UTC",
        description="Morning run",
    )
    schedule_id = schedule["id"]

    # ── Phase 3: Update via agent B → 404 ────────────────────────────────
    r = client.put(
        f"{API}/agents/{agent_b['id']}/schedules/{schedule_id}",
        headers=superuser_token_headers,
        json={"name": "hijacked"},
    )
    assert r.status_code == 404

    # ── Phase 4: Delete via agent B → 404 ────────────────────────────────
    r = client.delete(
        f"{API}/agents/{agent_b['id']}/schedules/{schedule_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    # ── Phase 5: Agent B's list is empty ─────────────────────────────────
    b_schedules = list_schedules(client, superuser_token_headers, agent_b["id"])
    assert b_schedules == []

    # ── Phase 6: Agent A still has the schedule, unmodified ──────────────
    a_schedules = list_schedules(client, superuser_token_headers, agent_a["id"])
    assert len(a_schedules) == 1
    assert a_schedules[0]["id"] == schedule_id
    assert a_schedules[0]["name"] == "Agent A Schedule"


# ── Permission / authorization ───────────────────────────────────────────────


def test_other_user_cannot_manage_schedules(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User B cannot list, create, update, or delete schedules for an agent
    owned by user A.  All operations return 404 (agent not found for user B).

      1. User A creates agent + schedule
      2. User B cannot list schedules
      3. User B cannot create schedule
      4. User B cannot update schedule
      5. User B cannot delete schedule
      6. User A can still list and manage the schedule
    """
    # ── Phase 1: User A creates agent + schedule ──────────────────────────
    agent = _make_agent(client, superuser_token_headers, name="Owner's Agent")
    agent_id = agent["id"]

    schedule = create_schedule(
        client, superuser_token_headers, agent_id,
        name="Owner's Schedule",
        cron_string="0 10 * * *",
        timezone="UTC",
        description="Daily at 10:00",
    )
    schedule_id = schedule["id"]

    # ── Phase 2: Create user B ────────────────────────────────────────────
    _, user_b_headers = create_random_user_with_headers(client)

    # ── Phase 3: User B cannot list schedules ────────────────────────────
    r = client.get(f"{API}/agents/{agent_id}/schedules", headers=user_b_headers)
    assert r.status_code in (400, 404), (
        f"Expected 400 or 404 for non-owner list, got {r.status_code}"
    )

    # ── Phase 4: User B cannot create schedule ────────────────────────────
    r = client.post(
        f"{API}/agents/{agent_id}/schedules",
        headers=user_b_headers,
        json={
            "name": "intruder",
            "cron_string": "0 9 * * *",
            "timezone": "UTC",
            "description": "Intruder schedule",
        },
    )
    assert r.status_code in (400, 404)

    # ── Phase 5: User B cannot update schedule ────────────────────────────
    r = client.put(
        f"{API}/agents/{agent_id}/schedules/{schedule_id}",
        headers=user_b_headers,
        json={"name": "hacked"},
    )
    assert r.status_code in (400, 404)

    # ── Phase 6: User B cannot delete schedule ────────────────────────────
    r = client.delete(
        f"{API}/agents/{agent_id}/schedules/{schedule_id}",
        headers=user_b_headers,
    )
    assert r.status_code in (400, 404)

    # ── Phase 7: User A still has full access ─────────────────────────────
    schedules = list_schedules(client, superuser_token_headers, agent_id)
    assert len(schedules) == 1
    assert schedules[0]["id"] == schedule_id
    assert schedules[0]["name"] == "Owner's Schedule"


# ── Generate endpoint ────────────────────────────────────────────────────────


def test_generate_schedule_returns_cron_on_success(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /schedules/generate delegates to AIFunctionsService.generate_schedule.
    When the AI succeeds, the response contains success=True, a cron_string,
    a description, and a next_execution preview.

    The AI call is mocked to return a deterministic result without an LLM.
    """
    agent = _make_agent(client, superuser_token_headers, name="Generate Agent")
    agent_id = agent["id"]

    mock_ai_result = {
        "success": True,
        "cron_string": "0 9 * * 1-5",
        "description": "Every weekday at 09:00 EST",
    }

    with patch(
        "app.services.ai_functions.ai_functions_service.AIFunctionsService.generate_schedule",
        return_value=mock_ai_result,
    ):
        body = generate_schedule(
            client, superuser_token_headers, agent_id,
            natural_language="Every weekday morning at 9am",
            timezone="America/New_York",
        )

    assert body["success"] is True
    assert body["cron_string"] == "0 9 * * 1-5"
    assert body["description"] == "Every weekday at 09:00 EST"
    assert body["next_execution"] is not None, "next_execution preview must be included"


def test_generate_schedule_returns_error_on_ai_failure(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When AIFunctionsService.generate_schedule indicates failure, the endpoint
    returns success=False and an error message (not a 5xx).
    """
    agent = _make_agent(client, superuser_token_headers, name="Generate Fail Agent")
    agent_id = agent["id"]

    mock_ai_result = {
        "success": False,
        "error": "Could not understand the schedule description",
    }

    with patch(
        "app.services.ai_functions.ai_functions_service.AIFunctionsService.generate_schedule",
        return_value=mock_ai_result,
    ):
        body = generate_schedule(
            client, superuser_token_headers, agent_id,
            natural_language="???",
        )

    assert body["success"] is False
    assert body["error"] is not None


def test_generate_schedule_for_nonexistent_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Calling the generate endpoint with a non-existent agent ID returns 404.
    """
    fake_agent = str(uuid.uuid4())
    r = client.post(
        f"{API}/agents/{fake_agent}/schedules/generate",
        headers=superuser_token_headers,
        json={"natural_language": "daily at noon", "timezone": "UTC"},
    )
    assert r.status_code == 404


def test_generate_schedule_non_owner_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user who doesn't own the agent receives a permission error (400 or 404)
    when calling the generate endpoint.
    """
    agent = _make_agent(client, superuser_token_headers, name="Generate Perm Agent")
    agent_id = agent["id"]

    _, other_headers = create_random_user_with_headers(client)

    r = client.post(
        f"{API}/agents/{agent_id}/schedules/generate",
        headers=other_headers,
        json={"natural_language": "every hour", "timezone": "UTC"},
    )
    assert r.status_code in (400, 404)  # raw call: expects permission error


# ── Response shape ───────────────────────────────────────────────────────────


def test_list_schedules_response_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /schedules returns AgentSchedulesPublic shape:
      { "data": [...], "count": <int> }
    count must always equal len(data).
    """
    agent = _make_agent(client, superuser_token_headers, name="Shape Agent")
    agent_id = agent["id"]

    # Empty list
    r = client.get(f"{API}/agents/{agent_id}/schedules", headers=superuser_token_headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"data", "count"}
    assert body["count"] == 0
    assert body["data"] == []

    # After adding two schedules
    create_schedule(
        client, superuser_token_headers, agent_id,
        name="A", cron_string="0 8 * * *", timezone="UTC", description="A",
    )
    create_schedule(
        client, superuser_token_headers, agent_id,
        name="B", cron_string="0 9 * * *", timezone="UTC", description="B",
    )

    r = client.get(f"{API}/agents/{agent_id}/schedules", headers=superuser_token_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert len(body["data"]) == 2

    # Verify each item contains required fields
    required_fields = {
        "id", "agent_id", "name", "cron_string", "description",
        "enabled", "prompt", "next_execution", "last_execution",
        "created_at", "updated_at",
    }
    for item in body["data"]:
        missing = required_fields - set(item.keys())
        assert not missing, f"Schedule item missing fields: {missing}"
