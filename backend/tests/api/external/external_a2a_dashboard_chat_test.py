"""
Integration tests for the dashboard chat over External A2A (Dashboards D6).

Cinna Desktop continues a dashboard chat session (``POST /dashboards/{id}/chat``)
over ``/external/a2a/agent/{id}/`` with the user's JWT, the session id as the
task id, and the dashboard context in ``message.metadata.cinna_dashboard_context``.
Core applies the messages route's rules to it.

Test scenarios:
  1. Resume keeps the session: same task id, dashboard_id in the signed
     session context, still the dashboard's chat; the context is stored in
     message_metadata (compact, never in the content) and sent as the
     <dashboard_context> block, omitted while unchanged, full on change
  2. A JSON string is taken like an object; bad JSON, a non-object and more
     than 12,000 characters are -32602 and store nothing (stream and send)
  3. Dropped silently on a session without a dashboard and on a new session
  4. A task id naming another agent's session is refused and writes nothing
"""
import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import build_streaming_request, extract_task_id, parse_sse_events
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.dashboard import create_dashboard
from tests.utils.dashboard_components import open_chat

API = settings.API_V1_STR


def _agent(client: TestClient, h: dict, name: str = "Desk Agent") -> dict:
    agent = create_agent_via_api(client, h, name=name)
    drain_tasks()
    return get_agent(client, h, agent["id"])


def _ctx(**focused) -> dict:
    return {
        "dashboard": {"id": "d", "name": "Sales"},
        "focused": {"component": "sales", "title": "Sales", **focused},
        "also_yours": [],
        "other_tiles": [{"title": "Local notes"}],
    }


def _request(text: str, task_id: str | None, context=None, method: str = "SendStreamingMessage") -> dict:
    request = build_streaming_request(text, task_id=task_id)
    request["method"] = method
    if context is not None:
        request["params"]["message"]["metadata"] = {"cinna_dashboard_context": context}
    return request


def _stream(client, h, agent_id, text, task_id=None, context=None):
    """One message/stream turn; returns (events, stub)."""
    stub = StubAgentEnvConnector(response_text="ok")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        r = client.post(
            f"{API}/external/a2a/agent/{agent_id}/", headers=h, json=_request(text, task_id, context)
        )
        drain_tasks()
    assert r.status_code == 200, r.text
    return parse_sse_events(r.text), stub


def _errors(events: list[dict]) -> list[dict]:
    return [e["error"] for e in events if "error" in e]


def _sent(stub: StubAgentEnvConnector) -> str:
    return stub.stream_calls[0]["payload"]["message"]


def _user_messages(client, h, session_id) -> list[dict]:
    r = client.get(f"{API}/sessions/{session_id}/messages", headers=h)
    assert r.status_code == 200, r.text
    return [m for m in r.json()["data"] if m["role"] == "user"]


# ── Scenario 1: resume and context block ────────────────────────────────────

def test_a2a_turn_resumes_the_dashboard_chat_with_its_context(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h, name="Desk", origin="desktop")
    agent = _agent(client, h)
    chat = open_chat(client, h, dash["id"], agent["id"])
    assert chat["integration_type"] is None

    ctx1 = _ctx(summary="Revenue is up")
    events, stub = _stream(client, h, agent["id"], "What changed?", chat["id"], ctx1)
    assert _errors(events) == []
    assert extract_task_id(events) == chat["id"]

    sent = _sent(stub)
    stored_text = json.dumps(ctx1, ensure_ascii=False, separators=(",", ":"))
    assert "<dashboard_context>" in sent and stored_text in sent and "What changed?" in sent
    session_ctx = stub.stream_calls[0]["payload"]["session_state"]["session_context"]
    assert session_ctx["dashboard_id"] == dash["id"]

    # Still the dashboard's chat, with its dashboard
    r = client.get(f"{API}/dashboards/{dash['id']}/chat", headers=h, params={"agent_id": agent["id"]})
    assert r.status_code == 200 and r.json()["session"]["id"] == chat["id"]
    assert r.json()["session"]["dashboard_id"] == dash["id"]

    # Stored unrendered and compact
    msgs = _user_messages(client, h, chat["id"])
    assert [m["content"] for m in msgs] == ["What changed?"]
    assert msgs[0]["message_metadata"]["dashboard_context"] == stored_text

    # Unchanged: omitted; changed: full again
    _, stub = _stream(client, h, agent["id"], "And now?", chat["id"], ctx1)
    assert "<dashboard_context>" not in _sent(stub) and "And now?" in _sent(stub)
    ctx2 = _ctx(summary="Revenue is down")
    _, stub = _stream(client, h, agent["id"], "Look", chat["id"], ctx2)
    assert json.dumps(ctx2, separators=(",", ":")) in _sent(stub)


# ── Scenario 2: refused contexts ────────────────────────────────────────────

def test_a2a_context_string_is_taken_and_bad_ones_are_invalid_params(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h, origin="desktop")
    agent = _agent(client, h)
    sid = open_chat(client, h, dash["id"], agent["id"])["id"]

    # A JSON string is accepted like an object
    pretty = json.dumps(_ctx(summary="s"), indent=2)
    events, _ = _stream(client, h, agent["id"], "fits", sid, pretty)
    assert _errors(events) == []

    for bad in ("{not json", "[1, 2]", [1, 2], "\"text\"", {"pad": "p" * 12_001}):
        events, stub = _stream(client, h, agent["id"], "x", sid, bad)
        errors = _errors(events)
        assert len(errors) == 1 and errors[0]["code"] == -32602, (bad if len(str(bad)) < 50 else "pad", events)
        assert stub.stream_calls == []

    # message/send maps the same refusal
    r = client.post(
        f"{API}/external/a2a/agent/{agent['id']}/",
        headers=h,
        json=_request("x", sid, "{not json", method="SendMessage"),
    )
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602

    assert [m["content"] for m in _user_messages(client, h, sid)] == ["fits"]


# ── Scenario 3: dropped without a dashboard ─────────────────────────────────

def test_a2a_context_is_dropped_without_a_dashboard(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    agent = _agent(client, h)
    plain = client.post(f"{API}/sessions/", headers=h, json={"agent_id": agent["id"], "mode": "conversation"}).json()

    # Even a bad one is dropped, as on the messages route
    for context in (_ctx(summary="s"), "{not json"):
        events, stub = _stream(client, h, agent["id"], "Hi", plain["id"], context)
        assert _errors(events) == []
        assert "<dashboard_context>" not in _sent(stub)
    assert all("dashboard_context" not in (m["message_metadata"] or {}) for m in _user_messages(client, h, plain["id"]))

    # A new session (no task id) never has a dashboard
    events, stub = _stream(client, h, agent["id"], "New", None, _ctx(summary="s"))
    assert _errors(events) == []
    assert "<dashboard_context>" not in _sent(stub)


# ── Scenario 4: a task id of another agent ──────────────────────────────────

def test_a2a_task_id_of_another_agents_session_is_refused(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h, origin="desktop")
    agent = _agent(client, h, "Agent A")
    other = _agent(client, h, "Agent B")
    chat_b = open_chat(client, h, dash["id"], other["id"])

    events, stub = _stream(client, h, agent["id"], "Hi", chat_b["id"], _ctx())
    errors = _errors(events)
    assert len(errors) == 1 and errors[0]["code"] == -32001
    assert stub.stream_calls == []
    assert _user_messages(client, h, chat_b["id"]) == []

    # Unknown task ids still start a new session on this surface
    events, _ = _stream(client, h, agent["id"], "Hi", str(uuid.uuid4()))
    assert _errors(events) == []
