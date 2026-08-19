"""
Agent Improvement Requests — submission rate limits.

See docs/plans/agent_improvement_requests_plan.md §4.4 rule 5:
  - <= 5 requests per session, the 6th is 429.
  - <= 20 requests per user per rolling 24h, the 21st is 429.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


def _seed_message(
    client: TestClient, headers: dict[str, str], session_id: str, content: str = "One more report."
) -> None:
    stub = StubAgentEnvConnector(response_text="OK.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def _submit(client: TestClient, headers: dict[str, str], session_id: str):
    return client.post(
        f"{API}/improvement-requests", headers=headers, json={"session_id": session_id}
    )


def test_per_session_rate_limit_429_at_sixth_request(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """5 requests on one session succeed; the 6th is 429 with a per-session reason."""
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"])

    for i in range(5):
        r = _submit(client, superuser_token_headers, session["id"])
        assert r.status_code == 201, f"request {i}: {r.text}"

    r = _submit(client, superuser_token_headers, session["id"])
    assert r.status_code == 429, r.text
    assert "5" in r.json()["detail"]


def test_per_user_daily_rate_limit_429_at_21st_request(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    20 requests spread across 20 different sessions (well under the
    per-session cap of 5 each) succeed; the 21st — on yet another fresh
    session — is 429 with the daily reason, not the per-session one.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()

    for i in range(20):
        session = create_session_via_api(client, superuser_token_headers, agent["id"])
        _seed_message(client, superuser_token_headers, session["id"], content=f"report {i}")
        r = _submit(client, superuser_token_headers, session["id"])
        assert r.status_code == 201, f"request {i}: {r.text}"

    overflow_session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, overflow_session["id"], content="report 21")
    r = _submit(client, superuser_token_headers, overflow_session["id"])
    assert r.status_code == 429, r.text
    assert "24 hours" in r.json()["detail"]
