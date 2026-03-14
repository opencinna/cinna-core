"""Helper functions for dashboard API tests."""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


_BASE = f"{settings.API_V1_STR}/dashboards"


# ── Dashboard helpers ────────────────────────────────────────────────────────

def create_dashboard(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Create a dashboard via POST /dashboards/ and return response JSON."""
    payload = {"name": name or f"Dashboard {random_lower_string()[:8]}"}
    if description is not None:
        payload["description"] = description
    r = client.post(f"{_BASE}/", headers=token_headers, json=payload)
    assert r.status_code == 200, f"Create dashboard failed: {r.text}"
    return r.json()


def list_dashboards(client: TestClient, token_headers: dict[str, str]) -> list[dict]:
    """List dashboards via GET /dashboards/."""
    r = client.get(f"{_BASE}/", headers=token_headers)
    assert r.status_code == 200, f"List dashboards failed: {r.text}"
    return r.json()


def get_dashboard(
    client: TestClient, token_headers: dict[str, str], dashboard_id: str
) -> dict:
    """Get a single dashboard via GET /dashboards/{id}."""
    r = client.get(f"{_BASE}/{dashboard_id}", headers=token_headers)
    assert r.status_code == 200, f"Get dashboard failed: {r.text}"
    return r.json()


def update_dashboard(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    **fields,
) -> dict:
    """Update a dashboard via PUT /dashboards/{id}."""
    r = client.put(f"{_BASE}/{dashboard_id}", headers=token_headers, json=fields)
    assert r.status_code == 200, f"Update dashboard failed: {r.text}"
    return r.json()


def delete_dashboard(
    client: TestClient, token_headers: dict[str, str], dashboard_id: str
) -> None:
    """Delete a dashboard via DELETE /dashboards/{id}."""
    r = client.delete(f"{_BASE}/{dashboard_id}", headers=token_headers)
    assert r.status_code == 200, f"Delete dashboard failed: {r.text}"


# ── Block helpers ────────────────────────────────────────────────────────────

def add_block(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    agent_id: str,
    view_type: str = "latest_session",
    title: str | None = None,
    show_header: bool = False,
    grid_x: int = 0,
    grid_y: int = 0,
    grid_w: int = 2,
    grid_h: int = 2,
) -> dict:
    """Add a block to a dashboard via POST /dashboards/{id}/blocks."""
    payload = {
        "agent_id": agent_id,
        "view_type": view_type,
        "title": title,
        "show_border": True,
        "show_header": show_header,
        "grid_x": grid_x,
        "grid_y": grid_y,
        "grid_w": grid_w,
        "grid_h": grid_h,
    }
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks", headers=token_headers, json=payload
    )
    assert r.status_code == 200, f"Add block failed: {r.text}"
    return r.json()


def update_block(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
    **fields,
) -> dict:
    """Update a block via PUT /dashboards/{id}/blocks/{block_id}."""
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update block failed: {r.text}"
    return r.json()


def delete_block(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
) -> None:
    """Delete a block via DELETE /dashboards/{id}/blocks/{block_id}."""
    r = client.delete(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}", headers=token_headers
    )
    assert r.status_code == 200, f"Delete block failed: {r.text}"


def update_block_layout(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    layouts: list[dict],
) -> list[dict]:
    """Bulk update block layout via PUT /dashboards/{id}/blocks/layout."""
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/layout",
        headers=token_headers,
        json=layouts,
    )
    assert r.status_code == 200, f"Update block layout failed: {r.text}"
    return r.json()


# ── Prompt action helpers ─────────────────────────────────────────────────────

def create_prompt_action(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
    prompt_text: str | None = None,
    label: str | None = None,
    sort_order: int = 0,
) -> dict:
    """Create a prompt action via POST /dashboards/{id}/blocks/{block_id}/prompt-actions."""
    payload: dict = {
        "prompt_text": prompt_text or "Run a quick check and report back",
        "sort_order": sort_order,
    }
    if label is not None:
        payload["label"] = label
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Create prompt action failed: {r.text}"
    return r.json()


def list_prompt_actions(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
) -> list[dict]:
    """List prompt actions via GET /dashboards/{id}/blocks/{block_id}/prompt-actions."""
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List prompt actions failed: {r.text}"
    return r.json()


def update_prompt_action(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
    action_id: str,
    **fields,
) -> dict:
    """Update a prompt action via PUT /dashboards/{id}/blocks/{block_id}/prompt-actions/{action_id}."""
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update prompt action failed: {r.text}"
    return r.json()


def delete_prompt_action(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
    action_id: str,
) -> None:
    """Delete a prompt action via DELETE /dashboards/{id}/blocks/{block_id}/prompt-actions/{action_id}."""
    r = client.delete(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Delete prompt action failed: {r.text}"


# ── Agent env file block helper ──────────────────────────────────────────────

def create_agent_env_file_block(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    agent_id: str,
    file_path: str | None = None,
    grid_x: int = 0,
    grid_y: int = 0,
    grid_w: int = 2,
    grid_h: int = 2,
) -> dict:
    """Add an agent_env_file block to a dashboard via POST /dashboards/{id}/blocks."""
    config = {"file_path": file_path} if file_path else None
    payload = {
        "agent_id": agent_id,
        "view_type": "agent_env_file",
        "config": config,
        "show_border": True,
        "grid_x": grid_x,
        "grid_y": grid_y,
        "grid_w": grid_w,
        "grid_h": grid_h,
    }
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks", headers=token_headers, json=payload
    )
    assert r.status_code == 200, f"Create agent_env_file block failed: {r.text}"
    return r.json()


def get_block_env_file(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
    path: str,
) -> tuple[int, str | None]:
    """
    Call GET /dashboards/{id}/blocks/{block_id}/env-file?path=...
    Returns (status_code, response_text) — does NOT assert status code.
    """
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/env-file",
        headers=token_headers,
        params={"path": path},
    )
    body = r.text if r.status_code == 200 else None
    return r.status_code, body


# ── Block latest-session helper ───────────────────────────────────────────────

def get_block_latest_session(
    client: TestClient,
    token_headers: dict[str, str],
    dashboard_id: str,
    block_id: str,
) -> tuple[int, dict | None]:
    """
    Call GET /dashboards/{id}/blocks/{block_id}/latest-session.
    Returns (status_code, response_json) — does NOT assert status code.
    """
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/latest-session",
        headers=token_headers,
    )
    body = r.json() if r.status_code == 200 else None
    return r.status_code, body
