"""HTTP wrappers for the agent addons projection.

    GET  /agents/{id}/addons          — cached, never wakes a container
    POST /agents/{id}/addons/refresh  — re-read the index, then re-project
"""
from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR


def get_agent_addons(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.get(f"{API}/agents/{agent_id}/addons", headers=headers)
    assert r.status_code == expected_status, (
        f"get addons: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def refresh_agent_addons(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.post(f"{API}/agents/{agent_id}/addons/refresh", headers=headers)
    assert r.status_code == expected_status, (
        f"refresh addons: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def addons_by_name(payload: dict) -> dict[str, dict]:
    """Rows keyed by their engine-facing ``name``."""
    rows = {row["name"]: row for row in payload["addons"]}
    assert len(rows) == len(payload["addons"]), (
        f"two rows share a name: {[r['name'] for r in payload['addons']]}"
    )
    return rows


def skill_names(row: dict) -> list[str]:
    """The names of the skills folded into one addon row."""
    return [skill["name"] for skill in row["skills"]]
