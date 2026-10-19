"""User groups admin API: CRUD, membership, guards.

Scenarios:
  1. Full lifecycle: create -> get -> list/filter -> patch -> members -> remove -> delete.
  2. Non-superusers get 403 on every admin route; unauthenticated is rejected.
  3. Duplicate names (case-insensitive) are 409 on create and rename.
  4. Membership: idempotent add, unknown/inactive ids 422 with nothing added.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.user import create_random_user_with_headers
from tests.utils.user_group import (
    add_members,
    create_group,
    delete_group,
    get_group,
    list_members,
    remove_member,
)
from tests.utils.utils import random_lower_string

ADMIN = f"{settings.API_V1_STR}/admin/user-groups"


def test_group_full_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Create group (trimmed name), creator recorded
    2. Get / list / filter by q
    3. Patch name and description; omitted fields unchanged
    4. Add two members, list them, filter members, counts update
    5. Remove one member (and a non-member -> 404)
    6. Delete group; gone everywhere
    """
    h = superuser_token_headers
    u1, _ = create_random_user_with_headers(client)
    u2, _ = create_random_user_with_headers(client)
    name = f"Team-{random_lower_string()[:8]}"

    # ── Phase 1: Create ───────────────────────────────────────────────
    r = client.post(ADMIN + "/", headers=h, json={"name": f"  {name}  ", "description": "desc"})
    assert r.status_code == 200, r.text
    group = r.json()
    gid = group["id"]
    assert group["name"] == name
    assert group["description"] == "desc"
    assert group["member_count"] == 0
    assert group["share_count"] == 0
    assert group["created_by"] is not None

    # ── Phase 2: Get / list / filter ──────────────────────────────────
    assert get_group(client, h, gid)["name"] == name
    r = client.get(ADMIN + "/", headers=h, params={"q": name.upper()})
    assert r.status_code == 200
    assert [g["id"] for g in r.json()["data"]] == [gid]
    assert r.json()["count"] == 1
    r = client.get(ADMIN + "/", headers=h, params={"q": "no-such-" + random_lower_string()})
    assert r.json()["data"] == []

    # ── Phase 3: Patch ────────────────────────────────────────────────
    r = client.patch(f"{ADMIN}/{gid}", headers=h, json={"name": name + "-x"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name + "-x"
    assert r.json()["description"] == "desc"
    r = client.patch(f"{ADMIN}/{gid}", headers=h, json={"description": "new"})
    assert r.json()["name"] == name + "-x"
    assert r.json()["description"] == "new"

    # ── Phase 4: Members ──────────────────────────────────────────────
    added = add_members(client, h, gid, [u1["id"], u2["id"]])
    assert added["count"] == 2
    assert {m["user_id"] for m in added["data"]} == {u1["id"], u2["id"]}
    assert {m["email"] for m in added["data"]} == {u1["email"], u2["email"]}
    assert all(m["is_active"] for m in added["data"])
    members = list_members(client, h, gid)
    assert members["count"] == 2
    assert {m["user_id"] for m in members["data"]} == {u1["id"], u2["id"]}
    filtered = list_members(client, h, gid, q=u1["email"])
    assert [m["user_id"] for m in filtered["data"]] == [u1["id"]]
    assert get_group(client, h, gid)["member_count"] == 2

    # ── Phase 5: Remove ───────────────────────────────────────────────
    remove_member(client, h, gid, u1["id"])
    assert [m["user_id"] for m in list_members(client, h, gid)["data"]] == [u2["id"]]
    assert get_group(client, h, gid)["member_count"] == 1
    r = client.delete(f"{ADMIN}/{gid}/members/{u1['id']}", headers=h)
    assert r.status_code == 404

    # ── Phase 6: Delete ───────────────────────────────────────────────
    delete_group(client, h, gid)
    assert client.get(f"{ADMIN}/{gid}", headers=h).status_code == 404
    assert client.delete(f"{ADMIN}/{gid}", headers=h).status_code == 404
    assert client.get(f"{ADMIN}/{gid}/members", headers=h).status_code == 404
    ids = [g["id"] for g in client.get(ADMIN + "/", headers=h).json()["data"]]
    assert gid not in ids
    # Members themselves survive the group
    assert client.get(f"{settings.API_V1_STR}/users/{u2['id']}", headers=h).status_code == 200


def test_non_superuser_forbidden_on_every_admin_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    group = create_group(client, superuser_token_headers)
    gid = group["id"]
    user, uh = create_random_user_with_headers(client)
    uid = user["id"]

    calls = [
        ("get", ADMIN + "/", None),
        ("post", ADMIN + "/", {"name": "x"}),
        ("get", f"{ADMIN}/{gid}", None),
        ("patch", f"{ADMIN}/{gid}", {"name": "y"}),
        ("delete", f"{ADMIN}/{gid}", None),
        ("get", f"{ADMIN}/{gid}/members", None),
        ("post", f"{ADMIN}/{gid}/members", {"user_ids": [uid]}),
        ("delete", f"{ADMIN}/{gid}/members/{uid}", None),
    ]
    for method, url, body in calls:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(client, method)(url, headers=uh, **kwargs).status_code == 403, (method, url)
        anon = getattr(client, method)(url, **kwargs)
        assert anon.status_code in (401, 403), (method, url)

    # Nothing changed
    assert get_group(client, superuser_token_headers, gid)["name"] == group["name"]
    assert get_group(client, superuser_token_headers, gid)["member_count"] == 0


def test_duplicate_names_are_409_case_insensitive(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    name = f"Dup-{random_lower_string()[:8]}"
    first = create_group(client, h, name=name)
    other = create_group(client, h)

    r = client.post(ADMIN + "/", headers=h, json={"name": name})
    assert r.status_code == 409
    r = client.post(ADMIN + "/", headers=h, json={"name": name.upper()})
    assert r.status_code == 409
    r = client.post(ADMIN + "/", headers=h, json={"name": f"  {name.lower()}  "})
    assert r.status_code == 409

    # Rename onto another group's name -> 409; renaming to own name in other case is fine
    r = client.patch(f"{ADMIN}/{other['id']}", headers=h, json={"name": name.upper()})
    assert r.status_code == 409
    r = client.patch(f"{ADMIN}/{first['id']}", headers=h, json={"name": name.upper()})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name.upper()

    # Validation: empty / blank / overlong names
    assert client.post(ADMIN + "/", headers=h, json={"name": ""}).status_code == 422
    assert client.post(ADMIN + "/", headers=h, json={"name": "   "}).status_code == 422
    assert client.post(ADMIN + "/", headers=h, json={"name": "a" * 101}).status_code == 422
    assert client.post(
        ADMIN + "/", headers=h, json={"name": "ok", "description": "d" * 501}
    ).status_code == 422
    assert client.get(f"{ADMIN}/{uuid.uuid4()}", headers=h).status_code == 404
    assert client.patch(f"{ADMIN}/{uuid.uuid4()}", headers=h, json={"name": "z"}).status_code == 404


def test_add_members_idempotent_and_rejects_unknown_or_inactive(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    group = create_group(client, h)
    gid = group["id"]
    u1, _ = create_random_user_with_headers(client)
    u2, _ = create_random_user_with_headers(client)
    inactive, _ = create_random_user_with_headers(client)
    r = client.patch(
        f"{settings.API_V1_STR}/users/{inactive['id']}", headers=h, json={"is_active": False}
    )
    assert r.status_code == 200, r.text

    # Idempotent: second add returns only the new rows; duplicates in one request collapse
    assert add_members(client, h, gid, [u1["id"], u1["id"]])["count"] == 1
    again = add_members(client, h, gid, [u1["id"], u2["id"]])
    assert [m["user_id"] for m in again["data"]] == [u2["id"]]
    assert add_members(client, h, gid, [u1["id"], u2["id"]]) == {"data": [], "count": 0}
    assert list_members(client, h, gid)["count"] == 2

    # Unknown and inactive ids -> 422 naming them, nothing added (even the valid one)
    fresh, _ = create_random_user_with_headers(client)
    ghost = str(uuid.uuid4())
    r = client.post(
        f"{ADMIN}/{gid}/members", headers=h,
        json={"user_ids": [fresh["id"], ghost, inactive["id"]]},
    )
    assert r.status_code == 422
    assert ghost in r.text and inactive["id"] in r.text
    assert list_members(client, h, gid)["count"] == 2

    # Body validation
    assert client.post(f"{ADMIN}/{gid}/members", headers=h, json={"user_ids": []}).status_code == 422
    too_many = [str(uuid.uuid4()) for _ in range(101)]
    assert client.post(
        f"{ADMIN}/{gid}/members", headers=h, json={"user_ids": too_many}
    ).status_code == 422
    assert client.post(
        f"{ADMIN}/{uuid.uuid4()}/members", headers=h, json={"user_ids": [u1["id"]]}
    ).status_code == 404
