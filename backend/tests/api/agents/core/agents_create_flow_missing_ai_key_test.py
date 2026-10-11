"""POST /api/v1/agents/create-flow without the AI key its environment needs.

The route resolves the SDKs and AI credentials before the SSE stream starts, so
a missing key is a clean 400 (same message as POST /agents/), no agent row is
written, and the billed ``generate_agent_configuration`` call never runs.

Scenarios:
  1. A developer with no AI credential: 400 naming the default SDK, agent list
     empty, configuration generator not called.
  2. A developer defaulting to MiniMax with only an Anthropic credential: 400
     naming ``claude-code/minimax``, no agent, no configuration call.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import list_agents
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers, promote_to_developer

API = settings.API_V1_STR

_CONFIG_GENERATOR = (
    "app.services.ai_functions.ai_functions_service."
    "AIFunctionsService.generate_agent_configuration"
)


def _developer_without_ai_key(
    client: TestClient, superuser_headers: dict[str, str]
) -> dict[str, str]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    return headers


def _post_create_flow(client: TestClient, headers: dict[str, str]):
    with patch(_CONFIG_GENERATOR) as config_generator:
        r = client.post(
            f"{API}/agents/create-flow",
            headers=headers,
            json={"description": "An agent that drafts release notes", "mode": "building"},
        )
        drain_tasks()
    return r, config_generator


def test_create_flow_without_any_ai_key_is_400_and_writes_no_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. A fresh developer with no AI credential POSTs /create-flow.
    2. 400 with the missing-key message for the default SDK (no SSE stream).
    3. No agent row; the configuration generator was never called.
    """
    headers = _developer_without_ai_key(client, superuser_token_headers)

    r, config_generator = _post_create_flow(client, headers)

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == (
        "Missing required API key for SDK 'claude-code/anthropic'. "
        "Please add it in user settings."
    )
    config_generator.assert_not_called()
    assert list_agents(client, headers)["data"] == []


def test_create_flow_missing_key_for_the_default_sdk_names_that_sdk(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. A developer holds only an Anthropic credential but defaults to MiniMax.
    2. /create-flow answers 400 naming ``claude-code/minimax``.
    3. No agent row; the configuration generator was never called.
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

    r, config_generator = _post_create_flow(client, headers)

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail.startswith("Missing required API key for SDK 'claude-code/minimax'."), detail
    config_generator.assert_not_called()
    assert list_agents(client, headers)["data"] == []
