"""
Tests for the email_smtp credential type.

Covers:
 1. Full CRUD lifecycle for email_smtp credentials
 2. Field whitelisting — all 7 SMTP fields must appear in credentials.json for agents
 3. Redaction — password must be redacted in credentials_readme; other fields visible
 4. email_smtp status=complete when all fields are present
 5. Placeholder behaviour (status=incomplete when data is omitted)
 6. Access control — other users cannot read/update/delete the credential
"""
from fastapi.testclient import TestClient

from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_credential_with_data,
    get_agent_credentials,
    link_credential_to_agent,
    unlink_credential_from_agent,
)
from app.core.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SMTP_DATA = {
    "host": "smtp.example.com",
    "port": 587,
    "username": "user@example.com",
    "password": "super-secret-pass",
    "from_email": "sender@example.com",
    "use_tls": True,
    "use_ssl": False,
}


def _create_smtp_credential(client, headers, **overrides):
    """Create an email_smtp credential with default data, allowing field overrides."""
    data = {**_SMTP_DATA, **overrides}
    return create_random_credential(
        client, headers, credential_type="email_smtp", credential_data=data
    )


# ---------------------------------------------------------------------------
# CRUD lifecycle
# ---------------------------------------------------------------------------

def test_email_smtp_full_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full CRUD lifecycle for email_smtp credential:
      1. Create with all SMTP fields — verify public response (no data leakage)
      2. Read via GET /credentials/{id} (public, no decrypted data)
      3. Read with decrypted data — all 7 fields round-trip correctly
      4. Update credential_data (change host, port, ssl/tls flags)
      5. Verify update persisted via with-data endpoint
      6. Delete the credential
      7. Verify it is gone (404)
    """
    # ── Phase 1: Create ───────────────────────────────────────────────────
    response = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "My SMTP Account",
            "type": "email_smtp",
            "notes": "Company mailer",
            "credential_data": _SMTP_DATA,
        },
    )
    assert response.status_code == 200
    created = response.json()
    cred_id = created["id"]
    assert created["name"] == "My SMTP Account"
    assert created["type"] == "email_smtp"
    assert created["notes"] == "Company mailer"
    assert created["status"] == "complete"
    # Public response must not expose raw credential data
    assert "credential_data" not in created
    assert "encrypted_data" not in created

    # ── Phase 2: Read (public) ────────────────────────────────────────────
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    public = r.json()
    assert public["id"] == cred_id
    assert public["type"] == "email_smtp"
    assert "credential_data" not in public

    # ── Phase 3: Read with decrypted data — all 7 fields round-trip ──────
    with_data = get_credential_with_data(client, superuser_token_headers, cred_id)
    cdata = with_data["credential_data"]
    assert cdata["host"] == "smtp.example.com"
    assert cdata["port"] == 587
    assert cdata["username"] == "user@example.com"
    assert cdata["password"] == "super-secret-pass"
    assert cdata["from_email"] == "sender@example.com"
    assert cdata["use_tls"] is True
    assert cdata["use_ssl"] is False

    # ── Phase 4: Update credential_data ───────────────────────────────────
    r = client.put(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
        json={
            "credential_data": {
                **_SMTP_DATA,
                "host": "smtp.newserver.com",
                "port": 465,
                "use_ssl": True,
                "use_tls": False,
            }
        },
    )
    assert r.status_code == 200

    # ── Phase 5: Verify update persisted ─────────────────────────────────
    updated = get_credential_with_data(client, superuser_token_headers, cred_id)
    udata = updated["credential_data"]
    assert udata["host"] == "smtp.newserver.com"
    assert udata["port"] == 465
    assert udata["use_ssl"] is True
    assert udata["use_tls"] is False

    # ── Phase 6: Delete ───────────────────────────────────────────────────
    r = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200

    # ── Phase 7: Verify gone ──────────────────────────────────────────────
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Placeholder (incomplete credential)
# ---------------------------------------------------------------------------

def test_email_smtp_placeholder(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Creating an email_smtp credential without data results in status=incomplete."""
    response = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=superuser_token_headers,
        json={"name": "Empty SMTP", "type": "email_smtp"},
    )
    assert response.status_code == 200
    content = response.json()
    assert content["type"] == "email_smtp"
    assert content["status"] == "incomplete"


# ---------------------------------------------------------------------------
# Field whitelisting and redaction — agent environment sync
# ---------------------------------------------------------------------------

