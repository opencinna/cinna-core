"""POST /api/v1/agents/ without the AI key its environment needs: a 400, no agent.

Bug 4 of the catalog-unification follow-up: the route answered 500
(``EnvironmentCredentialError`` escaped) and, because the agent row was
committed before the environment was validated, left an agent with no
environment behind. The SDKs and credentials are now resolved before any row
is written and the error maps to its own status (400).

Scenarios:
  1. A developer with no AI credential at all: 400 naming the default SDK, the
     agent list stays empty; after adding a default Anthropic credential the
     same request succeeds and the agent gets its environment.
  2. A developer whose default SDK is MiniMax but who only holds an Anthropic
     credential: 400 naming ``claude-code/minimax``, no agent written.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import list_agents
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import list_environments
from tests.utils.user import create_random_user_with_headers, promote_to_developer

API = settings.API_V1_STR


def _developer_without_ai_key(
    client: TestClient, superuser_headers: dict[str, str]
) -> dict[str, str]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    return headers


def _agent_names(client: TestClient, headers: dict[str, str]) -> list[str]:
    return [a["name"] for a in list_agents(client, headers)["data"]]


def test_create_agent_without_any_ai_key_is_400_and_writes_no_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. A fresh developer with no AI credential creates an agent: 400 with the
       missing-key message for the default SDK.
    2. The developer's agent list is still empty.
    3. With a default Anthropic credential the same request answers 200 and
       the agent has its default environment.
    """
    headers = _developer_without_ai_key(client, superuser_token_headers)

    # ── Phase 1: missing key → 400 with a clear message ───────────────────
    r = client.post(f"{API}/agents/", headers=headers, json={"name": "No Key Agent"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == (
        "Missing required API key for SDK 'claude-code/anthropic'. "
        "Please add it in user settings."
    )

    # ── Phase 2: no agent row was written ─────────────────────────────────
    assert _agent_names(client, headers) == []

    # ── Phase 3: with a key the same request succeeds ─────────────────────
    create_random_ai_credential(client, headers, set_default=True)
    r = client.post(f"{API}/agents/", headers=headers, json={"name": "No Key Agent"})
    assert r.status_code == 200, r.text
    agent = r.json()
    drain_tasks()

    assert _agent_names(client, headers) == ["No Key Agent"]
    envs = list_environments(client, headers, agent["id"])
    assert envs["count"] == 1


def test_create_agent_missing_key_for_the_default_sdk_names_that_sdk(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. A developer holds only an Anthropic credential but defaults to MiniMax.
    2. Creating an agent answers 400 naming ``claude-code/minimax``.
    3. No agent row was written.
    """
    headers = _developer_without_ai_key(client, superuser_token_headers)
    create_random_ai_credential(client, headers, set_default=True)
    r = client.patch(
        f"{API}/users/me",
        headers=headers,
        json={
            "default_sdk_conversation": "claude-code/minimax",
            "default_sdk_building": "claude-code/minimax",
        },
    )
    assert r.status_code == 200, r.text

    r = client.post(f"{API}/agents/", headers=headers, json={"name": "MiniMax Agent"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail.startswith("Missing required API key for SDK 'claude-code/minimax'."), detail

    assert _agent_names(client, headers) == []
