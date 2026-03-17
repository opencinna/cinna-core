from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


def create_random_credential(
    client: TestClient,
    token_headers: dict[str, str],
    credential_type: str = "email_imap",
    credential_data: dict | None = None,
) -> dict:
    """Create a random credential via the API and return the response data."""
    name = f"test-cred-{random_lower_string()[:12]}"

    if credential_data is None:
        credential_data = _default_credential_data(credential_type)

    data = {
        "name": name,
        "type": credential_type,
        "notes": "Test credential",
        "credential_data": credential_data,
    }
    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()


def _default_credential_data(credential_type: str) -> dict:
    """Return default test data for a given credential type."""
    if credential_type == "email_imap":
        return {
            "host": "imap.example.com",
            "port": 993,
            "login": "test@example.com",
            "password": "test-password-123",
            "is_ssl": True,
        }
    elif credential_type == "email_smtp":
        return {
            "host": "smtp.example.com",
            "port": 587,
            "username": "test@example.com",
            "password": "test-smtp-password-456",
            "from_email": "sender@example.com",
            "use_tls": True,
            "use_ssl": False,
        }
    elif credential_type == "odoo":
        return {
            "url": "https://odoo.example.com",
            "database_name": "test_db",
            "login": "admin",
            "api_token": "test-api-token-456",
        }
    elif credential_type == "api_token":
        return {
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": "sk-test-token-789",
        }
    elif credential_type == "google_service_account":
        return {
            "type": "service_account",
            "project_id": "test-project-123",
            "private_key_id": "key-id-abc",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEtest\n-----END RSA PRIVATE KEY-----\n",
            "client_email": "test-sa@test-project-123.iam.gserviceaccount.com",
            "client_id": "123456789",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    elif credential_type in (
        "gmail_oauth",
        "gmail_oauth_readonly",
        "gdrive_oauth",
        "gdrive_oauth_readonly",
        "gcalendar_oauth",
        "gcalendar_oauth_readonly",
    ):
        return {
            "access_token": "ya29.test-access-token",
            "refresh_token": "1//test-refresh-token",
            "token_type": "Bearer",
            "expires_at": 9999999999,
            "scope": "https://www.googleapis.com/auth/gmail.modify",
        }
    else:
        return {}


def update_credential(
    client: TestClient,
    token_headers: dict[str, str],
    credential_id: str,
    **fields,
) -> dict:
    """Update credential via PUT /credentials/{id}. Pass fields as kwargs."""
    r = client.put(
        f"{settings.API_V1_STR}/credentials/{credential_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200
    return r.json()


def get_credential_with_data(
    client: TestClient,
    token_headers: dict[str, str],
    credential_id: str,
) -> dict:
    """Get credential with decrypted data via GET /credentials/{id}/with-data."""
    r = client.get(
        f"{settings.API_V1_STR}/credentials/{credential_id}/with-data",
        headers=token_headers,
    )
    assert r.status_code == 200
    return r.json()


def link_credential_to_agent(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    credential_id: str,
) -> dict:
    """Link a credential to an agent via POST /agents/{id}/credentials."""
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/credentials",
        headers=token_headers,
        json={"credential_id": credential_id},
    )
    assert r.status_code == 200
    return r.json()


def unlink_credential_from_agent(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    credential_id: str,
) -> dict:
    """Unlink a credential from an agent via DELETE /agents/{id}/credentials/{credential_id}."""
    r = client.delete(
        f"{settings.API_V1_STR}/agents/{agent_id}/credentials/{credential_id}",
        headers=token_headers,
    )
    assert r.status_code == 200
    return r.json()


def get_agent_credentials(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Get all credentials linked to an agent via GET /agents/{id}/credentials."""
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/credentials",
        headers=token_headers,
    )
    assert r.status_code == 200
    return r.json()
