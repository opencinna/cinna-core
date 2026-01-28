from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string


def create_random_workspace(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
    icon: str | None = None,
) -> dict:
    """Create a random workspace via the API and return the response data."""
    if name is None:
        name = f"workspace-{random_lower_string()[:12]}"

    data = {"name": name}
    if icon is not None:
        data["icon"] = icon

    r = client.post(
        f"{settings.API_V1_STR}/user-workspaces/",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200
    return r.json()
