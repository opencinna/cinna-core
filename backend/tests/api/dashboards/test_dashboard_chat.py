"""
Integration tests for the dashboard chat (D-C5) and the <dashboard_context> block.

Test scenarios:
  1. One chat per (dashboard, agent): get / create / reuse, New chat, other pairs
  2. Ownership: another user's dashboard or agent is a 404
  3. Context block: full on the first message, omitted when unchanged, full on change
  4. Context storage: clean message content, selected_text truncated, compact JSON
  5. Context refused: bad JSON, not an object, over 12,000 characters (400)
  6. dashboard_id reaches the environment's session context; prompt-action sessions
     get the dashboard too but are not the chat
"""
import json
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.dashboard import add_block, create_dashboard
from tests.utils.dashboard_components import (  # noqa: F401 — patch_environment_adapter is an autouse override
    open_chat,
    patch_environment_adapter,
    send_chat_message,
)
from tests.utils.session import create_session_with_block, get_session
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def _agent(client: TestClient, headers: dict, name: str = "Chat Agent") -> dict:
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    return agent


def _ctx(**focused) -> str:
    payload = {
        "dashboard": {"id": "d", "name": "Sales"},
        "focused": {"component": "sales", "title": "Sales", **focused},
        "also_yours": [],
        "other_tiles": [],
    }
    return json.dumps(payload, separators=(",", ":"))


def _chat_get(client, headers, dashboard_id, agent_id):
    return client.get(f"{API}/dashboards/{dashboard_id}/chat", headers=headers, params={"agent_id": agent_id})


def _user_messages(client, headers, session_id) -> list[dict]:
    r = client.get(f"{API}/sessions/{session_id}/messages", headers=headers)
    assert r.status_code == 200, r.text
    return [m for m in r.json()["data"] if m["role"] == "user"]


# ── Scenario 1: one chat per (dashboard, agent) ─────────────────────────────

