"""
Agent-API group scope grants (user groups -> producer scopes).

Scenarios:
  1. Owner CRUD on ``/agent-api/group-grants`` (create, duplicate 409, unknown
     group 404, update, delete) and the access list ``groups`` entry.
  2. Owner gating: non-owner 404 on every verb, superuser allowed.
  3. Effective scopes (observed through the recipient's holder info, which
     uses the same resolution as the proxy): personal U group, overlap
     de-duplicated, empty personal grant + group, live membership removal,
     inactive member gets nothing.
  4. Group delete cascades the scope grants; producer delete removes them too.
  5. Audit: created / updated / deleted events carry ``granted_group_id``.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.mfa import find_security_events
from tests.utils.user import create_random_user_with_headers
from tests.utils.user_group import (
    add_members,
    create_group,
    delete_group,
    list_shares,
    remove_member,
)

API = settings.API_V1_STR


def _base(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/agent-api"


def _gg_url(agent_id: str, grant_id: str | None = None) -> str:
    url = f"{_base(agent_id)}/group-grants"
    return f"{url}/{grant_id}" if grant_id else url


def _create_gg(client, headers, agent_id, group_id, scopes=None, expected=200):
    body: dict = {"group_id": group_id}
    if scopes is not None:
        body["scopes"] = scopes
    r = client.post(_gg_url(agent_id), headers=headers, json=body)
    assert r.status_code == expected, r.text
    return r.json()


def _list_gg(client, headers, agent_id) -> list[dict]:
    r = client.get(_gg_url(agent_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _issue_key_to(client, issuer_headers, producer_id, recipient_id) -> str:
    """Issue a key to a recipient; return its credential id."""
    r = client.post(
        f"{_base(producer_id)}/keys",
        headers=issuer_headers,
        json={"issued_to_user_id": recipient_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["credential_id"]


def _holder_scopes(client, recipient_headers, credential_id) -> list[str]:
    r = client.get(f"{API}/credentials/{credential_id}/agent-api-key", headers=recipient_headers)
    assert r.status_code == 200, r.text
    return r.json()["scopes"]


def _owner_and_producer(client, su, name="Group Grant Producer"):
    owner, owner_h = make_developer(client, su)
    producer = setup_producer(client, owner_h, name=name, identity_enabled=True)
    return owner, owner_h, producer


def _recipient(client, su, owner_h, producer_id):
    user, headers = make_developer(client, su)
    cred_id = _issue_key_to(client, owner_h, producer_id, user["id"])
    return user, headers, cred_id


def test_owner_group_grant_crud_and_access_list(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    member, _ = create_random_user_with_headers(client)
    group = create_group(client, su, member_ids=[member["id"]])

    assert _list_gg(client, owner_h, pid) == []

    grant = _create_gg(client, owner_h, pid, group["id"], ["orders.read", " orders.read", "a b"])
    assert grant["group_id"] == group["id"]
    assert grant["producer_agent_id"] == pid
    assert grant["scopes"] == ["orders.read"]
    assert grant["member_count"] == 1
    assert grant["group"]["name"] == group["name"]

    # duplicate -> 409, unknown group -> 404
    r = client.post(_gg_url(pid), headers=owner_h, json={"group_id": group["id"]})
    assert r.status_code == 409, r.text
    r = client.post(_gg_url(pid), headers=owner_h, json={"group_id": str(uuid.uuid4())})
    assert r.status_code == 404, r.text

    # access list carries the group
    r = client.get(f"{_base(pid)}/access", headers=owner_h)
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["grant_id"] == grant["id"]
    assert groups[0]["group_id"] == group["id"]
    assert groups[0]["name"] == group["name"]
    assert groups[0]["member_count"] == 1
    assert groups[0]["scopes"] == ["orders.read"]

    # update
    r = client.put(_gg_url(pid, grant["id"]), headers=owner_h, json={"scopes": ["x", "y"]})
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["x", "y"]
    assert _list_gg(client, owner_h, pid)[0]["scopes"] == ["x", "y"]

    # ghost grant id
    ghost = str(uuid.uuid4())
    assert client.put(_gg_url(pid, ghost), headers=owner_h, json={"scopes": []}).status_code == 404
    assert client.delete(_gg_url(pid, ghost), headers=owner_h).status_code == 404

    # delete
    assert client.delete(_gg_url(pid, grant["id"]), headers=owner_h).status_code == 200
    assert _list_gg(client, owner_h, pid) == []
    assert client.get(f"{_base(pid)}/access", headers=owner_h).json()["groups"] == []


def test_group_grant_routes_are_owner_gated_superuser_allowed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    _, stranger_h = make_developer(client, su)
    g1, g2 = create_group(client, su), create_group(client, su)
    grant = _create_gg(client, owner_h, pid, g1["id"], ["a"])

    assert client.get(_gg_url(pid), headers=stranger_h).status_code == 404
    r = client.post(_gg_url(pid), headers=stranger_h, json={"group_id": g2["id"]})
    assert r.status_code == 404, r.text
    r = client.put(_gg_url(pid, grant["id"]), headers=stranger_h, json={"scopes": ["z"]})
    assert r.status_code == 404, r.text
    assert client.delete(_gg_url(pid, grant["id"]), headers=stranger_h).status_code == 404
    assert _list_gg(client, owner_h, pid)[0]["scopes"] == ["a"]

    # superuser may do every verb
    assert len(_list_gg(client, su, pid)) == 1
    g3 = _create_gg(client, su, pid, g2["id"], ["b"])
    r = client.put(_gg_url(pid, g3["id"]), headers=su, json={"scopes": ["c"]})
    assert r.status_code == 200 and r.json()["scopes"] == ["c"]
    assert client.delete(_gg_url(pid, g3["id"]), headers=su).status_code == 200


def test_effective_scopes_union_dedup_and_empty_personal_grant(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    user, user_h, cred_id = _recipient(client, su, owner_h, pid)
    assert _holder_scopes(client, user_h, cred_id) == []

    # explicit empty personal grant + group [b] -> [b]
    r = client.post(f"{_base(pid)}/grants", headers=owner_h,
                    json={"user_id": user["id"], "scopes": []})
    assert r.status_code == 200, r.text
    personal = r.json()
    g1 = create_group(client, su, member_ids=[user["id"]])
    _create_gg(client, owner_h, pid, g1["id"], ["b"])
    assert _holder_scopes(client, user_h, cred_id) == ["b"]

    # personal [a] + group [b] -> {a, b}
    r = client.put(f"{_base(pid)}/grants/{personal['id']}", headers=owner_h,
                   json={"scopes": ["a"]})
    assert r.status_code == 200, r.text
    assert sorted(_holder_scopes(client, user_h, cred_id)) == ["a", "b"]

    # a second group overlapping -> de-duplicated
    g2 = create_group(client, su, member_ids=[user["id"]])
    _create_gg(client, owner_h, pid, g2["id"], ["b", "c", "a"])
    scopes = _holder_scopes(client, user_h, cred_id)
    assert len(scopes) == len(set(scopes))
    assert sorted(scopes) == ["a", "b", "c"]


def test_membership_removal_drops_group_scopes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    user, user_h, cred_id = _recipient(client, su, owner_h, pid)
    group = create_group(client, su)
    _create_gg(client, owner_h, pid, group["id"], ["g.read"])
    assert _holder_scopes(client, user_h, cred_id) == []  # not a member yet

    add_members(client, su, group["id"], [user["id"]])
    assert _holder_scopes(client, user_h, cred_id) == ["g.read"]

    remove_member(client, su, group["id"], user["id"])
    assert _holder_scopes(client, user_h, cred_id) == []


def test_inactive_member_gets_no_group_scopes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    user, user_h, cred_id = _recipient(client, su, owner_h, pid)
    group = create_group(client, su, member_ids=[user["id"]])
    _create_gg(client, owner_h, pid, group["id"], ["g.read"])
    assert _holder_scopes(client, user_h, cred_id) == ["g.read"]

    r = client.patch(f"{API}/users/{user['id']}", headers=su, json={"is_active": False})
    assert r.status_code == 200, r.text
    # the group no longer counts the inactive member
    r = client.get(_gg_url(pid), headers=owner_h)
    assert r.json()["data"][0]["member_count"] == 0

    r = client.patch(f"{API}/users/{user['id']}", headers=su, json={"is_active": True})
    assert r.status_code == 200, r.text
    assert _holder_scopes(client, user_h, cred_id) == ["g.read"]


def test_group_delete_cascades_scope_grants_and_producer_delete_removes_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    user, user_h, cred_id = _recipient(client, su, owner_h, pid)
    doomed = create_group(client, su, member_ids=[user["id"]])
    kept = create_group(client, su, member_ids=[user["id"]])
    _create_gg(client, owner_h, pid, doomed["id"], ["d"])
    _create_gg(client, owner_h, pid, kept["id"], ["k"])
    assert sorted(_holder_scopes(client, user_h, cred_id)) == ["d", "k"]

    delete_group(client, su, doomed["id"])
    rows = _list_gg(client, owner_h, pid)
    assert [r["group_id"] for r in rows] == [kept["id"]]
    assert _holder_scopes(client, user_h, cred_id) == ["k"]

    # deleting the producer removes the remaining grant (visible on the group page)
    assert [s["resource_type"] for s in list_shares(client, su, kept["id"])["data"]] == [
        "agent_api_scopes"
    ]
    r = client.delete(f"{API}/agents/{pid}", headers=owner_h)
    assert r.status_code == 200, r.text
    assert list_shares(client, su, kept["id"]) == {"data": [], "count": 0}


def test_group_grant_audit_events_carry_granted_group_id(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner, owner_h, producer = _owner_and_producer(client, su)
    pid = producer["id"]
    group = create_group(client, su)
    grant = _create_gg(client, owner_h, pid, group["id"], ["a"])
    client.put(_gg_url(pid, grant["id"]), headers=owner_h, json={"scopes": ["b"]})
    client.delete(_gg_url(pid, grant["id"]), headers=owner_h)

    for event_type in (
        "AGENT_API_GRANT_CREATED", "AGENT_API_GRANT_UPDATED", "AGENT_API_GRANT_DELETED",
    ):
        events = [
            e for e in find_security_events(client, owner_h, event_type)
            if (e.get("details") or {}).get("granted_group_id") == group["id"]
        ]
        assert len(events) == 1, (event_type, events)
        assert events[0]["details"]["grant_id"] == grant["id"]
