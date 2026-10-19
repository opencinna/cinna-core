"""User-group grants on catalog items (User Groups P2).

Scenarios:
  1. PUT/GET grants with a group principal: use/edit accepted, maintain 422,
     unknown group 422; GET shows the group name + member_count, ordered
     people, groups, roles.
  2. Bundle: a group member sees it in the listing, opens the detail and
     installs it; a non-member gets 404; removal from the group makes the
     item 404 at once while the existing install keeps running; a member
     added later gains access.
  3. Skill: an ``edit`` group grant lets a member publish a revision, ``use``
     does not (403); removal revokes both listing and publish.
  4. Desktop agent: member lists / opens it and passes the git read gate;
     refused after removal.
  5. Direct ``use`` + group ``edit`` = edit; revoking the group grant leaves
     the direct one; revoking the direct one leaves the group's.
  6. ``catalog.granted`` reaches group members (and only newly reached ones).
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    bundle_catalog_uuids,
    get_item_grants,
    make_person,
    publish_agent_bundle,
    publish_skill_package,
    put_item_grants,
    role_grant,
    set_bundle_visibility,
    user_grant,
)
from tests.utils.bundle import install_bundle
from tests.utils.catalog import create_desktop_agent, get_item, list_items, patch_item
from tests.utils.skill_catalog import (
    catalog_ids,
    list_skill_catalog,
    make_agent_with_env,
    publish_skill,
    write_skill,
)
from tests.utils.user_group import add_members, create_group, remove_member

API = settings.API_V1_STR


def group_grant(group_id: str, access: str) -> dict:
    return {"principal": {"group_id": group_id}, "access": access}


def _basic(token: str) -> dict[str, str]:
    import base64

    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _git_refs_status(client: TestClient, repo_id: str, token: str) -> int:
    return client.get(
        f"/git/{repo_id}.git/info/refs",
        params={"service": "git-upload-pack"},
        headers=_basic(token),
    ).status_code


def _emits(stub, event_type: str) -> list[dict]:
    return [
        e for e in stub.emitted_events
        if e["event"] == "event" and e["data"]["type"] == event_type
    ]


def _skill_detail_status(client, headers, package_uuid) -> int:
    return client.get(f"{API}/skills/packages/{package_uuid}", headers=headers).status_code


# ── Scenario 1: validation and listing ────────────────────────────────────


def test_group_grants_validation_and_listing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    direct = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    gid = group["id"]
    bundle = publish_agent_bundle(client, publisher.headers, "grp-validate")
    b_uuid = bundle["bundle_uuid"]

    owner = user_grant(publisher.id, "maintain")
    put_item_grants(
        client, publisher.headers, b_uuid, [owner, group_grant(gid, "maintain")],
        expected_status=422,
    )
    put_item_grants(
        client, publisher.headers, b_uuid,
        [owner, group_grant("00000000-0000-4000-8000-000000000001", "use")],
        expected_status=422,
    )
    # refusals write nothing
    assert len(get_item_grants(client, publisher.headers, b_uuid)) == 1

    put_item_grants(client, publisher.headers, b_uuid, [
        role_grant("agent-user", "use"),
        group_grant(gid, "edit"),
        user_grant(direct.id, "use"),
        owner,
    ])
    grants = get_item_grants(client, publisher.headers, b_uuid)
    kinds = [
        "user" if "user_id" in g["principal"] else "group" if "group_id" in g["principal"] else "role"
        for g in grants
    ]
    # highest level first; within a level: people, groups, roles
    assert kinds == ["user", "group", "user", "role"]
    g = next(x for x in grants if "group_id" in x["principal"])
    assert g["principal"]["group_id"] == gid
    assert g["access"] == "edit"
    assert g["display_name"] == group["name"]
    assert g["member_count"] == 1

    add_members(client, su, gid, [direct.id])
    g = next(
        x for x in get_item_grants(client, publisher.headers, b_uuid)
        if "group_id" in x["principal"]
    )
    assert g["member_count"] == 2


# ── Scenario 2: bundle ────────────────────────────────────────────────────


def test_bundle_group_member_lists_opens_installs_until_removed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    late = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    bundle = publish_agent_bundle(client, publisher.headers, "grp-bundle")
    b_uuid, b_id = bundle["bundle_uuid"], bundle["bundle_id"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")

    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"), group_grant(group["id"], "use"),
    ])

    assert b_uuid in bundle_catalog_uuids(client, member.headers)
    assert b_uuid not in bundle_catalog_uuids(client, outsider.headers)
    assert client.get(f"{API}/bundles/{b_uuid}", headers=member.headers).status_code == 200
    assert client.get(f"{API}/bundles/{b_uuid}", headers=outsider.headers).status_code != 200
    assert get_item(client, member.headers, b_uuid)["uuid"] == b_uuid
    assert client.get(
        f"{API}/catalog/items/{b_uuid}", headers=outsider.headers
    ).status_code == 404
    install_bundle(client, outsider.headers, b_id, expected_status=403)

    install = install_bundle(client, member.headers, b_id)
    assert install["id"]

    # a member added later gains access at once
    assert b_uuid not in bundle_catalog_uuids(client, late.headers)
    add_members(client, su, group["id"], [late.id])
    assert b_uuid in bundle_catalog_uuids(client, late.headers)

    # removal: the item leaves the listing, detail 404; the install stays
    remove_member(client, su, group["id"], member.id)
    assert b_uuid not in bundle_catalog_uuids(client, member.headers)
    assert client.get(f"{API}/bundles/{b_uuid}", headers=member.headers).status_code != 200
    assert client.get(
        f"{API}/catalog/items/{b_uuid}", headers=member.headers
    ).status_code == 404
    install_bundle(client, member.headers, b_id, expected_status=403)
    assert client.get(
        f"{API}/agents/{install['id']}", headers=member.headers
    ).status_code == 200
    assert b_uuid in bundle_catalog_uuids(client, late.headers)


# ── Scenario 3: skill ─────────────────────────────────────────────────────


def test_skill_group_edit_publishes_use_does_not_and_removal_revokes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pkg = publish_skill_package(client, publisher.headers, "grp-notes", visibility="users")
    s_uuid, package_id = pkg["package_uuid"], pkg["package_id"]
    agent_id, env_id = make_agent_with_env(client, member.headers, "grp-member-agent")
    write_skill(env_id, "grp-notes", body="Member's steps.")
    owner = user_grant(publisher.id, "maintain")

    # use: listed, but cannot publish
    put_item_grants(client, publisher.headers, s_uuid, [owner, group_grant(group["id"], "use")])
    assert s_uuid in catalog_ids(list_skill_catalog(client, member.headers))
    assert s_uuid not in catalog_ids(list_skill_catalog(client, outsider.headers))
    assert _skill_detail_status(client, outsider.headers, s_uuid) == 404
    publish_skill(
        client, member.headers, agent_id, "grp-notes",
        package_id=package_id, expected_status=403,
    )

    # edit: publishes a revision into the package
    put_item_grants(client, publisher.headers, s_uuid, [owner, group_grant(group["id"], "edit")])
    revision = publish_skill(
        client, member.headers, agent_id, "grp-notes", package_id=package_id
    )
    assert revision["package_id"] == s_uuid
    assert revision["revision_number"] == 2
    assert revision["published_by_user_id"] == member.id

    # removal: no listing, no detail, no publish
    remove_member(client, su, group["id"], member.id)
    assert s_uuid not in catalog_ids(list_skill_catalog(client, member.headers))
    assert _skill_detail_status(client, member.headers, s_uuid) == 404
    assert client.get(
        f"{API}/catalog/items/{s_uuid}", headers=member.headers
    ).status_code == 404
    publish_skill(
        client, member.headers, agent_id, "grp-notes",
        package_id=package_id, expected_status=409,  # package_id_taken: no longer an editor
    )


# ── Scenario 4: desktop agent ─────────────────────────────────────────────


def test_desktop_agent_group_member_lists_and_reads_git_until_removed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner = make_person(client, su)
    member = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    item = create_desktop_agent(client, owner.headers, visibility="users")
    patch_item(client, owner.headers, item["uuid"], is_listed=True)
    repo_id = item["desktop"]["repo_id"]
    put_item_grants(client, owner.headers, item["uuid"], [
        # no pushed version yet, so only editors list it: grant ``edit``
        user_grant(owner.id, "maintain"), group_grant(group["id"], "edit"),
    ])

    assert item["uuid"] in {i["uuid"] for i in list_items(client, member.headers)}
    assert item["uuid"] not in {i["uuid"] for i in list_items(client, outsider.headers)}
    assert get_item(client, member.headers, item["uuid"])["uuid"] == item["uuid"]
    assert client.get(
        f"{API}/catalog/items/{item['uuid']}", headers=outsider.headers
    ).status_code == 404

    token = member.headers["Authorization"].removeprefix("Bearer ")
    outsider_token = outsider.headers["Authorization"].removeprefix("Bearer ")
    assert _git_refs_status(client, repo_id, token) == 200
    refused = _git_refs_status(client, repo_id, outsider_token)
    assert refused in (403, 404)

    remove_member(client, su, group["id"], member.id)
    assert item["uuid"] not in {i["uuid"] for i in list_items(client, member.headers)}
    assert client.get(
        f"{API}/catalog/items/{item['uuid']}", headers=member.headers
    ).status_code == 404
    assert _git_refs_status(client, repo_id, token) == refused


# ── Scenario 5: direct and group grants coexist ───────────────────────────


def test_direct_use_plus_group_edit_is_edit_and_revoking_one_keeps_the_other(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pkg = publish_skill_package(client, publisher.headers, "grp-coexist", visibility="users")
    s_uuid, package_id = pkg["package_uuid"], pkg["package_id"]
    agent_id, env_id = make_agent_with_env(client, member.headers, "grp-coexist-agent")
    write_skill(env_id, "grp-coexist", body="Steps.")
    owner = user_grant(publisher.id, "maintain")
    direct_use = user_grant(member.id, "use")
    group_edit = group_grant(group["id"], "edit")

    put_item_grants(client, publisher.headers, s_uuid, [owner, direct_use, group_edit])
    revision = publish_skill(client, member.headers, agent_id, "grp-coexist", package_id=package_id)
    assert revision["revision_number"] == 2  # edit wins over the direct use

    # drop the group grant: the direct use stays (listed, no publish)
    put_item_grants(client, publisher.headers, s_uuid, [owner, direct_use])
    assert s_uuid in catalog_ids(list_skill_catalog(client, member.headers))
    publish_skill(
        client, member.headers, agent_id, "grp-coexist",
        package_id=package_id, expected_status=403,
    )

    # drop the direct grant: the group path alone still opens the item
    put_item_grants(client, publisher.headers, s_uuid, [owner, group_edit])
    assert s_uuid in catalog_ids(list_skill_catalog(client, member.headers))
    publish_skill(client, member.headers, agent_id, "grp-coexist", package_id=package_id)

    # direct + group, member leaves the group: the direct grant keeps access
    put_item_grants(client, publisher.headers, s_uuid, [owner, direct_use, group_edit])
    remove_member(client, su, group["id"], member.id)
    assert s_uuid in catalog_ids(list_skill_catalog(client, member.headers))


# ── Scenario 6: catalog.granted ───────────────────────────────────────────


def test_catalog_granted_reaches_group_members(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    m1 = make_person(client, su)
    m2 = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[m1.id, m2.id])
    bundle = publish_agent_bundle(client, publisher.headers, "grp-events")
    b_uuid = bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    socket_stub.emitted_events.clear()

    body = [user_grant(publisher.id, "maintain"), group_grant(group["id"], "use")]
    put_item_grants(client, publisher.headers, b_uuid, body)
    granted = _emits(socket_stub, "catalog.granted")
    assert {e["room"] for e in granted} == {f"user_{m1.id}", f"user_{m2.id}"}
    assert f"user_{outsider.id}" not in {e["room"] for e in granted}
    assert granted[0]["data"]["meta"] == {"kind": "agent", "uuid": b_uuid}

    # repeating the PUT notifies nobody
    socket_stub.emitted_events.clear()
    put_item_grants(client, publisher.headers, b_uuid, body)
    assert _emits(socket_stub, "catalog.granted") == []