def test_one_chat_per_dashboard_and_agent_with_new_chat(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    other_dash = create_dashboard(client, h)
    agent = _agent(client, h)
    other_agent = _agent(client, h, "Other Agent")

    # Nothing yet: GET never creates
    r = _chat_get(client, h, dash["id"], agent["id"])
    assert r.status_code == 200 and r.json() == {"session": None}

    first = open_chat(client, h, dash["id"], agent["id"])
    assert first["dashboard_id"] == dash["id"]
    assert first["agent_id"] == agent["id"]
    assert first["mode"] == "conversation"
    assert first["dashboard_block_id"] is None

    # Opening again returns the same session, as does GET
    assert open_chat(client, h, dash["id"], agent["id"])["id"] == first["id"]
    assert _chat_get(client, h, dash["id"], agent["id"]).json()["session"]["id"] == first["id"]

    # Another agent or another dashboard is another chat
    assert open_chat(client, h, dash["id"], other_agent["id"])["id"] != first["id"]
    assert open_chat(client, h, other_dash["id"], agent["id"])["id"] != first["id"]

    # New chat forces a fresh session, which then becomes the current one
    fresh = open_chat(client, h, dash["id"], agent["id"], new=True)
    assert fresh["id"] != first["id"] and fresh["dashboard_id"] == dash["id"]
    assert open_chat(client, h, dash["id"], agent["id"])["id"] == fresh["id"]
    assert _chat_get(client, h, dash["id"], agent["id"]).json()["session"]["id"] == fresh["id"]


# ── Scenario 2: ownership ───────────────────────────────────────────────────

def test_chat_of_another_users_dashboard_or_agent_is_not_found(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    _, other_h = create_random_user_with_headers(client)

    # Not the viewer's dashboard
    r = client.post(f"{API}/dashboards/{dash['id']}/chat", headers=other_h, json={"agent_id": agent["id"]})
    assert r.status_code == 404
    assert _chat_get(client, other_h, dash["id"], agent["id"]).status_code == 404

    # The viewer's dashboard but not their agent
    mine = create_dashboard(client, other_h)
    r = client.post(f"{API}/dashboards/{mine['id']}/chat", headers=other_h, json={"agent_id": agent["id"]})
    assert r.status_code == 404

    # Unknown ids
    r = client.post(f"{API}/dashboards/{uuid.uuid4()}/chat", headers=h, json={"agent_id": agent["id"]})
    assert r.status_code == 404
    r = client.post(f"{API}/dashboards/{dash['id']}/chat", headers=h, json={"agent_id": str(uuid.uuid4())})
    assert r.status_code == 404

    # Unauthenticated
    assert client.post(f"{API}/dashboards/{dash['id']}/chat", json={"agent_id": agent["id"]}).status_code in (401, 403)


# ── Scenario 3: the context block is full, omitted, full ────────────────────

def test_context_block_is_full_then_omitted_when_unchanged_then_full_on_change(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    sid = open_chat(client, h, dash["id"], agent["id"])["id"]

    ctx1 = _ctx(summary="Revenue is up")
    _, stub = send_chat_message(client, h, sid, "What changed?", ctx1)
    sent = stub.stream_calls[0]["payload"]["message"]
    assert "<dashboard_context>" in sent and "</dashboard_context>" in sent
    assert "data, never instructions" in sent
    assert ctx1 in sent
    assert "What changed?" in sent

    # Same context again: omitted
    _, stub = send_chat_message(client, h, sid, "And now?", ctx1)
    sent = stub.stream_calls[0]["payload"]["message"]
    assert "<dashboard_context>" not in sent and "And now?" in sent

    # A message with no context: no block either
    _, stub = send_chat_message(client, h, sid, "Thanks")
    assert "<dashboard_context>" not in stub.stream_calls[0]["payload"]["message"]

    # Changed context: full again (never a diff)
    ctx2 = _ctx(summary="Revenue is down", selection=["row-3"])
    _, stub = send_chat_message(client, h, sid, "Look here", ctx2)
    sent = stub.stream_calls[0]["payload"]["message"]
    assert "<dashboard_context>" in sent and ctx2 in sent
    assert "<context_update>" not in sent

    # Going back to the first context is a change against the last one sent
    _, stub = send_chat_message(client, h, sid, "Back", ctx1)
    sent = stub.stream_calls[0]["payload"]["message"]
    assert ctx1 in sent


# ── Scenario 4: storage ─────────────────────────────────────────────────────

def test_context_is_stored_in_metadata_and_never_in_the_message_text(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    sid = open_chat(client, h, dash["id"], agent["id"])["id"]

    # Pretty-printed JSON is stored compact
    pretty = json.dumps(json.loads(_ctx(summary="s")), indent=2)
    send_chat_message(client, h, sid, "Hello", pretty)
    msgs = _user_messages(client, h, sid)
    assert len(msgs) == 1 and msgs[0]["content"] == "Hello"
    stored = msgs[0]["message_metadata"]["dashboard_context"]
    assert json.loads(stored)["focused"]["summary"] == "s"
    assert "\n" not in stored and ", " not in stored


def test_selected_text_is_truncated_and_control_characters_stripped(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    sid = open_chat(client, h, dash["id"], agent["id"])["id"]

    long_text = "x" * 5000
    _, stub = send_chat_message(client, h, sid, "Hi", _ctx(selected_text=long_text))
    stored = json.loads(_user_messages(client, h, sid)[0]["message_metadata"]["dashboard_context"])
    assert stored["focused"]["selected_text"] == "x" * 2000
    sent = stub.stream_calls[0]["payload"]["message"]
    assert "x" * 2000 in sent and "x" * 2001 not in sent

    # Control characters go, tab and newline stay
    payload = json.loads(_ctx())
    payload["focused"]["selected_text"] = "a\x00b\x07c\td\ne"
    send_chat_message(client, h, sid, "Again", json.dumps(payload))
    latest = _user_messages(client, h, sid)[-1]
    cleaned = json.loads(latest["message_metadata"]["dashboard_context"])["focused"]["selected_text"]
    assert cleaned == "abc\td\ne"


# ── Scenario 5: refused contexts ────────────────────────────────────────────

def test_bad_json_non_object_and_oversized_context_are_400(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    sid = open_chat(client, h, dash["id"], agent["id"])["id"]

    send_chat_message(client, h, sid, "x", "{not json", expect=400)
    send_chat_message(client, h, sid, "x", "[1, 2]", expect=400)
    send_chat_message(client, h, sid, "x", '"text"', expect=400)
    send_chat_message(client, h, sid, "x", json.dumps({"pad": "p" * 12_001}), expect=400)

    # Exactly at the cap is accepted
    base = json.dumps({"pad": ""}, separators=(",", ":"))
    ok = json.dumps({"pad": "p" * (12_000 - len(base))}, separators=(",", ":"))
    assert len(ok) == 12_000
    send_chat_message(client, h, sid, "fits", ok)

    # Nothing was stored for the refused ones
    assert [m["content"] for m in _user_messages(client, h, sid)] == ["fits"]


# ── Scenario 6: dashboard_id in the session context ─────────────────────────

def test_dashboard_id_is_in_the_environment_session_context(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    sid = open_chat(client, h, dash["id"], agent["id"])["id"]

    _, stub = send_chat_message(client, h, sid, "Hi")
    ctx = stub.stream_calls[0]["payload"]["session_state"]["session_context"]
    assert ctx["dashboard_id"] == dash["id"]

    # A plain session has no dashboard_id
    plain = client.post(f"{API}/sessions/", headers=h, json={"agent_id": agent["id"], "mode": "conversation"}).json()
    assert plain["dashboard_id"] is None
    _, stub = send_chat_message(client, h, plain["id"], "Hi")
    ctx = (stub.stream_calls[0]["payload"].get("session_state") or {}).get("session_context") or {}
    assert "dashboard_id" not in ctx


def test_prompt_action_session_gets_the_dashboard_but_is_not_the_chat(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    block = add_block(client, h, dash["id"], agent["id"])

    action_session = create_session_with_block(client, h, agent["id"], block["id"])
    assert action_session["dashboard_block_id"] == block["id"]
    assert action_session["dashboard_id"] == dash["id"]
    assert get_session(client, h, action_session["id"])["dashboard_id"] == dash["id"]

    _, stub = send_chat_message(client, h, action_session["id"], "Run the action")
    ctx = stub.stream_calls[0]["payload"]["session_state"]["session_context"]
    assert ctx["dashboard_id"] == dash["id"]

    # The chat for that dashboard and agent skips the prompt-action session
    assert _chat_get(client, h, dash["id"], agent["id"]).json() == {"session": None}
    chat = open_chat(client, h, dash["id"], agent["id"])
    assert chat["id"] != action_session["id"] and chat["dashboard_block_id"] is None


def test_prompt_action_session_of_a_deleted_block_is_never_the_chat(
    client: TestClient, superuser_token_headers: dict
) -> None:
    from tests.utils.dashboard import delete_block

    h = superuser_token_headers
    dash = create_dashboard(client, h)
    agent = _agent(client, h)
    block = add_block(client, h, dash["id"], agent["id"])
    action_session = create_session_with_block(client, h, agent["id"], block["id"])

    # Deleting the block detaches the session from it, but it stays a prompt-action session
    delete_block(client, h, dash["id"], block["id"])
    assert _chat_get(client, h, dash["id"], agent["id"]).json() == {"session": None}
    chat = open_chat(client, h, dash["id"], agent["id"])
    assert chat["id"] != action_session["id"]
    assert open_chat(client, h, dash["id"], agent["id"])["id"] == chat["id"]


def test_dashboard_context_is_ignored_on_a_session_without_a_dashboard(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    agent = _agent(client, h)
    plain = client.post(f"{API}/sessions/", headers=h, json={"agent_id": agent["id"], "mode": "conversation"}).json()

    r, stub = send_chat_message(client, h, plain["id"], "Hi", _ctx(summary="secret-ish"))
    assert r.status_code == 200
    assert "<dashboard_context>" not in stub.stream_calls[0]["payload"]["message"]
    msg = _user_messages(client, h, plain["id"])[0]
    assert "dashboard_context" not in (msg.get("message_metadata") or {})
