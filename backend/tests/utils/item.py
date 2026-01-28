from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


def create_random_item(client: TestClient, token_headers: dict[str, str]) -> dict:
    """Create a random item via the API and return the response data."""
    title = random_lower_string()
    description = random_lower_string()
    data = {"title": title, "description": description}
    r = client.post(
        f"{settings.API_V1_STR}/items/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()
