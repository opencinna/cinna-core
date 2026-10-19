"""Groups in the Bundle Permissions management card.

Endpoints:
  - ``GET  /agents/{id}/bundle-permissions-overview``  -> ``groups`` list
  - ``POST /agents/{id}/bundle-permissions/group-grants``
  - ``DELETE /agents/{id}/bundle-permissions/group-grants/{grant_id}``

Scenarios:
  1. Cross-surface: a group granted via the catalog ``PUT /items/{uuid}/grants``
     shows in the overview (name-sorted, active members only), and
     ``groups`` is empty unless visibility == "users".
  2. Add via the card -> visible in the catalog grants, the member can open and
     install the bundle, an outsider cannot; the card always grants use (an
     extra access key is ignored); revoke -> gone from both surfaces.
  3. Error codes: 404 unknown group, 409 duplicate, DELETE 404
     for a person grant id / another bundle's grant / unknown id.
  4. Publisher-only: a non-publisher (even a member of the group) is refused
     on the overview and both writes; a plain non-developer gets 403.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    bundle_catalog_uuids,
    get_item_grants,
    make_person,
    publish_agent_bundle,
    put_item_grants,
    set_bundle_visibility,
    user_grant,
)
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_api_keys import setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import install_bundle, publish_bundle
from tests.utils.credential import (
    get_credential_with_data,
    link_credential_to_agent,
    set_credential_sharing,
    share_credential_via_api,
)
from tests.utils.user_group import add_members, create_group, remove_member

API = settings.API_V1_STR


def _overview(client: TestClient, headers: dict, agent_id: str, expected: int = 200):
    r = client.get(f"{API}/agents/{agent_id}/bundle-permissions-overview", headers=headers)
    assert r.status_code == expected, r.text
    return r.json()


def _add(client: TestClient, headers: dict, agent_id: str, body: dict):
    return client.post(
        f"{API}/agents/{agent_id}/bundle-permissions/group-grants", headers=headers, json=body
    )


def _revoke(client: TestClient, headers: dict, agent_id: str, grant_id: str):
    return client.delete(
        f"{API}/agents/{agent_id}/bundle-permissions/group-grants/{grant_id}", headers=headers
    )


def _group_grants(client: TestClient, headers: dict, b_uuid: str) -> list[dict]:
    return [
        g for g in get_item_grants(client, headers, b_uuid) if "group_id" in g["principal"]
    ]


def test_overview_lists_catalog_granted_groups_sorted_active_members_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    m1, m2, m3 = (make_person(client, su) for _ in range(3))
    g_b = create_group(client, su, name=f"zz-{uuid.uuid4().hex[:6]}", member_ids=[m1.id, m2.id])
    g_a = create_group(client, su, name=f"aa-{uuid.uuid4().hex[:6]}", member_ids=[m3.id])
    bundle = publish_agent_bundle(client, publisher.headers, "grp-card-overview")
    b_uuid, agent_id = bundle["bundle_uuid"], bundle["agent_id"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")

    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        {"principal": {"group_id": g_b["id"]}, "access": "edit"},
        {"principal": {"group_id": g_a["id"]}, "access": "use"},
    ])

    groups = _overview(client, publisher.headers, agent_id)["groups"]
    assert [g["name"] for g in groups] == sorted([g_a["name"], g_b["name"]])
    by_id = {g["group_id"]: g for g in groups}
    # the card carries no level: an existing edit group grant still lists
    assert all("access" not in g for g in groups)
    assert by_id[g_b["id"]]["member_count"] == 2
    assert by_id[g_a["id"]]["member_count"] == 1
    assert all(g["bundle_grant_id"] for g in groups)

    # active members only: deactivating a member drops the count
    r = client.patch(f"{API}/users/{m1.id}", headers=su, json={"is_active": False})
    assert r.status_code == 200, r.text
    groups = _overview(client, publisher.headers, agent_id)["groups"]
    assert {g["group_id"]: g["member_count"] for g in groups}[g_b["id"]] == 1

    # visibility != users -> groups empty
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="private")
    assert _overview(client, publisher.headers, agent_id)["groups"] == []


def test_card_add_edit_and_revoke_group_grant_is_consistent_with_catalog(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    bundle = publish_agent_bundle(client, publisher.headers, "grp-card-add")
    b_uuid, b_id, agent_id = bundle["bundle_uuid"], bundle["bundle_id"], bundle["agent_id"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    assert b_uuid not in bundle_catalog_uuids(client, member.headers)

    # the card always grants use
    r = _add(client, publisher.headers, agent_id, {"group_id": group["id"]})
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["group_id"] == group["id"]
    assert row["name"] == group["name"]
    assert "access" not in row
    assert row["member_count"] == 1

    cat = _group_grants(client, publisher.headers, b_uuid)
    assert len(cat) == 1
    assert cat[0]["principal"]["group_id"] == group["id"]
    assert cat[0]["access"] == "use"
    assert [
        g["bundle_grant_id"] for g in _overview(client, publisher.headers, agent_id)["groups"]
    ] == [row["bundle_grant_id"]]

    # member sees and installs; outsider cannot
    assert b_uuid in bundle_catalog_uuids(client, member.headers)
    assert b_uuid not in bundle_catalog_uuids(client, outsider.headers)
    install_bundle(client, outsider.headers, b_id, expected_status=403)
    assert install_bundle(client, member.headers, b_id)["id"]

    # revoke, then re-add: an extra access key is ignored, the grant is use
    assert _revoke(client, publisher.headers, agent_id, row["bundle_grant_id"]).status_code == 204
    assert _group_grants(client, publisher.headers, b_uuid) == []
    assert _overview(client, publisher.headers, agent_id)["groups"] == []
    assert b_uuid not in bundle_catalog_uuids(client, member.headers)

    r = _add(client, publisher.headers, agent_id, {"group_id": group["id"], "access": "edit"})
    assert r.status_code == 201, r.text
    assert _group_grants(client, publisher.headers, b_uuid)[0]["access"] == "use"
    assert b_uuid in bundle_catalog_uuids(client, member.headers)

    # a member added later reaches the bundle; the count follows
    add_members(client, su, group["id"], [outsider.id])
    assert b_uuid in bundle_catalog_uuids(client, outsider.headers)
    assert _overview(client, publisher.headers, agent_id)["groups"][0]["member_count"] == 2
    remove_member(client, su, group["id"], outsider.id)
    assert b_uuid not in bundle_catalog_uuids(client, outsider.headers)


def test_group_grant_error_codes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    grantee = make_person(client, su)
    group = create_group(client, su)
    other_group = create_group(client, su)
    bundle = publish_agent_bundle(client, publisher.headers, "grp-card-errors")
    other = publish_agent_bundle(client, publisher.headers, "grp-card-errors-other")
    for b in (bundle, other):
        set_bundle_visibility(client, publisher.headers, b["bundle_uuid"], visibility="users")
    agent_id, b_uuid = bundle["agent_id"], bundle["bundle_uuid"]

    assert _add(
        client, publisher.headers, agent_id, {"group_id": str(uuid.uuid4())}
    ).status_code == 404
    assert _group_grants(client, publisher.headers, b_uuid) == []  # refusals write nothing

    # an extra access key (even maintain) is ignored: the card pins use
    assert _add(
        client, publisher.headers, agent_id, {"group_id": group["id"], "access": "maintain"}
    ).status_code == 201
    dup = _add(client, publisher.headers, agent_id, {"group_id": group["id"], "access": "edit"})
    assert dup.status_code == 409, dup.text
    assert _group_grants(client, publisher.headers, b_uuid)[0]["access"] == "use"

    # DELETE: a person grant id, another bundle's group grant, an unknown id -> 404
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(grantee.id, "use"),
        {"principal": {"group_id": group["id"]}, "access": "use"},
    ])
    ov = _overview(client, publisher.headers, agent_id)
    person_grant_id = next(
        u["bundle_grant_id"] for u in ov["users"] if u["user_id"] == grantee.id
    )
    other_row = _add(
        client, publisher.headers, other["agent_id"], {"group_id": other_group["id"]}
    ).json()

    assert _revoke(client, publisher.headers, agent_id, person_grant_id).status_code == 404
    assert _revoke(
        client, publisher.headers, agent_id, other_row["bundle_grant_id"]
    ).status_code == 404
    assert _revoke(client, publisher.headers, agent_id, str(uuid.uuid4())).status_code == 404
    # nothing was removed by the refusals
    assert len(_overview(client, publisher.headers, other["agent_id"])["groups"]) == 1
    assert any(u["user_id"] == grantee.id for u in _overview(
        client, publisher.headers, agent_id)["users"])
    assert len(_overview(client, publisher.headers, agent_id)["groups"]) == 1


def test_group_grant_writes_and_overview_are_publisher_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)  # developer AND group member
    plain = make_person(client, su, developer=False)
    group = create_group(client, su, member_ids=[member.id])
    bundle = publish_agent_bundle(client, publisher.headers, "grp-card-authz")
    agent_id, b_uuid = bundle["agent_id"], bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    row = _add(client, publisher.headers, agent_id, {"group_id": group["id"]}).json()

    for who in (member, plain):
        _overview(client, who.headers, agent_id, expected=404 if who is member else 403)
        r = _add(client, who.headers, agent_id, {"group_id": group["id"], "access": "edit"})
        assert r.status_code == (404 if who is member else 403), r.text
        r = _revoke(client, who.headers, agent_id, row["bundle_grant_id"])
        assert r.status_code == (404 if who is member else 403), r.text

    # the member's attempts changed nothing
    groups = _overview(client, publisher.headers, agent_id)["groups"]
    assert [g["bundle_grant_id"] for g in groups] == [row["bundle_grant_id"]]
    assert _group_grants(client, publisher.headers, b_uuid)[0]["access"] == "use"


# ── Group producer-scope grants in the card (user_groups_scopes B1) ──────────


def _gg_url(producer_id: str, grant_id: str | None = None) -> str:
    url = f"{API}/agents/{producer_id}/agent-api/group-grants"
    return f"{url}/{grant_id}" if grant_id else url


def _connected_install(client: TestClient, owner_headers: dict, producer_id: str, name: str):
    """Owner connects to ``producer_id`` and builds + publishes an install that
    consumes it. Returns ``{agent_id, bundle_uuid, credential_id}``."""
    r = client.post(f"{API}/agents/{producer_id}/agent-api/connect", headers=owner_headers, json={})
    assert r.status_code == 200, r.text
    cred_id = r.json()["credential_id"]
    install_agent = create_agent_via_api(client, owner_headers, name=name)
    drain_tasks()
    link_credential_to_agent(client, owner_headers, install_agent["id"], cred_id)
    fresh = publish_bundle(client, owner_headers, install_agent["id"])
    return {
        "agent_id": install_agent["id"],
        "bundle_uuid": fresh["bundle_uuid"],
        "credential_id": cred_id,
    }


def test_overview_carries_group_scope_grants_and_scopes_only_group(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    scopes_only = create_group(client, su, name=f"so-{uuid.uuid4().hex[:6]}", member_ids=[member.id])
    both = create_group(client, su, name=f"zz-{uuid.uuid4().hex[:6]}", member_ids=[member.id])
    producer = setup_producer(client, publisher.headers, name="Card Producer", identity_enabled=True)
    inst = _connected_install(client, publisher.headers, producer["id"], "card-install")
    set_bundle_visibility(client, publisher.headers, inst["bundle_uuid"], visibility="users")

    # a group holding scopes only
    r = client.post(_gg_url(producer["id"]), headers=publisher.headers,
                    json={"group_id": scopes_only["id"], "scopes": ["orders.read"]})
    assert r.status_code == 200, r.text
    sg = r.json()
    # a group holding bundle access (via the card) and scopes
    r = _add(client, publisher.headers, inst["agent_id"], {"group_id": both["id"]})
    assert r.status_code == 201, r.text
    bundle_grant_id = r.json()["bundle_grant_id"]
    r = client.post(_gg_url(producer["id"]), headers=publisher.headers,
                    json={"group_id": both["id"], "scopes": ["a", "b"]})
    assert r.status_code == 200, r.text
    bg = r.json()

    ov = _overview(client, publisher.headers, inst["agent_id"])
    prod = next(p for p in ov["producers"] if p["producer_agent_id"] == producer["id"])
    assert prod["can_manage"] is True
    by_group = {g["group_id"]: g for g in prod["group_grants"]}
    assert by_group[scopes_only["id"]] == {
        "group_id": scopes_only["id"], "grant_id": sg["id"], "scopes": ["orders.read"],
    }
    assert by_group[both["id"]]["grant_id"] == bg["id"]
    assert by_group[both["id"]]["scopes"] == ["a", "b"]

    groups = {g["group_id"]: g for g in ov["groups"]}
    assert set(groups) == {scopes_only["id"], both["id"]}
    assert groups[scopes_only["id"]]["bundle_grant_id"] is None
    assert groups[scopes_only["id"]]["member_count"] == 1
    assert "access" not in groups[scopes_only["id"]]
    assert groups[both["id"]]["bundle_grant_id"] == bundle_grant_id
    assert [g["name"] for g in ov["groups"]] == sorted(
        (g["name"] for g in ov["groups"]), key=str.lower
    )


def test_overview_non_manageable_producer_has_empty_group_grants(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    prod_owner = make_person(client, su)
    publisher = make_person(client, su)
    group = create_group(client, su)
    producer = setup_producer(client, prod_owner.headers, name="Foreign Producer",
                              identity_enabled=True)
    r = client.post(_gg_url(producer["id"]), headers=prod_owner.headers,
                    json={"group_id": group["id"], "scopes": ["s"]})
    assert r.status_code == 200, r.text

    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect",
                    headers=prod_owner.headers, json={})
    assert r.status_code == 200, r.text
    cred_id = r.json()["credential_id"]
    set_credential_sharing(client, prod_owner.headers, cred_id, True)
    share_credential_via_api(client, prod_owner.headers, cred_id, publisher.email)

    install_agent = create_agent_via_api(client, publisher.headers, name="foreign-install")
    drain_tasks()
    link_credential_to_agent(client, publisher.headers, install_agent["id"], cred_id)
    publish_bundle(client, publisher.headers, install_agent["id"])

    ov = _overview(client, publisher.headers, install_agent["id"])
    prod = next(p for p in ov["producers"] if p["producer_agent_id"] == producer["id"])
    assert prod["can_manage"] is False
    assert prod["group_grants"] == []
    assert ov["groups"] == []  # no group data leaks


def test_group_removal_flow_revokes_bundle_and_scope_grants(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The card's remove-group action: revoke the bundle grant, then delete the
    scope grant on each manageable producer."""
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    producer = setup_producer(client, publisher.headers, name="Removal Producer",
                              identity_enabled=True)
    inst = _connected_install(client, publisher.headers, producer["id"], "removal-install")
    set_bundle_visibility(client, publisher.headers, inst["bundle_uuid"], visibility="users")

    r = _add(client, publisher.headers, inst["agent_id"], {"group_id": group["id"]})
    assert r.status_code == 201, r.text
    bundle_grant_id = r.json()["bundle_grant_id"]
    r = client.post(_gg_url(producer["id"]), headers=publisher.headers,
                    json={"group_id": group["id"], "scopes": ["x"]})
    assert r.status_code == 200, r.text
    scope_grant_id = r.json()["id"]
    assert len(_overview(client, publisher.headers, inst["agent_id"])["groups"]) == 1

    # revoking only the bundle grant leaves the group listed (scopes remain)
    assert _revoke(client, publisher.headers, inst["agent_id"], bundle_grant_id).status_code == 204
    groups = _overview(client, publisher.headers, inst["agent_id"])["groups"]
    assert len(groups) == 1 and groups[0]["bundle_grant_id"] is None

    r = client.delete(_gg_url(producer["id"], scope_grant_id), headers=publisher.headers)
    assert r.status_code == 200, r.text
    ov = _overview(client, publisher.headers, inst["agent_id"])
    assert ov["groups"] == []
    assert all(p["group_grants"] == [] for p in ov["producers"])


def test_card_group_add_is_use_and_ignores_extra_access_field(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    group = create_group(client, su)
    bundle = publish_agent_bundle(client, publisher.headers, "card-use-pin")
    set_bundle_visibility(client, publisher.headers, bundle["bundle_uuid"], visibility="users")
    r = _add(client, publisher.headers, bundle["agent_id"],
             {"group_id": group["id"], "access": "edit"})
    assert r.status_code == 201, r.text
    assert "access" not in r.json()
    assert _group_grants(client, publisher.headers, bundle["bundle_uuid"])[0]["access"] == "use"
    # duplicate stays 409
    r = _add(client, publisher.headers, bundle["agent_id"], {"group_id": group["id"]})
    assert r.status_code == 409, r.text


def test_existing_edit_group_grant_still_listed_in_overview(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    group = create_group(client, su)
    bundle = publish_agent_bundle(client, publisher.headers, "card-edit-legacy")
    b_uuid = bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        {"principal": {"group_id": group["id"]}, "access": "edit"},
    ])
    groups = _overview(client, publisher.headers, bundle["agent_id"])["groups"]
    assert [g["group_id"] for g in groups] == [group["id"]]
    assert groups[0]["bundle_grant_id"] is not None
    assert "access" not in groups[0]
