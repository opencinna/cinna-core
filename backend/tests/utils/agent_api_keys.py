"""Shared setup helpers for Agent REST API issued-key tests."""
from fastapi.testclient import TestClient

from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers, promote_to_developer


def setup_producer(
    client: TestClient,
    headers: dict[str, str],
    name: str = "External Key Producer",
    agent_api_enabled: bool = True,
    external_access_enabled: bool = True,
    identity_enabled: bool = False,
) -> dict:
    """Create an agent with the agent_api toggles this file needs."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(
        client,
        headers,
        agent["id"],
        agent_api_enabled=agent_api_enabled,
        agent_api_identity_enabled=identity_enabled,
        agent_api_external_access_enabled=external_access_enabled,
    )
    return agent


def make_developer(
    client: TestClient, superuser_headers: dict[str, str]
) -> tuple[dict, dict[str, str]]:
    """A developer user with a default AI credential (can own agents)."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, credential_type="anthropic", set_default=True)
    return user, headers
