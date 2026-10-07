"""GET /users/{id} — who may read which user, and what a missing id returns.

Covered here:

  1. A user reads themself → 200
  2. A non-superuser reading another user → 403
  3. A non-superuser reading an id that does not exist → 403 (same answer as an
     existing user, so the route never reveals whether an id exists)
  4. A superuser reading an id that does not exist (e.g. a deleted user) → 404
     — this used to crash with a 500
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.user import create_random_user_with_headers

NEEDS_AGENT_STUBS = False

API = settings.API_V1_STR


def test_read_user_by_id_access_and_missing_ids(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user, headers = create_random_user_with_headers(client)
    other, _ = create_random_user_with_headers(client)

    # 1. Self
    r = client.get(f"{API}/users/{user['id']}", headers=headers)
    assert r.status_code == 200
    assert r.json()["id"] == user["id"]

    # 2. Another existing user, as a non-superuser
    r = client.get(f"{API}/users/{other['id']}", headers=headers)
    assert r.status_code == 403

    # 3. A missing id, as a non-superuser — indistinguishable from case 2
    missing = uuid.uuid4()
    r = client.get(f"{API}/users/{missing}", headers=headers)
    assert r.status_code == 403

    # 4. A deleted user, as a superuser
    r = client.delete(f"{API}/users/me", headers=headers)
    assert r.status_code == 200
    r = client.get(f"{API}/users/{user['id']}", headers=superuser_token_headers)
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"
