"""Helpers for InputTask API tests — tasks, comments, attachments, subtasks."""
import io
from uuid import UUID

from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/tasks"


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

def create_task(
    client: TestClient,
    headers: dict,
    original_message: str = "Test task description",
    title: str | None = None,
    priority: str = "normal",
    selected_agent_id: str | None = None,
    team_id: str | None = None,
    assigned_node_id: str | None = None,
    parent_task_id: str | None = None,
) -> dict:
    """POST /tasks/ — asserts 200 and returns body."""
    payload: dict = {"original_message": original_message, "priority": priority}
    if title is not None:
        payload["title"] = title
    if selected_agent_id is not None:
        payload["selected_agent_id"] = selected_agent_id
    if team_id is not None:
        payload["team_id"] = team_id
    if assigned_node_id is not None:
        payload["assigned_node_id"] = assigned_node_id
    if parent_task_id is not None:
        payload["parent_task_id"] = parent_task_id
    r = client.post(_BASE + "/", headers=headers, json=payload)
    assert r.status_code == 200, f"Task creation failed: {r.text}"
    return r.json()


def get_task(client: TestClient, headers: dict, task_id: str) -> dict:
    """GET /tasks/{task_id} — asserts 200 and returns body."""
    r = client.get(f"{_BASE}/{task_id}", headers=headers)
    assert r.status_code == 200, f"Get task failed: {r.text}"
    return r.json()


def get_task_detail(client: TestClient, headers: dict, task_id: str) -> dict:
    """GET /tasks/{task_id}/detail — asserts 200 and returns body."""
    r = client.get(f"{_BASE}/{task_id}/detail", headers=headers)
    assert r.status_code == 200, f"Get task detail failed: {r.text}"
    return r.json()


def get_task_by_code(client: TestClient, headers: dict, short_code: str) -> dict:
    """GET /tasks/by-code/{short_code} — asserts 200 and returns body."""
    r = client.get(f"{_BASE}/by-code/{short_code}", headers=headers)
    assert r.status_code == 200, f"Get task by code failed: {r.text}"
    return r.json()


def get_task_detail_by_code(client: TestClient, headers: dict, short_code: str) -> dict:
    """GET /tasks/by-code/{short_code}/detail — asserts 200 and returns body."""
    r = client.get(f"{_BASE}/by-code/{short_code}/detail", headers=headers)
    assert r.status_code == 200, f"Get task detail by code failed: {r.text}"
    return r.json()


def get_task_tree_by_code(client: TestClient, headers: dict, short_code: str) -> dict:
    """GET /tasks/by-code/{short_code}/tree — asserts 200 and returns body."""
    r = client.get(f"{_BASE}/by-code/{short_code}/tree", headers=headers)
    assert r.status_code == 200, f"Get task tree by code failed: {r.text}"
    return r.json()


def list_tasks(
    client: TestClient,
    headers: dict,
    status: str | None = None,
    root_only: bool = False,
    team_id: str | None = None,
    priority: str | None = None,
) -> dict:
    """GET /tasks/ — asserts 200 and returns full body (data + count)."""
    params: dict = {}
    if status is not None:
        params["status"] = status
    if root_only:
        params["root_only"] = "true"
    if team_id is not None:
        params["team_id"] = team_id
    if priority is not None:
        params["priority"] = priority
    r = client.get(_BASE + "/", headers=headers, params=params)
    assert r.status_code == 200, f"List tasks failed: {r.text}"
    return r.json()


def update_task(
    client: TestClient,
    headers: dict,
    task_id: str,
    **fields,
) -> dict:
    """PATCH /tasks/{task_id} — asserts 200 and returns body."""
    r = client.patch(f"{_BASE}/{task_id}", headers=headers, json=fields)
    assert r.status_code == 200, f"Update task failed: {r.text}"
    return r.json()


def delete_task(client: TestClient, headers: dict, task_id: str) -> dict:
    """DELETE /tasks/{task_id} — asserts 200 and returns body."""
    r = client.delete(f"{_BASE}/{task_id}", headers=headers)
    assert r.status_code == 200, f"Delete task failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Comment helpers
# ---------------------------------------------------------------------------

def add_comment(
    client: TestClient,
    headers: dict,
    task_id: str,
    content: str = "Test comment",
    comment_type: str = "message",
) -> dict:
    """POST /tasks/{task_id}/comments/ — asserts 200 and returns body."""
    payload = {"content": content, "comment_type": comment_type}
    r = client.post(f"{_BASE}/{task_id}/comments/", headers=headers, json=payload)
    assert r.status_code == 200, f"Add comment failed: {r.text}"
    return r.json()


