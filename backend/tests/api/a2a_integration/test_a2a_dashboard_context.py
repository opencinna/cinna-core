"""
Integration tests for the dashboard context on the core A2A surface (Dashboards D6).

The same rules as on ``/external/a2a/`` (see
``tests/api/external/external_a2a_dashboard_chat_test.py``), on ``/a2a/{agent_id}/``
with the owner's JWT.

Test scenarios:
  1. A dashboard chat session resumed by task id gets the <dashboard_context>
     block and keeps dashboard_id; message/send refuses a bad context with -32602
  2. A task id naming another agent's session is refused on stream and send
"""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import build_streaming_request, parse_sse_events
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.dashboard import create_dashboard
from tests.utils.dashboard_components import open_chat

API = settings.API_V1_STR

_CTX = {"dashboard": {"id": "d", "name": "Ops"}, "focused": {"component": "sales"}, "other_tiles": []}


def _agent(client: TestClient, h: dict, name: str) -> dict:
    agent = create_agent_via_api(client, h, name=name)
    drain_tasks()
    return get_agent(client, h, agent["id"])


def _post(client, h, agent_id, task_id, context, method="SendStreamingMessage"):
    request = build_streaming_request("Hi", task_id=task_id)
    request["method"] = method
    request["params"]["message"]["metadata"] = {"cinna_dashboard_context": context}
    stub = StubAgentEnvConnector(response_text="ok")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        r = client.post(f"{API}/a2a/{agent_id}/", headers=h, json=request)
        drain_tasks()
    assert r.status_code == 200, r.text
    return r, stub


def test_core_a2a_resumes_the_dashboard_chat_with_its_context(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h, "Core A2A Agent")
    chat = open_chat(client, h, dash["id"], agent["id"])

    r, stub = _post(client, h, agent["id"], chat["id"], _CTX)
    assert not [e for e in parse_sse_events(r.text) if "error" in e]
    payload = stub.stream_calls[0]["payload"]
    assert json.dumps(_CTX, separators=(",", ":")) in payload["message"]
    assert payload["session_state"]["session_context"]["dashboard_id"] == dash["id"]

    r, stub = _post(client, h, agent["id"], chat["id"], {"pad": "p" * 12_001}, method="SendMessage")
    assert r.json()["error"]["code"] == -32602
    assert stub.stream_calls == []


def test_core_a2a_task_id_of_another_agents_session_is_refused(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h, "Agent A")
    other = _agent(client, h, "Agent B")
    chat_b = open_chat(client, h, dash["id"], other["id"])

    r, stub = _post(client, h, agent["id"], chat_b["id"], _CTX)
    errors = [e["error"] for e in parse_sse_events(r.text) if "error" in e]
    assert len(errors) == 1 and errors[0]["code"] == -32001
    r, stub = _post(client, h, agent["id"], chat_b["id"], _CTX, method="SendMessage")
    assert r.json()["error"]["code"] == -32001
    assert stub.stream_calls == []

    r = client.get(f"{API}/sessions/{chat_b['id']}/messages", headers=h)
    assert [m for m in r.json()["data"] if m["role"] == "user"] == []
