"""
Producer env tools for capability requests (``/agent/api-requests/*``).

Called by env-core with the env token + ``X-Agent-Env-Id``. Every answer is
HTTP 200: the result or ``{error: {code, message}}``.

Test scenarios:
  1. list / get / update-status are scoped to the env's own agent
  2. Another agent's request, and a session-kind request, answer not_found
  3. agent_api disabled answers agent_api_disabled on every tool
  4. Invalid status answers invalid_input; the consumer sees the closing note
  5. Env auth is required
"""
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import update_agent
from tests.utils.agent_api_keys import setup_producer
from tests.utils.agent_api_requests import connected_consumer, file_request, requests_url
from tests.utils.background_tasks import drain_tasks
from tests.utils.agent_env import create_env_with_token

API = settings.API_V1_STR


def _seed_message(client, headers, session_id):
    from unittest.mock import patch
    from tests.stubs.agent_env_stub import StubAgentEnvConnector
    from tests.utils.message import send_message
    stub = StubAgentEnvConnector(response_text="Noted.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content="Needs work.")
        drain_tasks()

TOOLS = f"{API}/agent/api-requests"


def _env_headers(client, db: Session, h, agent_id: str) -> dict[str, str]:
    me = client.get(f"{API}/users/me", headers=h).json()
    _, env_h = create_env_with_token(db, agent_id=agent_id, owner_id=me["id"])
    return env_h


def test_tools_list_get_update_scoped_to_the_env_agent(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    p1 = setup_producer(client, h, name="Tools Producer One")["id"]
    p2 = setup_producer(client, h, name="Tools Producer Two")["id"]
    c1 = connected_consumer(client, h, p1, name="Tools Consumer One")
    c2 = connected_consumer(client, h, p2, name="Tools Consumer Two")
    mine = file_request(client, p1, c1["headers"], title="Request on one")
    other = file_request(client, p2, c2["headers"], title="Request on two")
    env_h = _env_headers(client, db, h, p1)

    lst = client.post(f"{TOOLS}/list", headers=env_h, json={})
    assert lst.status_code == 200, lst.text
    body = lst.json()
    assert body["count"] == 1
    row = body["requests"][0]
    assert row["id"] == mine["id"]
    assert row["title"] == "Request on one"
    assert row["status"] == "new"
    assert row["consumer_agent_name"] == "Tools Consumer One"
    assert row["requester_display"]
    assert row["spec_version_seen"] == "1.0.0"

    filtered = client.post(f"{TOOLS}/list", headers=env_h, json={"status": "completed"}).json()
    assert filtered["count"] == 0 and filtered["requests"] == []

    got = client.post(f"{TOOLS}/get", headers=env_h, json={"request_id": mine["id"]}).json()
    assert got["id"] == mine["id"]
    assert got["need"].startswith("I need to export")
    assert got["proposed_endpoint"]["path"] == "/orders/export"
    assert got["resolution_note"] is None

    foreign = client.post(f"{TOOLS}/get", headers=env_h, json={"request_id": other["id"]})
    assert foreign.status_code == 200
    assert foreign.json()["error"]["code"] == "not_found"
    foreign_upd = client.post(
        f"{TOOLS}/update-status", headers=env_h,
        json={"request_id": other["id"], "status": "completed"},
    ).json()
    assert foreign_upd["error"]["code"] == "not_found"
    assert client.get(requests_url(p2), headers=c2["headers"]).json()["data"][0]["status"] == "new"

    upd = client.post(
        f"{TOOLS}/update-status", headers=env_h,
        json={"request_id": mine["id"], "status": "completed", "resolution_note": "Shipped it."},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["status"] == "completed"
    assert upd.json()["resolution_note"] == "Shipped it."
    seen = client.get(requests_url(p1), headers=c1["headers"]).json()["data"][0]
    assert seen["status"] == "completed" and seen["resolution_note"] == "Shipped it."

    in_progress = client.post(
        f"{TOOLS}/list", headers=env_h, json={"status": "completed", "limit": 5}
    ).json()
    assert in_progress["count"] == 1


def test_unknown_request_and_invalid_status(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Tools Invalid Producer")["id"]
    c = connected_consumer(client, h, pid)
    created = file_request(client, pid, c["headers"])
    env_h = _env_headers(client, db, h, pid)

    missing = client.post(
        f"{TOOLS}/get", headers=env_h,
        json={"request_id": "00000000-0000-0000-0000-000000000001"},
    ).json()
    assert missing["error"]["code"] == "not_found"

    bad = client.post(
        f"{TOOLS}/update-status", headers=env_h,
        json={"request_id": created["id"], "status": "exploded"},
    ).json()
    assert bad["error"]["code"] == "invalid_input"
    bad_list = client.post(f"{TOOLS}/list", headers=env_h, json={"status": "exploded"}).json()
    assert bad_list["error"]["code"] == "invalid_input"


def test_session_kind_request_is_not_visible_through_the_tools(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    from tests.utils.session import create_session_via_api

    h = superuser_token_headers
    pid = setup_producer(client, h, name="Tools Kind Producer")["id"]
    session = create_session_via_api(client, h, pid)
    drain_tasks()
    _seed_message(client, h, session["id"])
    r = client.post(
        f"{API}/improvement-requests", headers=h,
        json={"session_id": session["id"], "comment": "Session based."},
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "session"
    env_h = _env_headers(client, db, h, pid)

    assert client.post(f"{TOOLS}/list", headers=env_h, json={}).json()["count"] == 0
    got = client.post(f"{TOOLS}/get", headers=env_h, json={"request_id": r.json()["id"]}).json()
    assert got["error"]["code"] == "not_found"


def test_tools_answer_agent_api_disabled_and_require_env_auth(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Tools Disabled Producer")["id"]
    c = connected_consumer(client, h, pid)
    created = file_request(client, pid, c["headers"])
    env_h = _env_headers(client, db, h, pid)
    update_agent(client, h, pid, agent_api_enabled=False)

    for path, body in (
        ("list", {}),
        ("get", {"request_id": created["id"]}),
        ("update-status", {"request_id": created["id"], "status": "declined"}),
    ):
        r = client.post(f"{TOOLS}/{path}", headers=env_h, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["error"]["code"] == "agent_api_disabled", path

    anon = client.post(f"{TOOLS}/list", json={})
    assert anon.status_code in (401, 403)
    user_only = client.post(f"{TOOLS}/list", headers=h, json={})
    assert user_only.status_code in (401, 403)
