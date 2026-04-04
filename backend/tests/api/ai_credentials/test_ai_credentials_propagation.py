"""
AI Credentials Propagation Tests

Tests that AI credentials propagate correctly to:
1. User profile (via set-default → auto-sync to ai_credentials_encrypted)
2. Agent environments (via explicit linking with conversation/building credential IDs)
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.environments.environment import AgentEnvironment
from app.services.environments.environment_service import EnvironmentService
from tests.utils.ai_credential import (
    create_random_ai_credential,
    delete_ai_credential,
    get_affected_environments,
    get_ai_credentials_profile,
    get_ai_credentials_status,
    update_ai_credential,
)
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers


def _create_agent_with_environment(
    client: TestClient, headers: dict[str, str]
) -> dict:
    """Create agent, drain background tasks, and re-fetch to get active_environment_id."""
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    r = client.get(
        f"/api/v1/agents/{agent['id']}", headers=headers
    )
    assert r.status_code == 200
    agent = r.json()
    assert agent["active_environment_id"] is not None
    return agent


# ---------------------------------------------------------------------------
# Profile Sync (set-default → user profile)
# ---------------------------------------------------------------------------


def test_set_default_syncs_anthropic_to_profile(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Setting an anthropic credential as default syncs its key to user profile."""
    api_key = "sk-ant-api03-test-sync-key"
    create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic", api_key=api_key,
        set_default=True,
    )

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == api_key


def test_set_default_syncs_minimax_to_profile(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Setting a minimax credential as default syncs its key to user profile."""
    api_key = "mm-test-sync-key-123"
    create_random_ai_credential(
        client, superuser_token_headers, credential_type="minimax", api_key=api_key,
        set_default=True,
    )

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["minimax_api_key"] == api_key


def test_set_default_syncs_openai_compatible_to_profile(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Setting an openai_compatible credential as default syncs key, base_url, and model."""
    api_key = "sk-oai-compat-sync-key"
    base_url = "https://api.example.com/v1"
    model = "gpt-4-turbo"
    create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai_compatible",
        api_key=api_key,
        base_url=base_url,
        model=model,
        set_default=True,
    )

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["openai_compatible_api_key"] == api_key
    assert profile["openai_compatible_base_url"] == base_url
    assert profile["openai_compatible_model"] == model


def test_update_default_resyncs_to_profile(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Updating the api_key of a default credential re-syncs to user profile."""
    cred = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic", api_key="sk-old-key",
        set_default=True,
    )

    new_key = "sk-ant-api03-updated-key"
    update_ai_credential(client, superuser_token_headers, cred["id"], api_key=new_key)

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == new_key


def test_delete_default_clears_profile(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Deleting a default credential clears its field from user profile."""
    cred = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic", api_key="sk-to-delete",
        set_default=True,
    )

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == "sk-to-delete"

    delete_ai_credential(client, superuser_token_headers, cred["id"])

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] is None


def test_set_default_replaces_previous_in_profile(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Setting a new default replaces the previous credential's key in profile."""
    create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic", api_key="sk-first",
        set_default=True,
    )
    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == "sk-first"

    # Set a new credential as default — should replace the previous key in profile
    create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic", api_key="sk-second",
        set_default=True,
    )
    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == "sk-second"


def test_credentials_status_reflects_defaults(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """GET /users/me/ai-credentials/status shows has_*_api_key: true after set-default."""
    create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic",
        set_default=True,
    )

    status = get_ai_credentials_status(client, superuser_token_headers)
    assert status["has_anthropic_api_key"] is True


# ---------------------------------------------------------------------------
# Affected Environments Query
# ---------------------------------------------------------------------------


def _link_credential_to_environment(
    db: Session,
    environment_id: uuid.UUID,
    credential_id: uuid.UUID,
    conversation: bool = False,
    building: bool = False,
) -> None:
    """Directly set credential IDs on an environment via DB (not exposed via API)."""
    env = db.get(AgentEnvironment, environment_id)
    assert env is not None, f"Environment {environment_id} not found"
    if conversation:
        env.conversation_ai_credential_id = credential_id
    if building:
        env.building_ai_credential_id = credential_id
    db.add(env)
    db.commit()
    db.refresh(env)


