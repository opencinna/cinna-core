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
