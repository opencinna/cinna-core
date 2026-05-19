"""Helper functions for managing agent schedules via API in tests."""
from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR

# A valid UTC CRON expression used as a safe default in helpers.
# "0 9 * * 1-5" = every weekday at 09:00 UTC.
_DEFAULT_CRON = "0 9 * * 1-5"
_DEFAULT_TZ = "UTC"
_DEFAULT_DESC = "Weekday morning run"


def _schedules_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/schedules"


def _schedule_url(agent_id: str, schedule_id: str) -> str:
    return f"{API}/agents/{agent_id}/schedules/{schedule_id}"


def generate_schedule(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    natural_language: str,
    timezone: str = _DEFAULT_TZ,
) -> dict:
    """Call the AI-powered schedule generation endpoint.

    Asserts 200 on success.  Use inline client.post() calls to test errors.
    """
    r = client.post(
        f"{_schedules_url(agent_id)}/generate",
        headers=headers,
        json={"natural_language": natural_language, "timezone": timezone},
    )
    assert r.status_code == 200, f"Generate schedule failed: {r.text}"
    return r.json()


def create_schedule(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    name: str = "Test Schedule",
    cron_string: str = _DEFAULT_CRON,
    timezone: str = _DEFAULT_TZ,
    description: str = _DEFAULT_DESC,
    prompt: str | None = None,
    enabled: bool = True,
) -> dict:
    """Create an agent schedule and return the parsed response body.

    Asserts 200 on success.  Use inline client.post() calls when you need to
    test non-200 responses (validation errors, permission errors, etc.).
    """
    payload: dict = {
        "name": name,
        "cron_string": cron_string,
        "timezone": timezone,
        "description": description,
        "enabled": enabled,
    }
    if prompt is not None:
        payload["prompt"] = prompt

    r = client.post(_schedules_url(agent_id), headers=headers, json=payload)
    assert r.status_code == 200, f"Create schedule failed: {r.text}"
    return r.json()


def list_schedules(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> list[dict]:
    """List all schedules for agent and return the ``data`` array.

    Asserts 200 and that count matches len(data).
    """
    r = client.get(_schedules_url(agent_id), headers=headers)
    assert r.status_code == 200, f"List schedules failed: {r.text}"
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]


def update_schedule(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    schedule_id: str,
    **fields,
) -> dict:
    """Partially update a schedule (PUT).  Only supplied kwargs are sent.

    Asserts 200 on success.  Use inline client.put() calls to test errors.
    """
    r = client.put(
        _schedule_url(agent_id, schedule_id),
        headers=headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update schedule failed: {r.text}"
    return r.json()


def delete_schedule(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    schedule_id: str,
) -> dict:
    """Delete a schedule and return the response body.

    Asserts 200 on success.  Use inline client.delete() calls to test errors.
    """
    r = client.delete(_schedule_url(agent_id, schedule_id), headers=headers)
    assert r.status_code == 200, f"Delete schedule failed: {r.text}"
    return r.json()


def run_schedule_now(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    schedule_id: str,
) -> dict:
    """Trigger a schedule to execute immediately (POST …/run).

    Asserts 200 on success.  Use inline client.post() calls to test error
    responses (400 no-env, 404 non-existent, permission errors, etc.).

    Returns the parsed response body (a ``Message`` with ``message`` field).
    """
    r = client.post(
        f"{_schedule_url(agent_id, schedule_id)}/run",
        headers=headers,
    )
    assert r.status_code == 200, f"run_schedule_now failed: {r.text}"
    return r.json()


def get_schedule_logs(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    schedule_id: str,
) -> list[dict]:
    """Fetch execution logs for a schedule and return the ``data`` list.

    Asserts 200 and that count matches len(data).
    Use inline client.get() calls to test error responses.
    """
    r = client.get(
        f"{_schedule_url(agent_id, schedule_id)}/logs",
        headers=headers,
    )
    assert r.status_code == 200, f"get_schedule_logs failed: {r.text}"
    body = r.json()
    assert "data" in body and "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]