def test_email_smtp_agent_env_sync_whitelisting_and_redaction(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When an email_smtp credential is linked to an agent:
      1. Create email_smtp credential with all 7 fields
      2. Create agent (environment stub)
      3. Link credential to agent → env receives credentials via adapter
      4. Verify all 7 SMTP fields appear in credentials_json (whitelisted)
      5. Verify password is REDACTED in credentials_readme
      6. Verify non-sensitive fields (host, username, from_email) appear in readme
      7. Unlink credential → agent env updated with empty credentials
    """
    # ── Phase 1: Create email_smtp credential ─────────────────────────────
    cred_name = "My SMTP Mailer"
    cred_notes = "For outgoing email from scripts"
    cred = _create_smtp_credential(client, superuser_token_headers)
    # Update name/notes for clearer assertions
    r = client.put(
        f"{settings.API_V1_STR}/credentials/{cred['id']}",
        headers=superuser_token_headers,
        json={"name": cred_name, "notes": cred_notes},
    )
    assert r.status_code == 200
    credential_id = cred["id"]

    # ── Phase 2: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    # Wire a shared adapter to capture what the backend sends to the agent env
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 3: Link credential to agent ─────────────────────────────────
    result = link_credential_to_agent(
        client, superuser_token_headers, agent_id, credential_id
    )
    assert result["message"] == "Credential linked successfully"

    # Verify credential appears in agent's credential list
    agent_creds = get_agent_credentials(client, superuser_token_headers, agent_id)
    assert agent_creds["count"] == 1
    assert agent_creds["data"][0]["id"] == credential_id
    assert agent_creds["data"][0]["type"] == "email_smtp"

    # ── Phase 4: Verify all 7 SMTP fields in credentials_json (whitelisted) ─
    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter should have received credentials"

    creds_json = env_data["credentials_json"]
    assert len(creds_json) == 1
    entry = creds_json[0]
    assert entry["id"] == credential_id
    assert entry["name"] == cred_name
    assert entry["type"] == "email_smtp"
    assert entry["notes"] == cred_notes

    field_data = entry["credential_data"]
    # All 7 whitelisted fields must be present
    assert field_data["host"] == "smtp.example.com"
    assert field_data["port"] == 587
    assert field_data["username"] == "user@example.com"
    assert field_data["password"] == "super-secret-pass"
    assert field_data["from_email"] == "sender@example.com"
    assert field_data["use_tls"] is True
    assert field_data["use_ssl"] is False

    # ── Phase 5: Verify password is REDACTED in credentials_readme ─────────
    readme = env_data["credentials_readme"]
    assert readme, "credentials_readme should not be empty"
    assert "email_smtp" in readme
    # The raw password must NOT appear in the readme
    assert "super-secret-pass" not in readme
    assert "***REDACTED***" in readme

    # ── Phase 6: Non-sensitive fields appear in readme ─────────────────────
    assert "smtp.example.com" in readme
    assert "user@example.com" in readme
    assert "sender@example.com" in readme

    # ── Phase 7: Unlink credential → env updated ───────────────────────────
    result = unlink_credential_from_agent(
        client, superuser_token_headers, agent_id, credential_id
    )
    assert result["message"] == "Credential unlinked successfully"

    env_data_after = shared_adapter.credentials_set
    assert env_data_after["credentials_json"] == []

    # Agent's credential list should be empty
    agent_creds = get_agent_credentials(client, superuser_token_headers, agent_id)
    assert agent_creds["count"] == 0
    assert agent_creds["data"] == []


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def test_email_smtp_access_control(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    A normal user cannot read, update, or delete an email_smtp credential owned
    by the superuser. Unauthenticated requests are also rejected.
    """
    cred = _create_smtp_credential(client, superuser_token_headers)
    cred_id = cred["id"]

    # Read — should be denied for other user
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=normal_user_token_headers,
    )
    assert r.status_code == 400

    # Update — should be denied for other user
    r = client.put(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=normal_user_token_headers,
        json={"name": "Hacked"},
    )
    assert r.status_code in (400, 403, 404)

    # Delete — should be denied for other user
    r = client.delete(
        f"{settings.API_V1_STR}/credentials/{cred_id}",
        headers=normal_user_token_headers,
    )
    assert r.status_code in (400, 403, 404)

    # Unauthenticated read — should be denied
    r = client.get(f"{settings.API_V1_STR}/credentials/{cred_id}")
    assert r.status_code in (401, 403)
