from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string

_BASE = f"{settings.API_V1_STR}/knowledge-sources"


def create_knowledge_source(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
    git_url: str | None = None,
    branch: str = "main",
    description: str | None = None,
) -> dict:
    """Create a knowledge source via POST and return the response data."""
    name = name or f"test-ks-{random_lower_string()[:16]}"
    git_url = git_url or f"https://github.com/test/{random_lower_string()[:12]}.git"
    data: dict = {"name": name, "git_url": git_url, "branch": branch}
    if description is not None:
        data["description"] = description
    r = client.post(_BASE + "/", headers=token_headers, json=data)
    assert r.status_code == 200, r.text
    return r.json()


def get_knowledge_source(
    client: TestClient,
    token_headers: dict[str, str],
    source_id: str,
) -> dict:
    """GET /knowledge-sources/{id} and return the response data."""
    r = client.get(f"{_BASE}/{source_id}", headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()


def list_knowledge_sources(
    client: TestClient,
    token_headers: dict[str, str],
) -> list:
    """GET /knowledge-sources/ and return the list of sources."""
    r = client.get(_BASE + "/", headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()


def update_knowledge_source(
    client: TestClient,
    token_headers: dict[str, str],
    source_id: str,
    **kwargs,
) -> dict:
    """PUT /knowledge-sources/{id} and return the updated source data."""
    r = client.put(f"{_BASE}/{source_id}", headers=token_headers, json=kwargs)
    assert r.status_code == 200, r.text
    return r.json()


def delete_knowledge_source(
    client: TestClient,
    token_headers: dict[str, str],
    source_id: str,
) -> None:
    """DELETE /knowledge-sources/{id} and assert success."""
    r = client.delete(f"{_BASE}/{source_id}", headers=token_headers)
    assert r.status_code == 200, r.text


def enable_knowledge_source(
    client: TestClient,
    token_headers: dict[str, str],
    source_id: str,
) -> dict:
    """POST /knowledge-sources/{id}/enable and return the updated source data."""
    r = client.post(f"{_BASE}/{source_id}/enable", headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()


def disable_knowledge_source(
    client: TestClient,
    token_headers: dict[str, str],
    source_id: str,
) -> dict:
    """POST /knowledge-sources/{id}/disable and return the updated source data."""
    r = client.post(f"{_BASE}/{source_id}/disable", headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()
