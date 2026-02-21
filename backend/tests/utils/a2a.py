"""Helpers for A2A integration tests."""
import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent, enable_a2a
from tests.utils.background_tasks import drain_tasks


def create_access_token(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    mode: str = "conversation",
    scope: str = "limited",
) -> dict:
    """Create an A2A access token via POST /agents/{id}/access-tokens/.

    Returns the full response including the one-time ``token`` field.
    """
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/access-tokens/",
        headers=token_headers,
        json={
            "agent_id": agent_id,
            "name": f"test-token-{uuid.uuid4().hex[:8]}",
            "mode": mode,
            "scope": scope,
        },
    )
    assert r.status_code == 200, f"Create access token failed: {r.text}"
    data = r.json()
    assert "token" in data, "Token value not returned on creation"
    return data


def build_streaming_request(
    message_text: str,
    task_id: str | None = None,
) -> dict:
    """Build a v1.0 SendStreamingMessage JSON-RPC request."""
    message: dict = {
        "role": "user",
        "parts": [{"text": message_text}],
        "messageId": uuid.uuid4().hex,
    }
    if task_id:
        message["taskId"] = task_id

    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "SendStreamingMessage",
        "params": {
            "message": message,
        },
    }


def parse_sse_events(response_text: str) -> list[dict]:
    """Parse SSE ``data:`` lines from a streaming response body."""
    events = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _a2a_headers(a2a_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {a2a_token}",
        "Content-Type": "application/json",
    }


def send_a2a_streaming_message(
    client: TestClient,
    agent_id: str,
    a2a_token: str,
    message_text: str,
    response_text: str,
    task_id: str | None = None,
) -> tuple[list[dict], StubAgentEnvConnector]:
    """Send a streaming message via A2A and return (parsed SSE events, stub).

    Handles stub creation, patching, HTTP call, drain, status assertion, and
    SSE parsing in one shot.
    """
    stub = StubAgentEnvConnector(response_text=response_text)
    request = build_streaming_request(message_text, task_id=task_id)

    with patch("app.services.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=_a2a_headers(a2a_token),
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"

    events = parse_sse_events(resp.text)
    return events, stub


def post_a2a_jsonrpc(
    client: TestClient,
    agent_id: str,
    a2a_token: str,
    request: dict,
) -> dict:
    """POST a JSON-RPC request to the A2A endpoint and return parsed JSON."""
    resp = client.post(
        f"{settings.API_V1_STR}/a2a/{agent_id}/",
        headers=_a2a_headers(a2a_token),
        json=request,
    )
    assert resp.status_code == 200, f"A2A JSON-RPC request failed: {resp.text}"
    return resp.json()


def get_a2a_agent_card(
    client: TestClient,
    agent_id: str,
    a2a_token: str,
) -> dict:
    """GET the A2A AgentCard for an agent."""
    resp = client.get(
        f"{settings.API_V1_STR}/a2a/{agent_id}/",
        headers={"Authorization": f"Bearer {a2a_token}"},
    )
    assert resp.status_code == 200, f"Get A2A agent card failed: {resp.text}"
    return resp.json()


def update_access_token(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    token_id: str,
    **fields,
) -> dict:
    """Update an access token via PUT /agents/{id}/access-tokens/{token_id}."""
    r = client.put(
        f"{settings.API_V1_STR}/agents/{agent_id}/access-tokens/{token_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update access token failed: {r.text}"
    return r.json()


def delete_access_token(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    token_id: str,
) -> dict:
    """Delete an access token via DELETE /agents/{id}/access-tokens/{token_id}."""
    r = client.delete(
        f"{settings.API_V1_STR}/agents/{agent_id}/access-tokens/{token_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Delete access token failed: {r.text}"
    return r.json()


def post_a2a_raw(
    client: TestClient,
    agent_id: str,
    request: dict,
    a2a_token: str | None = None,
):
    """POST a JSON-RPC request to the A2A endpoint, returning the raw response.

    Unlike :func:`post_a2a_jsonrpc` this does **not** assert HTTP 200, so it
    can be used to test authentication failures and other error paths.
    """
    headers = _a2a_headers(a2a_token) if a2a_token else {}
    return client.post(
        f"{settings.API_V1_STR}/a2a/{agent_id}/",
        headers=headers,
        json=request,
    )


def setup_a2a_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "A2A Agent",
) -> tuple[dict, dict]:
    """Create agent, enable A2A, create access token.

    Returns ``(agent_dict, token_data_dict)``.
    ``token_data_dict`` contains ``token`` (the JWT string) and ``id``
    (the token record UUID) among other fields.
    """
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()

    agent = get_agent(client, token_headers, agent["id"])
    assert agent["active_environment_id"] is not None

    enable_a2a(client, token_headers, agent["id"])

    token_data = create_access_token(client, token_headers, agent["id"])
    return agent, token_data
