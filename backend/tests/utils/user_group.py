"""HTTP helpers for User Groups (admin CRUD/membership + share-picker search).

Generic on purpose: later phases (catalog, credentials, artifacts, knowledge)
reuse them to build groups before sharing with them.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string

_ADMIN = f"{settings.API_V1_STR}/admin/user-groups"


def create_group(
    client: TestClient,
    superuser_headers: dict[str, str],
    name: str | None = None,
    description: str | None = None,
    member_ids: list[str] | None = None,
) -> dict:
    """POST a group (random name by default); optionally add members. Returns the group."""
    body: dict = {"name": name or f"grp-{random_lower_string()[:12]}"}
    if description is not None:
        body["description"] = description
    r = client.post(f"{_ADMIN}/", headers=superuser_headers, json=body)
    assert r.status_code == 200, r.text
    group = r.json()
    if member_ids:
        add_members(client, superuser_headers, group["id"], member_ids)
        r = client.get(f"{_ADMIN}/{group['id']}", headers=superuser_headers)
        assert r.status_code == 200, r.text
        group = r.json()
    return group


def add_members(
    client: TestClient,
    superuser_headers: dict[str, str],
    group_id: str,
    user_ids: list[str],
) -> dict:
    """POST members; returns UserGroupMembersPublic (only rows actually added)."""
    r = client.post(
        f"{_ADMIN}/{group_id}/members",
        headers=superuser_headers,
        json={"user_ids": user_ids},
    )
    assert r.status_code == 200, r.text
    return r.json()


def list_members(
    client: TestClient, superuser_headers: dict[str, str], group_id: str, **params
) -> dict:
    r = client.get(f"{_ADMIN}/{group_id}/members", headers=superuser_headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def remove_member(
    client: TestClient,
    superuser_headers: dict[str, str],
    group_id: str,
    user_id: str,
) -> dict:
    r = client.delete(f"{_ADMIN}/{group_id}/members/{user_id}", headers=superuser_headers)
    assert r.status_code == 200, r.text
    return r.json()


def get_group(client: TestClient, superuser_headers: dict[str, str], group_id: str) -> dict:
    r = client.get(f"{_ADMIN}/{group_id}", headers=superuser_headers)
    assert r.status_code == 200, r.text
    return r.json()


def delete_group(client: TestClient, superuser_headers: dict[str, str], group_id: str) -> dict:
    r = client.delete(f"{_ADMIN}/{group_id}", headers=superuser_headers)
    assert r.status_code == 200, r.text
    return r.json()


def search_groups(
    client: TestClient, headers: dict[str, str], q: str | None = None, limit: int | None = None
) -> list[dict]:
    """GET /user-groups/search as any authenticated user; returns the ``data`` list."""
    params: dict = {}
    if q is not None:
        params["q"] = q
    if limit is not None:
        params["limit"] = limit
    r = client.get(f"{settings.API_V1_STR}/user-groups/search", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ── Group page: shares, revoke, impact (P5) ─────────────────────────


def list_shares(client: TestClient, superuser_headers: dict[str, str], group_id: str) -> dict:
    """GET /admin/user-groups/{id}/shares; returns ``{data, count}``."""
    r = client.get(f"{_ADMIN}/{group_id}/shares", headers=superuser_headers)
    assert r.status_code == 200, r.text
    return r.json()


def revoke_share(
    client: TestClient,
    superuser_headers: dict[str, str],
    group_id: str,
    resource_type: str,
    resource_id: str,
):
    """DELETE a share; returns the raw response so callers assert the status."""
    return client.delete(
        f"{_ADMIN}/{group_id}/shares/{resource_type}/{resource_id}",
        headers=superuser_headers,
    )


def share_revoke_impact(
    client: TestClient,
    superuser_headers: dict[str, str],
    group_id: str,
    resource_type: str,
    resource_id: str,
) -> dict:
    r = client.get(
        f"{_ADMIN}/{group_id}/shares/{resource_type}/{resource_id}/revoke-impact",
        headers=superuser_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def delete_impact(client: TestClient, superuser_headers: dict[str, str], group_id: str) -> dict:
    r = client.get(f"{_ADMIN}/{group_id}/delete-impact", headers=superuser_headers)
    assert r.status_code == 200, r.text
    return r.json()


def removal_impact(
    client: TestClient, superuser_headers: dict[str, str], group_id: str, user_id: str
) -> dict:
    r = client.get(
        f"{_ADMIN}/{group_id}/members/{user_id}/removal-impact", headers=superuser_headers
    )
    assert r.status_code == 200, r.text
    return r.json()
