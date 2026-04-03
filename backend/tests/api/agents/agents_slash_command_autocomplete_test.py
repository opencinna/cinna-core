"""
Integration tests: GET /api/v1/sessions/{session_id}/commands endpoint.

Tests:
  1. Returns the full command list for a valid authenticated session
  2. Returns 404 for a non-existent session
  3. Returns 400 for a session belonging to another user
  4. /rebuild-env is_available=True when no session is streaming
  5. /rebuild-env is_available=False when a session on the same environment is streaming
  6. Unauthenticated request returns 401
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import create_session_via_api
from tests.utils.user import create_random_user_with_headers


def _list_session_commands(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
) -> tuple[int, dict]:
    """Call GET /api/v1/sessions/{session_id}/commands. Returns (status_code, json)."""
    r = client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/commands",
        headers=token_headers,
    )
    return r.status_code, r.json()


def test_list_session_commands_returns_all_registered_commands(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Happy path: authenticated owner gets the full command list.

    All registered commands should appear. Each entry has name, description,
    and is_available fields.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    status, data = _list_session_commands(client, superuser_token_headers, session_id)

    assert status == 200
    assert "commands" in data
    commands = data["commands"]

    # There should be at least the known commands registered at startup
    command_names = {cmd["name"] for cmd in commands}
    assert "/files" in command_names
    assert "/files-all" in command_names
    assert "/session-recover" in command_names
    assert "/session-reset" in command_names
    assert "/rebuild-env" in command_names

    # Each command should have all required fields
    for cmd in commands:
        assert "name" in cmd
        assert "description" in cmd
        assert "is_available" in cmd
        assert isinstance(cmd["name"], str)
        assert isinstance(cmd["description"], str)
        assert isinstance(cmd["is_available"], bool)
        assert cmd["name"].startswith("/")


def test_list_session_commands_returns_404_for_nonexistent_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Requesting commands for a non-existent session returns 404."""
    import uuid

    fake_session_id = str(uuid.uuid4())
    status, data = _list_session_commands(client, superuser_token_headers, fake_session_id)

    assert status == 404
    assert "not found" in data["detail"].lower()


def test_list_session_commands_returns_400_for_other_users_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A session belonging to user A cannot be accessed by user B (non-superuser).

    Returns 400 (Not enough permissions).
    """
    # Create agent and session as superuser
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    # Create a separate non-superuser
    _, other_user_headers = create_random_user_with_headers(client)

    status, data = _list_session_commands(client, other_user_headers, session_id)

    assert status == 400
    assert "permissions" in data["detail"].lower()


def test_list_session_commands_unauthenticated_returns_401(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Unauthenticated request (no token) returns 401."""
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    status, _ = _list_session_commands(client, {}, session_id)

    assert status == 401


def test_rebuild_env_is_available_when_not_streaming(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    /rebuild-env shows is_available=True when no session on the environment is streaming.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    # Mock active_streaming_manager to report no active streams
    with patch(
        "app.api.routes.messages.active_streaming_manager.is_any_session_streaming",
        new_callable=AsyncMock,
        return_value=False,
    ):
        status, data = _list_session_commands(client, superuser_token_headers, session_id)

    assert status == 200
    rebuild_cmd = next(
        (cmd for cmd in data["commands"] if cmd["name"] == "/rebuild-env"),
        None,
    )
    assert rebuild_cmd is not None
    assert rebuild_cmd["is_available"] is True


def test_rebuild_env_is_unavailable_when_streaming(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    /rebuild-env shows is_available=False when a session on the same environment
    is actively streaming. All other commands remain available.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    # Mock active_streaming_manager to report an active stream
    with patch(
        "app.api.routes.messages.active_streaming_manager.is_any_session_streaming",
        new_callable=AsyncMock,
        return_value=True,
    ):
        status, data = _list_session_commands(client, superuser_token_headers, session_id)

    assert status == 200
    commands_by_name = {cmd["name"]: cmd for cmd in data["commands"]}

    # /rebuild-env must be unavailable
    assert commands_by_name["/rebuild-env"]["is_available"] is False

    # All other commands must remain available
    for name, cmd in commands_by_name.items():
        if name != "/rebuild-env":
            assert cmd["is_available"] is True, (
                f"Expected {name} to be available when only streaming blocks /rebuild-env"
            )


def test_list_session_commands_non_owner_cannot_access_commands(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A non-superuser who does not own the session cannot access the commands list.
    This verifies the ownership check applies to any authenticated non-superuser.
    """
    # Create agent and session as superuser
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    session_data = create_session_via_api(client, superuser_token_headers, agent["id"])
    session_id = session_data["id"]

    # Create a second session as a different user — their token should not access
    # the superuser's session
    _, other_headers = create_random_user_with_headers(client)

    status, data = _list_session_commands(client, other_headers, session_id)

    assert status == 400
    assert "permissions" in data["detail"].lower()
