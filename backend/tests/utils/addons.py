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


# ── Addons directory (K3) and MCP directory (K4) ───────────────────────────

DIRECTORY_URL = f"{API}/addons/directory"
MCP_DIR = f"{API}/mcp-directory"


def get_directory(
    client: TestClient,
    headers: dict[str, str],
    *,
    expected_status: int = 200,
    **params,
) -> dict:
    """GET ``/addons/directory`` (search, kind, category, source, runs_on, skip, limit)."""
    r = client.get(DIRECTORY_URL, headers=headers, params=params)
    assert r.status_code == expected_status, (
        f"directory: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def directory_names(payload: dict) -> list[str]:
    return [e["name"] for e in payload["data"]]


def directory_entry(payload: dict, name: str) -> dict | None:
    return next((e for e in payload["data"] if e["name"] == name), None)


def mcp_http_body(name: str, **overrides) -> dict:
    """A valid manual http entry body."""
    body: dict = {
        "name": name,
        "title": f"Title of {name}",
        "transport": "http",
        "url": f"https://{name}.example.com/mcp",
        "auth": "none",
    }
    body.update(overrides)
    return body


def create_mcp_entry(
    client: TestClient,
    headers: dict[str, str],
    body: dict,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.post(f"{MCP_DIR}/entries", headers=headers, json=body)
    assert r.status_code == expected_status, (
        f"create mcp entry: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def list_mcp_entries(client: TestClient, headers: dict[str, str]) -> list[dict]:
    r = client.get(f"{MCP_DIR}/entries", headers=headers)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["count"] == len(payload["data"])
    return payload["data"]


def set_mcp_access(
    client: TestClient,
    headers: dict[str, str],
    entry_id: str,
    access: str,
    *,
    reason: str | None = None,
    expected_status: int = 200,
) -> dict:
    body: dict = {"access": access}
    if reason is not None:
        body["reason"] = reason
    r = client.put(f"{MCP_DIR}/entries/{entry_id}/access", headers=headers, json=body)
    assert r.status_code == expected_status, (
        f"set access: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def opencinna_server(sid: str, *, remotes: list | None = None, **extra) -> dict:
    """One server of the opencinna index."""
    return {
        "id": sid,
        "name": sid,
        "title": f"Title {sid}",
        "description": f"{sid} server",
        "websiteUrl": f"https://{sid}.example.com",
        "remotes": (
            remotes
            if remotes is not None
            else [{"type": "streamable-http", "url": f"https://{sid}.example.com/mcp"}]
        ),
        **extra,
    }


def import_mcp_index(
    client: TestClient,
    headers: dict[str, str],
    payload: dict,
    *,
    expected_status: int = 200,
) -> dict:
    """POST ``/mcp-directory/import`` with the index fetch stubbed to ``payload``."""
    from unittest.mock import patch

    with patch(
        "app.services.mcp.mcp_directory_service.McpDirectoryService.fetch_index",
        return_value=payload,
    ):
        r = client.post(f"{MCP_DIR}/import", headers=headers)
    assert r.status_code == expected_status, (
        f"mcp import: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()
