"""
Integration tests: schedule types (static_prompt + script_trigger) and execution logs.

Tests the new schedule_type + command fields, validation rules,
schedule log API, and immutability of schedule_type after creation.

  - POST  /api/v1/agents/{id}/schedules            – Create (with type/command validation)
  - GET   /api/v1/agents/{id}/schedules/{sid}/logs – List execution logs
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.schedule import create_schedule
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

_CRON = "0 9 * * 1-5"
_TZ = "UTC"
_DESC = "Weekday morning run"


def _make_agent(client: TestClient, headers: dict) -> dict:
    agent = create_agent_via_api(client, headers, name="ScheduleTypeAgent")
    drain_tasks()
    return agent


def _schedules_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/schedules"


def _logs_url(agent_id: str, schedule_id: str) -> str:
    return f"{API}/agents/{agent_id}/schedules/{schedule_id}/logs"


# ── schedule_type field on create/list ──────────────────────────────────────


def test_create_static_prompt_has_schedule_type_field(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Static prompt schedules include schedule_type='static_prompt' in response."""
    agent = _make_agent(client, superuser_token_headers)
    schedule = create_schedule(
        client, superuser_token_headers, agent["id"],
        name="Static Schedule",
    )
    assert schedule["schedule_type"] == "static_prompt"
    assert schedule["command"] is None


def test_create_script_trigger_schedule(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """script_trigger schedules can be created with a command field."""
    agent = _make_agent(client, superuser_token_headers)
    payload = {
        "name": "Check status",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "script_trigger",
        "command": "bash /app/workspace/scripts/check.sh",
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule_type"] == "script_trigger"
    assert body["command"] == "bash /app/workspace/scripts/check.sh"
    assert body["prompt"] is None


def test_script_trigger_requires_command(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Creating a script_trigger schedule without a command returns 400."""
    agent = _make_agent(client, superuser_token_headers)
    payload = {
        "name": "No command",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "script_trigger",
        # command intentionally omitted
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 400, r.text
    assert "command" in r.json()["detail"].lower()


def test_script_trigger_empty_command_returns_400(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Creating a script_trigger schedule with an empty command returns 400."""
    agent = _make_agent(client, superuser_token_headers)
    payload = {
        "name": "Empty command",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "script_trigger",
        "command": "   ",
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 400, r.text


def test_invalid_schedule_type_returns_400(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Creating a schedule with an unknown schedule_type returns 400."""
    agent = _make_agent(client, superuser_token_headers)
    payload = {
        "name": "Bad type",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "webhook_trigger",
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 400, r.text


def test_static_prompt_defaults(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Schedules created without schedule_type default to static_prompt."""
    agent = _make_agent(client, superuser_token_headers)
    # No schedule_type field in payload
    r = client.post(
        _schedules_url(agent["id"]),
        headers=superuser_token_headers,
        json={
            "name": "Default type",
            "cron_string": _CRON,
            "timezone": _TZ,
            "description": _DESC,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule_type"] == "static_prompt"
    assert body["command"] is None


def test_schedule_type_immutable_on_update(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    schedule_type cannot be changed via update (field is excluded from UpdateScheduleRequest).
    The server ignores it and the type stays as originally created.
    """
    agent = _make_agent(client, superuser_token_headers)
    payload = {
        "name": "Static Schedule",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "static_prompt",
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 200, r.text
    schedule_id = r.json()["id"]

    # Attempt to change schedule_type via update — should be silently ignored
    r2 = client.put(
        f"{_schedules_url(agent['id'])}/{schedule_id}",
        headers=superuser_token_headers,
        json={"schedule_type": "script_trigger"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["schedule_type"] == "static_prompt"


def test_script_trigger_command_visible_in_list(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """script_trigger schedules show command in list response."""
    agent = _make_agent(client, superuser_token_headers)
    cmd = "python /app/workspace/check.py"
    payload = {
        "name": "Script Check",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "script_trigger",
        "command": cmd,
        "enabled": True,
    }
    client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)

    r = client.get(_schedules_url(agent["id"]), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    schedules = r.json()["data"]
    script_schedules = [s for s in schedules if s["schedule_type"] == "script_trigger"]
    assert len(script_schedules) == 1
    assert script_schedules[0]["command"] == cmd


# ── Schedule logs API ────────────────────────────────────────────────────────


def test_schedule_logs_empty_initially(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A newly created schedule has no execution logs."""
    agent = _make_agent(client, superuser_token_headers)
    schedule = create_schedule(
        client, superuser_token_headers, agent["id"], name="Log test"
    )
    r = client.get(
        _logs_url(agent["id"], schedule["id"]),
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == 0
    assert body["data"] == []


def test_schedule_logs_access_denied_for_other_user(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Another user cannot access schedule logs for an agent they don't own."""
    agent = _make_agent(client, superuser_token_headers)
    schedule = create_schedule(
        client, superuser_token_headers, agent["id"], name="Log test"
    )

    other_user, other_headers = create_random_user_with_headers(client)
    r = client.get(
        _logs_url(agent["id"], schedule["id"]),
        headers=other_headers,
    )
    assert r.status_code in (400, 403, 404), r.text


def test_schedule_logs_nonexistent_schedule_returns_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Fetching logs for a non-existent schedule returns 404."""
    agent = _make_agent(client, superuser_token_headers)
    fake_id = str(uuid.uuid4())
    r = client.get(
        _logs_url(agent["id"], fake_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, r.text


def test_schedule_logs_nonexistent_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Fetching logs for a non-existent agent returns 404."""
    fake_agent_id = str(uuid.uuid4())
    fake_schedule_id = str(uuid.uuid4())
    r = client.get(
        _logs_url(fake_agent_id, fake_schedule_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, r.text


def test_schedule_logs_wrong_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Fetching logs from agent_b using a schedule_id from agent_a returns 404."""
    agent_a = _make_agent(client, superuser_token_headers)
    agent_b_data = create_agent_via_api(
        client, superuser_token_headers, name="Agent B"
    )
    drain_tasks()

    schedule = create_schedule(
        client, superuser_token_headers, agent_a["id"], name="Agent A schedule"
    )

    # Try to access the schedule via agent_b
    r = client.get(
        _logs_url(agent_b_data["id"], schedule["id"]),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, r.text


# ── Backward compatibility ───────────────────────────────────────────────────


def test_existing_static_prompt_schedules_still_work(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Schedules created without schedule_type (pre-migration style) work correctly.
    Tests that the default static_prompt type is applied and prompt field still works.
    """
    agent = _make_agent(client, superuser_token_headers)
    # Create with prompt but no schedule_type
    payload = {
        "name": "Legacy schedule",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "prompt": "Do the thing",
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule_type"] == "static_prompt"
    assert body["prompt"] == "Do the thing"
    assert body["command"] is None


def test_script_trigger_update_command(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A script_trigger schedule's command can be updated via PUT."""
    agent = _make_agent(client, superuser_token_headers)
    payload = {
        "name": "Script Check",
        "cron_string": _CRON,
        "timezone": _TZ,
        "description": _DESC,
        "schedule_type": "script_trigger",
        "command": "echo old_command",
        "enabled": True,
    }
    r = client.post(_schedules_url(agent["id"]), headers=superuser_token_headers, json=payload)
    assert r.status_code == 200, r.text
    schedule_id = r.json()["id"]

    r2 = client.put(
        f"{_schedules_url(agent['id'])}/{schedule_id}",
        headers=superuser_token_headers,
        json={"command": "echo new_command"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["command"] == "echo new_command"
    assert r2.json()["schedule_type"] == "script_trigger"
