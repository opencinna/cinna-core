"""
MCP file upload integration tests.

Verifies the get_file_upload_url MCP tool and the POST /mcp/{connector_id}/upload endpoint:
  - Tool returns a valid CURL command with a temporary JWT
  - Upload endpoint accepts files with valid tokens
  - Upload endpoint rejects expired tokens, wrong connectors, oversized files
  - Upload endpoint returns 503 when agent environment is not running
  - Upload endpoint returns 404 for inactive connectors

These tests call handle_get_file_upload_url() directly for the tool, and use
the FastAPI TestClient for the upload endpoint.
"""
import asyncio
import io
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.mcp.upload_token import create_file_upload_token, verify_file_upload_token
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import create_mcp_connector, update_mcp_connector


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent_with_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_name: str = "MCP Upload Agent",
    connector_name: str = "Upload Connector",
    mode: str = "conversation",
) -> tuple[dict, dict]:
    """Create agent + connector. Returns (agent, connector)."""
    agent = create_agent_via_api(client, token_headers, name=agent_name)
    drain_tasks()
    agent = get_agent(client, token_headers, agent["id"])
    connector = create_mcp_connector(
        client, token_headers, agent["id"],
        name=connector_name, mode=mode,
    )
    return agent, connector


def _run_get_file_upload_url(
    connector_id: str,
    filename: str,
    workspace_path: str = "uploads",
) -> str:
    """Call handle_get_file_upload_url with the connector context var set."""
    from app.mcp.tools import handle_get_file_upload_url
    from app.mcp.server import mcp_connector_id_var

    async def _run():
        token = mcp_connector_id_var.set(connector_id)
        try:
            return await handle_get_file_upload_url(filename, workspace_path)
        finally:
            mcp_connector_id_var.reset(token)

    return asyncio.run(_run())


# ── Tool Tests ───────────────────────────────────────────────────────────────


def test_get_file_upload_url_returns_curl_command(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    get_file_upload_url tool returns a CURL command with a valid JWT and URL:
      1. Create agent + connector
      2. Call get_file_upload_url tool
      3. Verify response contains CURL command, upload URL, and Bearer token
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Upload URL Agent",
    )
    connector_id = connector["id"]

    result = _run_get_file_upload_url(connector_id, "test_file.txt")

    # Should contain CURL command structure
    assert "curl" in result.lower()
    assert connector_id in result
    assert "Bearer" in result
    assert "test_file.txt" in result
    assert "workspace_path=uploads" in result
    assert "/upload" in result

    # Extract and verify the JWT from the CURL command
    bearer_idx = result.index("Bearer ") + len("Bearer ")
    # Token ends at the quote
    token_end = result.index('"', bearer_idx)
    token = result[bearer_idx:token_end]

    verified_connector_id = verify_file_upload_token(token)
    assert verified_connector_id == connector_id


def test_get_file_upload_url_inactive_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """get_file_upload_url on an inactive connector returns error."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Inactive Upload Agent",
    )
    connector_id = connector["id"]
    agent_id = agent["id"]

    # Deactivate
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_active=False,
    )

    result = _run_get_file_upload_url(connector_id, "test.txt")
    assert "error" in result.lower()


# ── Upload Endpoint Tests ────────────────────────────────────────────────────


def test_file_upload_endpoint_success(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    POST /mcp/{connector_id}/upload with valid token uploads file successfully:
      1. Create agent + connector
      2. Generate upload token
      3. POST file to upload endpoint
      4. Verify response contains status, workspace_path, filename, size
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Upload Success Agent",
    )
    connector_id = connector["id"]

    # Set environment status to "running" so the endpoint accepts uploads
    from app.models import AgentEnvironment
    env = db.get(AgentEnvironment, agent["active_environment_id"])
    env.status = "running"
    db.add(env)
    db.flush()

    token = create_file_upload_token(connector_id)

    file_content = b"Hello, this is test file content!"
    response = client.post(
        f"/mcp/{connector_id}/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test_upload.txt", io.BytesIO(file_content), "text/plain")},
        data={"workspace_path": "uploads"},
    )

    assert response.status_code == 200, f"Upload failed: {response.text}"
    data = response.json()
    assert data["status"] == "uploaded"
    assert data["filename"] == "test_upload.txt"
    assert data["size"] == len(file_content)
    assert "workspace_path" in data


def test_file_upload_expired_token_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Upload with an expired JWT returns 401."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Expired Token Agent",
    )
    connector_id = connector["id"]

    # Create a token that expires immediately
    token = create_file_upload_token(connector_id, expires_minutes=0)
    # Wait a moment to ensure it's expired
    time.sleep(1)

    response = client.post(
        f"/mcp/{connector_id}/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert response.status_code == 401


def test_file_upload_wrong_connector_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Token for connector A used on connector B's URL returns 403."""
    agent, connector_a = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Wrong Connector Agent A",
        connector_name="Connector A",
    )
    connector_b = create_mcp_connector(
        client, superuser_token_headers, agent["id"],
        name="Connector B",
    )

    # Token is for connector A
    token = create_file_upload_token(connector_a["id"])

    # Use it on connector B's URL
    response = client.post(
        f"/mcp/{connector_b['id']}/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert response.status_code == 403


def test_file_upload_inactive_connector_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Upload to a deactivated connector returns 404."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Inactive Upload Endpoint Agent",
    )
    connector_id = connector["id"]
    agent_id = agent["id"]

    # Generate token while active
    token = create_file_upload_token(connector_id)

    # Deactivate
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_active=False,
    )

    response = client.post(
        f"/mcp/{connector_id}/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert response.status_code == 404


def test_file_upload_too_large_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Oversized file returns 413."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Large File Agent",
    )
    connector_id = connector["id"]

    token = create_file_upload_token(connector_id)

    # Temporarily set a very small max file size
    original_mb = settings.UPLOAD_MAX_FILE_SIZE_MB
    settings.UPLOAD_MAX_FILE_SIZE_MB = 0
    try:
        response = client.post(
            f"/mcp/{connector_id}/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("big.txt", io.BytesIO(b"x" * 100), "text/plain")},
        )
    finally:
        settings.UPLOAD_MAX_FILE_SIZE_MB = original_mb

    assert response.status_code == 413


def test_file_upload_environment_not_running(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Upload when environment is not running returns 503."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Stopped Env Agent",
    )
    connector_id = connector["id"]

    # Ensure environment is NOT running (default stub status is "stopped")
    from app.models import AgentEnvironment
    env = db.get(AgentEnvironment, agent["active_environment_id"])
    env.status = "stopped"
    db.add(env)
    db.flush()

    token = create_file_upload_token(connector_id)

    response = client.post(
        f"/mcp/{connector_id}/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert response.status_code == 503


def test_file_upload_missing_auth_header(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Upload without Authorization header returns 401."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No Auth Agent",
    )
    connector_id = connector["id"]

    response = client.post(
        f"/mcp/{connector_id}/upload",
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert response.status_code == 401