def test_affected_environments_empty(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Credential not linked to any environment → count=0."""
    cred = create_random_ai_credential(client, superuser_token_headers)

    body = get_affected_environments(client, superuser_token_headers, cred["id"])
    assert body["count"] == 0
    assert body["environments"] == []
    assert body["credential_id"] == cred["id"]


def test_affected_environments_conversation_only(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
) -> None:
    """Environment linked via conversation_ai_credential_id → usage='conversation'."""
    agent = _create_agent_with_environment(client, superuser_token_headers)
    cred = create_random_ai_credential(client, superuser_token_headers)

    _link_credential_to_environment(
        db, agent["active_environment_id"], cred["id"], conversation=True
    )

    body = get_affected_environments(client, superuser_token_headers, cred["id"])
    assert body["count"] == 1
    env_info = body["environments"][0]
    assert env_info["usage"] == "conversation"
    assert env_info["agent_id"] == agent["id"]


def test_affected_environments_building_only(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
) -> None:
    """Environment linked via building_ai_credential_id → usage='building'."""
    agent = _create_agent_with_environment(client, superuser_token_headers)
    cred = create_random_ai_credential(client, superuser_token_headers)

    _link_credential_to_environment(
        db, agent["active_environment_id"], cred["id"], building=True
    )

    body = get_affected_environments(client, superuser_token_headers, cred["id"])
    assert body["count"] == 1
    env_info = body["environments"][0]
    assert env_info["usage"] == "building"
    assert env_info["agent_id"] == agent["id"]


def test_affected_environments_both(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
) -> None:
    """Same credential linked to both conversation and building → usage='conversation & building'."""
    agent = _create_agent_with_environment(client, superuser_token_headers)
    cred = create_random_ai_credential(client, superuser_token_headers)

    _link_credential_to_environment(
        db, agent["active_environment_id"], cred["id"], conversation=True, building=True
    )

    body = get_affected_environments(client, superuser_token_headers, cred["id"])
    assert body["count"] == 1
    env_info = body["environments"][0]
    assert env_info["usage"] == "conversation & building"


def test_affected_environments_multiple_envs(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
) -> None:
    """Multiple environments using the same credential are all returned."""
    agent1 = _create_agent_with_environment(client, superuser_token_headers)
    agent2 = _create_agent_with_environment(client, superuser_token_headers)
    cred = create_random_ai_credential(client, superuser_token_headers)

    _link_credential_to_environment(
        db, agent1["active_environment_id"], cred["id"], conversation=True
    )
    _link_credential_to_environment(
        db, agent2["active_environment_id"], cred["id"], building=True
    )

    body = get_affected_environments(client, superuser_token_headers, cred["id"])
    assert body["count"] == 2

    agent_ids = {e["agent_id"] for e in body["environments"]}
    assert agent_ids == {agent1["id"], agent2["id"]}


def test_affected_environments_includes_default_credential_envs(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Environment using default credentials (use_default_ai_credentials=True)
    should appear in affected environments when the default anthropic credential
    is queried.
    """
    api_key = "sk-ant-api03-default-cred-affected-test"
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key=api_key,
        name="default-affected-test",
        set_default=True,
    )

    # Create agent → auto-creates environment with default credentials
    agent = _create_agent_with_environment(client, superuser_token_headers)
    env_id = agent["active_environment_id"]
    assert env_id is not None

    # Verify the environment uses default credentials (credential IDs are null)
    env = db.get(AgentEnvironment, env_id)
    assert env is not None
    assert env.use_default_ai_credentials is True
    assert env.conversation_ai_credential_id is None
    assert env.building_ai_credential_id is None

    # Query affected environments for the default credential
    body = get_affected_environments(client, superuser_token_headers, cred["id"])

    assert body["count"] >= 1, (
        f"Expected at least 1 affected environment for default credential, "
        f"got {body['count']}. Environments using default credentials with "
        f"null credential IDs are not being detected."
    )
    env_ids = {e["environment_id"] for e in body["environments"]}
    assert str(env_id) in env_ids, (
        f"Environment {env_id} (using default credentials) should appear in "
        f"affected environments list but was not found. "
        f"Found: {env_ids}"
    )


