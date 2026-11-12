"""
Improvement request ``kind`` across the existing surfaces.

Session requests keep reporting ``kind=session``; capability requests
(``agent_api``) appear on the same inbox, /mine and CLI lists with ``kind`` and
``title``, and the shared lifecycle (status, delete) works unchanged.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent_api_keys import setup_producer
from tests.utils.agent_api_requests import connected_consumer, file_request, requests_url
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import account_cli_headers, bootstrap_account_token
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


def _seed_message(client, headers, session_id):
    from unittest.mock import patch
    from tests.stubs.agent_env_stub import StubAgentEnvConnector
    from tests.utils.message import send_message
    stub = StubAgentEnvConnector(response_text="Noted.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content="Needs work.")
        drain_tasks()



def test_kind_and_title_on_inbox_mine_and_cli_lists(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Kind Producer")["id"]
    c = connected_consumer(client, h, pid)
    cap = file_request(client, pid, c["headers"], title="Capability ask")

    session = create_session_via_api(client, h, pid)
    drain_tasks()
    _seed_message(client, h, session["id"])
    sess = client.post(
        f"{API}/improvement-requests", headers=h,
        json={"session_id": session["id"], "comment": "Session ask."},
    )
    assert sess.status_code == 201, sess.text
    assert sess.json()["kind"] == "session"
    assert sess.json()["title"] is None

    inbox = {x["id"]: x for x in client.get(
        f"{API}/agents/{pid}/improvement-requests", headers=h).json()["data"]}
    assert inbox[cap["id"]]["kind"] == "agent_api"
    assert inbox[cap["id"]]["title"] == "Capability ask"
    assert inbox[sess.json()["id"]]["kind"] == "session"

    mine = {x["id"]: x for x in client.get(
        f"{API}/improvement-requests/mine", headers=h).json()["data"]}
    assert mine[cap["id"]]["kind"] == "agent_api"
    assert mine[sess.json()["id"]]["kind"] == "session"

    acc, _ = bootstrap_account_token(client, h)
    cli = client.get(f"{API}/cli/account/improvement-requests", headers=account_cli_headers(acc))
    assert cli.status_code == 200, cli.text
    cli_rows = {x["id"]: x for x in cli.json()["data"]}
    assert cli_rows[cap["id"]]["kind"] == "agent_api"
    assert cli_rows[cap["id"]]["title"] == "Capability ask"
    assert cli_rows[sess.json()["id"]]["kind"] == "session"


def test_capability_request_lifecycle_through_existing_routes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Kind Lifecycle Producer")["id"]
    c = connected_consumer(client, h, pid)
    cap = file_request(client, pid, c["headers"])

    for status in ("in_progress", "declined"):
        r = client.patch(
            f"{API}/improvement-requests/{cap['id']}", headers=h,
            json={"status": status, "resolution_note": f"now {status}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == status
        assert r.json()["kind"] == "agent_api"

    d = client.delete(f"{API}/improvement-requests/{cap['id']}", headers=h)
    assert d.status_code == 200, d.text
    assert client.get(requests_url(pid), headers=c["headers"]).json()["count"] == 0
