from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


def create_random_ai_credential(
    client: TestClient,
    token_headers: dict[str, str],
    credential_type: str = "anthropic",
    api_key: str | None = None,
    name: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict:
    """Create a random AI credential via the API and return the response data."""
    name = name or f"test-ai-cred-{random_lower_string()[:12]}"
    api_key = api_key or f"sk-ant-api03-{random_lower_string()}"

    data: dict = {
        "name": name,
        "type": credential_type,
        "api_key": api_key,
    }
    if base_url is not None:
        data["base_url"] = base_url
    if model is not None:
        data["model"] = model

    r = client.post(
        f"{settings.API_V1_STR}/ai-credentials/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()