def list_comments(client: TestClient, headers: dict, task_id: str) -> dict:
    """GET /tasks/{task_id}/comments/ — asserts 200 and returns full body."""
    r = client.get(f"{_BASE}/{task_id}/comments/", headers=headers)
    assert r.status_code == 200, f"List comments failed: {r.text}"
    return r.json()


def delete_comment(client: TestClient, headers: dict, task_id: str, comment_id: str) -> dict:
    """DELETE /tasks/{task_id}/comments/{comment_id} — asserts 200 and returns body."""
    r = client.delete(f"{_BASE}/{task_id}/comments/{comment_id}", headers=headers)
    assert r.status_code == 200, f"Delete comment failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

def upload_attachment(
    client: TestClient,
    headers: dict,
    task_id: str,
    content: bytes = b"Hello attachment content",
    filename: str = "test_file.txt",
    content_type: str = "text/plain",
) -> dict:
    """POST /tasks/{task_id}/attachments/ — asserts 200 and returns body."""
    r = client.post(
        f"{_BASE}/{task_id}/attachments/",
        headers=headers,
        files={"file": (filename, io.BytesIO(content), content_type)},
    )
    assert r.status_code == 200, f"Upload attachment failed: {r.text}"
    return r.json()


def list_attachments(client: TestClient, headers: dict, task_id: str) -> dict:
    """GET /tasks/{task_id}/attachments/ — asserts 200 and returns full body."""
    r = client.get(f"{_BASE}/{task_id}/attachments/", headers=headers)
    assert r.status_code == 200, f"List attachments failed: {r.text}"
    return r.json()


def delete_attachment(
    client: TestClient, headers: dict, task_id: str, attachment_id: str
) -> dict:
    """DELETE /tasks/{task_id}/attachments/{attachment_id} — asserts 200 and returns body."""
    r = client.delete(f"{_BASE}/{task_id}/attachments/{attachment_id}", headers=headers)
    assert r.status_code == 200, f"Delete attachment failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Subtask helpers
# ---------------------------------------------------------------------------

def create_subtask(
    client: TestClient,
    headers: dict,
    parent_task_id: str,
    original_message: str = "Subtask description",
    title: str | None = None,
    priority: str = "normal",
) -> dict:
    """POST /tasks/{parent_task_id}/subtasks/ — asserts 200 and returns body."""
    payload: dict = {"original_message": original_message, "priority": priority}
    if title is not None:
        payload["title"] = title
    r = client.post(f"{_BASE}/{parent_task_id}/subtasks/", headers=headers, json=payload)
    assert r.status_code == 200, f"Create subtask failed: {r.text}"
    return r.json()


def list_subtasks(client: TestClient, headers: dict, task_id: str) -> dict:
    """GET /tasks/{task_id}/subtasks/ — asserts 200 and returns full body."""
    r = client.get(f"{_BASE}/{task_id}/subtasks/", headers=headers)
    assert r.status_code == 200, f"List subtasks failed: {r.text}"
    return r.json()


def execute_task(
    client: TestClient,
    headers: dict,
    task_id: str,
    mode: str = "conversation",
) -> dict:
    """POST /tasks/{task_id}/execute — asserts 200 and returns body."""
    r = client.post(
        f"{_BASE}/{task_id}/execute",
        headers=headers,
        json={"mode": mode},
    )
    assert r.status_code == 200, f"Execute task failed: {r.text}"
    return r.json()


def get_task_sessions(client: TestClient, headers: dict, task_id: str) -> list[dict]:
    """GET /tasks/{task_id}/sessions — asserts 200 and returns data list."""
    r = client.get(f"{_BASE}/{task_id}/sessions", headers=headers)
    assert r.status_code == 200, f"Get task sessions failed: {r.text}"
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Agent-side task API helpers (used by MCP tools in agent environments)
# ---------------------------------------------------------------------------

_AGENT_BASE = f"{settings.API_V1_STR}/agent/tasks"