def test_affected_environments_other_user_forbidden(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Another user cannot query affected environments for a credential they don't own."""
    cred = create_random_ai_credential(client, superuser_token_headers)

    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )

    r = client.get(
        f"/api/v1/ai-credentials/{cred['id']}/affected-environments",
        headers=other_headers,
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Full E2E: Credential Propagation via Environment Rebuild
# ---------------------------------------------------------------------------


def test_credential_propagation_through_environment_rebuild(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    End-to-end credential propagation flow:

    1. Create anthropic AI credential and set as default
    2. Create agent → environment auto-created with that credential
    3. Verify generated .env contains correct ANTHROPIC_API_KEY and SDK adapters
    4. Update credential with a new API key
    5. Trigger rebuild (simulates the "propagate credentials" user action)
    6. Verify .env now contains the updated key
    7. Verify adapter stub received the rebuild call
    """
    api_key_v1 = "sk-ant-api03-original-key-for-propagation"

    # --- Step 1: Create anthropic credential and set as default ---
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key=api_key_v1,
        name="propagation-test-credential",
        set_default=True,
    )

    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == api_key_v1

    # --- Step 2: Create agent (triggers background environment creation) ---
    agent = _create_agent_with_environment(client, superuser_token_headers)
    env_id = agent["active_environment_id"]
    assert env_id is not None

    # --- Step 3: Verify .env has original key and correct SDK adapters ---
    lm = EnvironmentService.get_lifecycle_manager()
    env_file = lm.instances_dir / str(env_id) / ".env"
    assert env_file.exists(), f".env file not found at {env_file}"

    env_content = env_file.read_text()
    assert f"ANTHROPIC_API_KEY={api_key_v1}" in env_content
    assert "SDK_ADAPTER_BUILDING=claude-code/anthropic" in env_content
    assert "SDK_ADAPTER_CONVERSATION=claude-code/anthropic" in env_content

    # Verify adapter received initialize + start calls
    adapter = lm._test_adapter
    assert len(adapter.initialize_calls) == 1
    assert adapter.start_calls >= 1

    # --- Step 4: Update credential with new API key ---
    api_key_v2 = "sk-ant-api03-updated-key-after-propagation"
    update_ai_credential(client, superuser_token_headers, cred["id"], api_key=api_key_v2)

    # Since this credential is default, the user profile auto-syncs
    profile = get_ai_credentials_profile(client, superuser_token_headers)
    assert profile["anthropic_api_key"] == api_key_v2

    # --- Step 5: Query affected environments and rebuild via that list ---
    affected = get_affected_environments(client, superuser_token_headers, cred["id"])
    assert affected["count"] >= 1, (
        "Environment using default credential should appear in affected list"
    )

    affected_env_ids = {e["environment_id"] for e in affected["environments"]}
    assert str(env_id) in affected_env_ids, (
        f"Environment {env_id} not found in affected environments"
    )

    # Rebuild each affected environment (simulates the UI batch rebuild flow)
    rebuild_calls_before = len(adapter.rebuild_calls)
    for affected_env in affected["environments"]:
        r = client.post(
            f"/api/v1/environments/{affected_env['environment_id']}/rebuild",
            headers=superuser_token_headers,
        )
        assert r.status_code == 200
    drain_tasks()

    # --- Step 6: Verify .env now contains the updated key ---
    env_content = env_file.read_text()
    assert f"ANTHROPIC_API_KEY={api_key_v2}" in env_content
    assert api_key_v1 not in env_content, "Old API key should not remain in .env"

    # --- Step 7: Verify adapter received the rebuild call ---
    assert len(adapter.rebuild_calls) >= rebuild_calls_before + 1
    rebuild_info = adapter.rebuild_calls[-1]
    assert rebuild_info["was_running"] is True
