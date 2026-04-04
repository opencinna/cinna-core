"""Helpers to create/manage agent collaborations via API for tests."""
import uuid
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import create_session_via_api


_COLLAB_BASE = f"{settings.API_V1_STR}/agents/collaborations"


def _mock_create_agent_task(task_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None):
    """Return a context manager that stubs AgentService.create_agent_task.

    By default (task_id=None, session_id=None) returns success=True with no IDs.
    The collaboration service sets subtask status="error" when both IDs are None
    (it requires success AND task_id AND session_id to mark a subtask "running").
    This avoids FK violations from fake UUIDs that don't exist in the DB.

    Pass explicit real UUIDs (from real DB records) when the test needs the
    subtask's input_task_id and session_id fields to be populated — but note that
    both must point to real rows in their respective tables to avoid FK errors.

    The patch target is the method on AgentService's own module, because the
    collaboration service imports AgentService locally inside the function body.
    """
    return patch(
        "app.services.agents.agent_service.AgentService.create_agent_task",
        new=AsyncMock(return_value=(True, task_id, session_id, None)),
    )


def _mock_create_agent_task_failure(error: str = "Dispatch failed"):
    """Return a context manager that stubs AgentService.create_agent_task to fail.

    Returns success=False so the collaboration service records the subtask as
    status="error" with the given error message in result_summary.
    """
    return patch(
        "app.services.agents.agent_service.AgentService.create_agent_task",
        new=AsyncMock(return_value=(False, None, None, error)),
    )


def setup_coordinator_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "Coordinator",
) -> tuple[dict, dict]:
    """Create a coordinator agent and a session for it.

    Returns ``(agent, session)`` dicts. The session is needed because the
    create_collaboration endpoint derives coordinator_agent_id from the
    source_session's environment.
    """
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    session = create_session_via_api(client, token_headers, agent["id"])
    return agent, session


def create_collaboration(
    client: TestClient,
    token_headers: dict[str, str],
    *,
    title: str,
    source_session_id: str,
    subtasks: list[dict],
    description: str | None = None,
) -> dict:
    """Call POST /agents/collaborations/create and assert success.

    Subtasks are mocked via the patched AgentService.create_agent_task
    that must already be active in the calling context.

    Returns the parsed JSON response body.
    """
    payload: dict = {
        "title": title,
        "source_session_id": source_session_id,
        "subtasks": subtasks,
    }
    if description is not None:
        payload["description"] = description

    r = client.post(f"{_COLLAB_BASE}/create", headers=token_headers, json=payload)
    assert r.status_code == 200, f"Create collaboration failed: {r.text}"
    body = r.json()
    assert body["success"] is True, f"Create collaboration returned success=False: {body}"
    return body


def get_collaboration_status(
    client: TestClient,
    token_headers: dict[str, str],
    collaboration_id: str,
) -> dict:
    """Call GET /agents/collaborations/{id}/status and assert 200."""
    r = client.get(f"{_COLLAB_BASE}/{collaboration_id}/status", headers=token_headers)
    assert r.status_code == 200, f"Get status failed: {r.text}"
    return r.json()


def post_finding(
    client: TestClient,
    token_headers: dict[str, str],
    collaboration_id: str,
    finding: str,
    source_session_id: str | None = None,
) -> dict:
    """Call POST /agents/collaborations/{id}/findings and assert success=True."""
    payload: dict = {"finding": finding}
    if source_session_id is not None:
        payload["source_session_id"] = source_session_id
    r = client.post(
        f"{_COLLAB_BASE}/{collaboration_id}/findings",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Post finding failed: {r.text}"
    body = r.json()
    assert body["success"] is True, f"Post finding returned success=False: {body}"
    return body


def get_collaboration_by_session(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
) -> dict:
    """Call GET /agents/collaborations/by-session/{session_id} and assert 200."""
    r = client.get(f"{_COLLAB_BASE}/by-session/{session_id}", headers=token_headers)
    assert r.status_code == 200, f"Get by-session failed: {r.text}"
    return r.json()