def agent_create_subtask(
    client: TestClient,
    headers: dict,
    task_id: str,
    title: str,
    description: str | None = None,
    assigned_to: str | None = None,
    priority: str = "normal",
    source_session_id: str | None = None,
) -> dict:
    """POST /agent/tasks/{task_id}/subtask — asserts 200 and returns body."""
    payload: dict = {"title": title, "priority": priority}
    if description is not None:
        payload["description"] = description
    if assigned_to is not None:
        payload["assigned_to"] = assigned_to
    if source_session_id is not None:
        payload["source_session_id"] = source_session_id
    r = client.post(f"{_AGENT_BASE}/{task_id}/subtask", headers=headers, json=payload)
    assert r.status_code == 200, f"Agent create subtask failed: {r.text}"
    return r.json()


def agent_create_subtask_current(
    client: TestClient,
    headers: dict,
    title: str,
    source_session_id: str,
    description: str | None = None,
    assigned_to: str | None = None,
    priority: str = "normal",
) -> dict:
    """POST /agent/tasks/current/subtask — asserts 200 and returns body."""
    payload: dict = {
        "title": title,
        "priority": priority,
        "source_session_id": source_session_id,
    }
    if description is not None:
        payload["description"] = description
    if assigned_to is not None:
        payload["assigned_to"] = assigned_to
    r = client.post(f"{_AGENT_BASE}/current/subtask", headers=headers, json=payload)
    assert r.status_code == 200, f"Agent create subtask (current) failed: {r.text}"
    return r.json()


def agent_add_comment(
    client: TestClient,
    headers: dict,
    task_id: str,
    content: str,
    comment_type: str = "message",
    file_paths: list[str] | None = None,
) -> dict:
    """POST /agent/tasks/{task_id}/comment — asserts 200 and returns body."""
    payload: dict = {"content": content, "comment_type": comment_type}
    if file_paths is not None:
        payload["file_paths"] = file_paths
    r = client.post(f"{_AGENT_BASE}/{task_id}/comment", headers=headers, json=payload)
    assert r.status_code == 200, f"Agent add comment failed: {r.text}"
    return r.json()


def agent_add_comment_current(
    client: TestClient,
    headers: dict,
    content: str,
    source_session_id: str,
    comment_type: str = "message",
    file_paths: list[str] | None = None,
) -> dict:
    """POST /agent/tasks/current/comment — asserts 200 and returns body."""
    payload: dict = {
        "content": content,
        "comment_type": comment_type,
        "source_session_id": source_session_id,
    }
    if file_paths is not None:
        payload["file_paths"] = file_paths
    r = client.post(f"{_AGENT_BASE}/current/comment", headers=headers, json=payload)
    assert r.status_code == 200, f"Agent add comment (current) failed: {r.text}"
    return r.json()


def agent_update_status(
    client: TestClient,
    headers: dict,
    task_id: str,
    status: str,
    reason: str | None = None,
) -> dict:
    """POST /agent/tasks/{task_id}/status — asserts 200 and returns body."""
    payload: dict = {"status": status}
    if reason is not None:
        payload["reason"] = reason
    r = client.post(f"{_AGENT_BASE}/{task_id}/status", headers=headers, json=payload)
    assert r.status_code == 200, f"Agent update status failed: {r.text}"
    return r.json()


def agent_get_task_details(
    client: TestClient,
    headers: dict,
    task_id: str,
    source_session_id: str | None = None,
) -> dict:
    """GET /agent/tasks/{task_id}/details — asserts 200 and returns body."""
    params: dict = {}
    if source_session_id is not None:
        params["source_session_id"] = source_session_id
    r = client.get(f"{_AGENT_BASE}/{task_id}/details", headers=headers, params=params)
    assert r.status_code == 200, f"Agent get task details failed: {r.text}"
    return r.json()


def agent_get_task_details_current(
    client: TestClient,
    headers: dict,
    source_session_id: str,
) -> dict:
    """GET /agent/tasks/current/details — asserts 200 and returns body."""
    r = client.get(
        f"{_AGENT_BASE}/current/details",
        headers=headers,
        params={"source_session_id": source_session_id},
    )
    assert r.status_code == 200, f"Agent get task details (current) failed: {r.text}"
    return r.json()


def agent_resolve_by_code(
    client: TestClient,
    headers: dict,
    short_code: str,
) -> dict:
    """GET /agent/tasks/by-code/{short_code} — asserts 200 and returns body."""
    r = client.get(f"{_AGENT_BASE}/by-code/{short_code}", headers=headers)
    assert r.status_code == 200, f"Agent resolve by code failed: {r.text}"
    return r.json()
