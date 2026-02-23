"""
Integration test: Google Service Account credential lifecycle with agent environment sync.

Tests the full workflow:
  1. User creates a google_service_account credential with valid SA JSON
  2. Verify the record is stored correctly (encrypted data round-trips)
  3. User creates an agent (environment created via stub)
  4. User links the credential to the agent
  5. Verify the agent-env received the SA file + credentials.json metadata
  6. User unlinks the credential from the agent
  7. Verify the agent-env no longer has the credential
"""
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    get_credential_with_data,
    link_credential_to_agent,
    unlink_credential_from_agent,
    update_credential,
)

# Valid Google Service Account JSON for testing
_SA_JSON = {
    "type": "service_account",
    "project_id": "my-test-project",
    "private_key_id": "abc123def456",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "my-sa@my-test-project.iam.gserviceaccount.com",
    "client_id": "112233445566",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def test_service_account_credential_agent_env_sync(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full service-account credential → agent environment sync scenario:
      1. Create google_service_account credential with valid SA JSON
      2. Verify stored data round-trips correctly
      3. Create agent (environment stub)
      4. Link credential to agent → env receives SA file + metadata
      5. Verify credentials.json entry and service_account_files
      6. Unlink credential → env updated with empty credentials
      7. Verify credential is removed from agent env
    """
    # ── Phase 1: Create service account credential ────────────────────
    cred_name = "My GCP Service Account"
    cred_notes = "For BigQuery access"
    cred = create_random_credential(
        client,
        superuser_token_headers,
        credential_type="google_service_account",
        credential_data=_SA_JSON,
    )
    # Override name and notes for clearer assertions
    cred = update_credential(
        client, superuser_token_headers, cred["id"],
        name=cred_name, notes=cred_notes,
    )

    credential_id = cred["id"]
    assert cred["type"] == "google_service_account"
    assert cred["name"] == cred_name
    assert cred["notes"] == cred_notes
    assert cred["status"] == "complete"
    # Public response must not leak credential_data
    assert "credential_data" not in cred
    assert "encrypted_data" not in cred

    # ── Phase 2: Verify stored data round-trips ───────────────────────
    cred_with_data = get_credential_with_data(
        client, superuser_token_headers, credential_id,
    )
    stored_data = cred_with_data["credential_data"]

    assert stored_data["type"] == "service_account"
    assert stored_data["project_id"] == _SA_JSON["project_id"]
    assert stored_data["private_key_id"] == _SA_JSON["private_key_id"]
    assert stored_data["private_key"] == _SA_JSON["private_key"]
    assert stored_data["client_email"] == _SA_JSON["client_email"]
    assert stored_data["client_id"] == _SA_JSON["client_id"]

    # ── Phase 3: Create agent ─────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    # Use a shared adapter so we can inspect credentials_set
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 4: Link credential to agent ─────────────────────────────
    result = link_credential_to_agent(
        client, superuser_token_headers, agent_id, credential_id,
    )
    assert result["message"] == "Credential linked successfully"

    # Verify credential appears in agent's credential list
    agent_creds = get_agent_credentials(
        client, superuser_token_headers, agent_id,
    )
    assert agent_creds["count"] == 1
    assert agent_creds["data"][0]["id"] == credential_id
    assert agent_creds["data"][0]["type"] == "google_service_account"

    # ── Phase 5: Verify agent-env received correct credential data ────
    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter should have received credentials"

    # credentials_json: one entry with file_path reference (no private key)
    creds_json = env_data["credentials_json"]
    assert len(creds_json) == 1
    entry = creds_json[0]
    assert entry["id"] == credential_id
    assert entry["name"] == cred_name
    assert entry["type"] == "google_service_account"
    assert entry["notes"] == cred_notes

    # credential_data should be the processed reference, not the full SA JSON
    ref = entry["credential_data"]
    assert ref["file_path"] == f"credentials/{credential_id}.json"
    assert ref["project_id"] == _SA_JSON["project_id"]
    assert ref["client_email"] == _SA_JSON["client_email"]
    # Private key must NOT be in credentials.json entry
    assert "private_key" not in ref
    assert "private_key_id" not in ref

    # service_account_files: full SA JSON for writing as standalone file
    sa_files = env_data["service_account_files"]
    assert len(sa_files) == 1
    sa_file = sa_files[0]
    assert sa_file["credential_id"] == credential_id
    sa_content = sa_file["json_content"]
    assert sa_content["type"] == "service_account"
    assert sa_content["project_id"] == _SA_JSON["project_id"]
    assert sa_content["private_key"] == _SA_JSON["private_key"]
    assert sa_content["client_email"] == _SA_JSON["client_email"]

    # credentials_readme: should contain non-empty documentation
    assert env_data["credentials_readme"]
    assert "google_service_account" in env_data["credentials_readme"]

    # ── Phase 6: Unlink credential from agent ─────────────────────────
    result = unlink_credential_from_agent(
        client, superuser_token_headers, agent_id, credential_id,
    )
    assert result["message"] == "Credential unlinked successfully"

    # ── Phase 7: Verify agent-env updated — credential removed ────────
    env_data_after = shared_adapter.credentials_set
    assert env_data_after["credentials_json"] == []
    assert env_data_after["service_account_files"] == []

    # Agent's credential list should be empty
    agent_creds = get_agent_credentials(
        client, superuser_token_headers, agent_id,
    )
    assert agent_creds["count"] == 0
    assert agent_creds["data"] == []
