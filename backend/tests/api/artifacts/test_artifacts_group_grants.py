"""User-group grants on artifacts (User Groups P4).

Scenarios:
  1. Grant validation: a role principal is 422, an unknown group 400, a
     non-owner 404; PUT returns user rows then group rows with name and
     member_count; ``grant_count`` counts shares (user + group).
  2. A group member sees the artifact (shared_with_me, detail, view, content);
     a non-member gets 404; removal from the group kills the live content cap on
     its next request; a newly added member gains access.
  3. Direct and group grants coexist: dropping one keeps the other.
  4. Revoking the group grant (full replace) kills members' access.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import (
    build_zip,
    entry_url,
    get_artifact,
    list_artifacts,
    publish,
    view,
)
from tests.utils.user import create_random_user_with_headers
from tests.utils.user_group import add_members, create_group, remove_member

API = settings.API_V1_STR


def _site() -> bytes:
    return build_zip({"index.html": "<p>hi</p>"})


def _put(client, headers, artifact_id, principals: list[dict], *, expect: int = 200):
    body = [{"principal": p, "access": "view"} for p in principals]
    r = client.put(f"{API}/artifacts/{artifact_id}/grants", headers=headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _shared_ids(client, headers) -> list[str]:
    return [x["id"] for x in list_artifacts(client, headers, scope="shared_with_me")["data"]]


def test_group_grant_validation_and_listing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner = create_random_user_with_headers(client)
    friend, _ = create_random_user_with_headers(client)
    member, _ = create_random_user_with_headers(client)
    _, stranger = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    group = create_group(client, su, member_ids=[member["id"]])

    # role principal still refused; unknown group 400; non-owner 404
    _put(client, owner, a["id"], [{"role": "agent-developer"}], expect=422)
    _put(client, owner, a["id"], [{"group_id": str(uuid.uuid4())}], expect=400)
    _put(client, stranger, a["id"], [{"group_id": group["id"]}], expect=404)
    assert get_artifact(client, owner, a["id"])["grant_count"] == 0

    grants = _put(
        client, owner, a["id"],
        [{"group_id": group["id"]}, {"user_id": friend["id"]}],
    )
    assert [("user_id" in g["principal"], "group_id" in g["principal"]) for g in grants] == [
        (True, False),
        (False, True),
    ]
    g = grants[1]
    assert g["principal"]["group_id"] == group["id"]
    assert g["display_name"] == group["name"]
    assert g["member_count"] == 1
    assert g["email"] is None
    assert g["access"] == "view"

    detail_grants = get_artifact(client, owner, a["id"])["grants"]
    assert [("user_id" in x["principal"]) for x in detail_grants] == [True, False]
    assert detail_grants[1]["display_name"] == group["name"]
    assert get_artifact(client, owner, a["id"])["grant_count"] == 2
    owned = list_artifacts(client, owner, scope="mine")["data"]
    assert [x["grant_count"] for x in owned if x["id"] == a["id"]] == [2]


def test_group_member_access_follows_membership(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner = create_random_user_with_headers(client)
    member, member_h = create_random_user_with_headers(client)
    late, late_h = create_random_user_with_headers(client)
    _, outsider = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    group = create_group(client, su, member_ids=[member["id"]])

    _put(client, owner, a["id"], [{"group_id": group["id"]}])

    assert _shared_ids(client, member_h) == [a["id"]]
    detail = get_artifact(client, member_h, a["id"])
    assert detail["grants"] is None
    member_view = view(client, member_h, a["id"])
    assert client.get(entry_url(member_view)).status_code == 200
    assert _shared_ids(client, outsider) == []
    get_artifact(client, outsider, a["id"], expect=404)
    view(client, outsider, a["id"], expect=404)
    get_artifact(client, late_h, a["id"], expect=404)

    # A member added later gains access at once
    add_members(client, su, group["id"], [late["id"]])
    assert _shared_ids(client, late_h) == [a["id"]]

    # Removal kills the live content cap on its next request
    remove_member(client, su, group["id"], member["id"])
    assert client.get(entry_url(member_view)).status_code == 404
    get_artifact(client, member_h, a["id"], expect=404)
    assert _shared_ids(client, member_h) == []
    assert _shared_ids(client, late_h) == [a["id"]]


def test_direct_and_group_grants_coexist(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner = create_random_user_with_headers(client)
    user, user_h = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    group = create_group(client, su, member_ids=[user["id"]])

    _put(client, owner, a["id"], [{"user_id": user["id"]}, {"group_id": group["id"]}])
    assert _shared_ids(client, user_h) == [a["id"]]

    # drop the direct grant: group path remains
    _put(client, owner, a["id"], [{"group_id": group["id"]}])
    assert _shared_ids(client, user_h) == [a["id"]]
    get_artifact(client, user_h, a["id"])

    # direct only, then the group membership going away changes nothing
    _put(client, owner, a["id"], [{"user_id": user["id"]}, {"group_id": group["id"]}])
    _put(client, owner, a["id"], [{"user_id": user["id"]}])
    remove_member(client, su, group["id"], user["id"])
    assert _shared_ids(client, user_h) == [a["id"]]

    # full replace with nothing: access gone
    assert _put(client, owner, a["id"], []) == []
    get_artifact(client, user_h, a["id"], expect=404)


def test_revoking_group_grant_removes_member_access(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner = create_random_user_with_headers(client)
    member, member_h = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    group = create_group(client, su, member_ids=[member["id"]])

    _put(client, owner, a["id"], [{"group_id": group["id"]}])
    live = view(client, member_h, a["id"])
    assert client.get(entry_url(live)).status_code == 200

    _put(client, owner, a["id"], [])
    assert client.get(entry_url(live)).status_code == 404
    assert _shared_ids(client, member_h) == []
