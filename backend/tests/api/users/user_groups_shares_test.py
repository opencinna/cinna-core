"""User Groups P5: shares aggregate, superuser revoke, impact previews, group delete.

Scenarios:
  1. One share per family is listed with type / name / level / granted-by;
     the routes are superuser-only; unknown type 422, missing share 404.
  2. Superuser revoke per family removes members' access; a direct grant keeps it;
     a catalog revoke keeps the item's other grants.
  3. Credential: revoke-impact / removal-impact / delete-impact numbers match the
     links removed; revoke unlinks only members with no other path.
  4. AI credential: the same for pinned environments (cleared / restarted).
  5. Group delete releases only this group's shares; a link to an unrelated
     directly shared credential survives; catalog / artifact / knowledge go.
  6. ``member_count`` counts active members only.
"""
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.api.ai_credentials.test_ai_credentials_group_sharing import (
    RESTART_SEAM,
    _env_of,
    _pins,
    _share as _share_ai,
)
from tests.api.artifacts.test_artifacts_group_grants import _put as _put_artifact_grants
from tests.api.artifacts.test_artifacts_group_grants import _shared_ids, _site
from tests.api.catalog.catalog_helpers import (
    bundle_catalog_uuids,
    make_person,
    publish_agent_bundle,
    publish_skill_package,
    put_item_grants,
    role_grant,
    set_bundle_visibility,
    user_grant,
)
from tests.api.catalog.catalog_group_grants_test import group_grant
from tests.api.credentials.test_credentials_group_sharing import (
    _agent_with_adapter,
    _group_share,
    _linked,
    _shareable,
    _synced,
)
from tests.api.knowledge_sources.test_knowledge_sources_group_shares import (
    _connected_source_with_article,
    _query_ids,
    _share_group,
)
from tests.api.knowledge_sources.test_knowledge_query import _agent_headers_for_owner
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.artifacts import publish
from tests.utils.background_tasks import drain_tasks
from tests.utils.catalog import create_desktop_agent, list_items, patch_item
from tests.utils.credential import link_credential_to_agent, share_credential_via_api
from tests.utils.environment import link_ai_credential_to_environment, set_environment_status
from tests.utils.knowledge_source import add_shared_user, create_knowledge_source
from tests.utils.skill_catalog import catalog_ids, list_skill_catalog
from tests.utils.user import create_random_user, create_random_user_with_headers
from tests.utils.user_group import (
    add_members,
    create_group,
    delete_group,
    delete_impact,
    list_shares,
    removal_impact,
    revoke_share,
    share_revoke_impact,
)

API = settings.API_V1_STR
_ADMIN = f"{API}/admin/user-groups"


def _by_type(shares: dict) -> dict[str, dict]:
    return {s["resource_type"]: s for s in shares["data"]}


def _share_bundle(client, publisher, group_id, name) -> dict:
    bundle = publish_agent_bundle(client, publisher.headers, name)
    set_bundle_visibility(client, publisher.headers, bundle["bundle_uuid"], visibility="users")
    put_item_grants(client, publisher.headers, bundle["bundle_uuid"], [
        user_grant(publisher.id, "maintain"), group_grant(group_id, "use"),
    ])
    return bundle


# ── Scenario 1: listing, authorization, validation ────────────────────────


def test_group_shares_list_one_per_family_with_levels_and_granted_by(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    gid = group["id"]
    assert list_shares(client, su, gid) == {"data": [], "count": 0}

    _share_bundle(client, publisher, gid, "p5-bundle")
    skill = publish_skill_package(client, publisher.headers, "p5-skill", visibility="users")
    put_item_grants(client, publisher.headers, skill["package_uuid"], [
        user_grant(publisher.id, "maintain"), group_grant(gid, "edit"),
    ])
    desktop = create_desktop_agent(client, publisher.headers, display_name="P5 Desktop",
                                   visibility="users")
    put_item_grants(client, publisher.headers, desktop["uuid"], [
        user_grant(publisher.id, "maintain"), group_grant(gid, "use"),
    ])
    cred = _shareable(client, publisher.headers)
    _group_share(client, publisher.headers, cred["id"], gid)
    ai = create_random_ai_credential(client, publisher.headers, name="p5-ai-key")
    _share_ai(client, publisher.headers, ai["id"], gid)
    _, art_owner = create_random_user_with_headers(client)
    art = publish(client, art_owner, archive=_site(), title="P5 Artifact")
    _put_artifact_grants(client, art_owner, art["id"], [{"group_id": gid}])
    source = create_knowledge_source(client, su, name="p5-knowledge", access_level="shared")
    _share_group(client, su, source["id"], gid)

    shares = list_shares(client, su, gid)
    assert shares["count"] == 7
    rows = _by_type(shares)
    assert set(rows) == {
        "agent_bundle", "desktop_agent", "skill_package", "credential",
        "ai_credential", "artifact", "knowledge_source",
    }
    for kind in ("agent_bundle", "skill_package", "desktop_agent"):
        uuid.UUID(rows[kind]["resource_id"])  # the internal row id, not the catalog slug
        assert rows[kind]["resource_name"]
    assert rows["agent_bundle"]["level"] == "use"
    assert rows["skill_package"]["level"] == "edit"
    assert rows["desktop_agent"]["resource_id"]
    assert rows["desktop_agent"]["resource_name"] == "P5 Desktop"
    assert rows["desktop_agent"]["level"] == "use"
    assert rows["credential"]["resource_id"] == cred["id"]
    assert rows["credential"]["resource_name"] == cred["name"]
    assert rows["credential"]["level"] == "read"
    assert rows["ai_credential"]["resource_name"] == "p5-ai-key"
    assert rows["ai_credential"]["level"] == "use"
    assert rows["artifact"]["resource_id"] == art["id"]
    assert rows["artifact"]["resource_name"] == "P5 Artifact"
    assert rows["artifact"]["level"] == "view"
    assert rows["knowledge_source"]["resource_id"] == source["id"]
    assert rows["knowledge_source"]["resource_name"] == "p5-knowledge"
    assert rows["knowledge_source"]["level"] == "query"

    for kind in ("agent_bundle", "desktop_agent", "skill_package", "credential", "ai_credential"):
        assert rows[kind]["granted_by"]["user_id"] == publisher.id, kind
        assert rows[kind]["owner"]["user_id"] == publisher.id, kind
    assert rows["knowledge_source"]["granted_by"] is not None
    assert all(s["granted_at"] for s in shares["data"])
    # sorted by type: bundle family first, knowledge last
    assert [s["resource_type"] for s in shares["data"]][0] == "agent_bundle"
    assert [s["resource_type"] for s in shares["data"]][-1] == "knowledge_source"

    # share_count on the group comes from the same aggregate
    r = client.get(f"{_ADMIN}/{gid}", headers=su)
    assert r.json()["share_count"] == 7


def test_group_share_routes_are_superuser_only_and_validate_input(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, plain = create_random_user_with_headers(client)
    group = create_group(client, su)
    gid, rid = group["id"], str(uuid.uuid4())

    for method, url in [
        ("get", f"{_ADMIN}/{gid}/shares"),
        ("delete", f"{_ADMIN}/{gid}/shares/artifact/{rid}"),
        ("get", f"{_ADMIN}/{gid}/shares/artifact/{rid}/revoke-impact"),
        ("get", f"{_ADMIN}/{gid}/delete-impact"),
        ("get", f"{_ADMIN}/{gid}/members/{rid}/removal-impact"),
        ("delete", f"{_ADMIN}/{gid}"),
    ]:
        assert getattr(client, method)(url, headers=plain).status_code == 403, url

    # unknown resource type 422; missing share 404 (revoke and its preview)
    assert revoke_share(client, su, gid, "widget", rid).status_code == 422
    for rtype in ("agent_bundle", "desktop_agent", "skill_package", "credential",
                  "ai_credential", "artifact", "knowledge_source"):
        assert revoke_share(client, su, gid, rtype, rid).status_code == 404, rtype
    assert client.get(
        f"{_ADMIN}/{gid}/shares/widget/{rid}/revoke-impact", headers=su
    ).status_code == 422
    assert client.get(
        f"{_ADMIN}/{gid}/shares/credential/{rid}/revoke-impact", headers=su
    ).status_code == 404

    # unknown group / non-member
    ghost = str(uuid.uuid4())
    assert client.get(f"{_ADMIN}/{ghost}/shares", headers=su).status_code == 404
    assert client.get(f"{_ADMIN}/{ghost}/delete-impact", headers=su).status_code == 404
    assert client.get(
        f"{_ADMIN}/{gid}/members/{rid}/removal-impact", headers=su
    ).status_code == 404
    assert client.delete(f"{_ADMIN}/{ghost}", headers=su).status_code == 404


# ── Scenario 2: revoke per family ─────────────────────────────────────────


def test_revoke_catalog_shares_removes_member_access_and_keeps_other_grants(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    direct_member = make_person(client, su)
    gid = create_group(client, su, member_ids=[member.id, direct_member.id])["id"]
    bundle = _share_bundle(client, publisher, gid, "p5-rev-bundle")
    b_uuid = bundle["bundle_uuid"]
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"), group_grant(gid, "use"),
        user_grant(direct_member.id, "use"),
    ])
    skill = publish_skill_package(client, publisher.headers, "p5-rev-skill", visibility="users")
    s_uuid = skill["package_uuid"]
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"), group_grant(gid, "use"),
    ])
    desktop = create_desktop_agent(client, publisher.headers, visibility="users")
    patch_item(client, publisher.headers, desktop["uuid"], is_listed=True)
    put_item_grants(client, publisher.headers, desktop["uuid"], [
        user_grant(publisher.id, "maintain"), group_grant(gid, "edit"),
    ])
    assert b_uuid in bundle_catalog_uuids(client, member.headers)
    assert s_uuid in catalog_ids(list_skill_catalog(client, member.headers))
    assert desktop["uuid"] in {i["uuid"] for i in list_items(client, member.headers)}

    ids = {t: s["resource_id"] for t, s in _by_type(list_shares(client, su, gid)).items()}
    for rtype, rid in ids.items():
        assert share_revoke_impact(client, su, gid, rtype, rid) == {
            "member_count": 2, "shares_by_type": {rtype: 1},
            "users_losing_credential_access": 0, "credential_links_removed": 0,
            "ai_environments_cleared": 0, "ai_environments_restarted": 0,
        }
        r = revoke_share(client, su, gid, rtype, rid)
        assert r.status_code == 200, r.text
        assert revoke_share(client, su, gid, rtype, rid).status_code == 404

    assert list_shares(client, su, gid)["count"] == 0
    assert b_uuid not in bundle_catalog_uuids(client, member.headers)
    assert s_uuid not in catalog_ids(list_skill_catalog(client, member.headers))
    assert desktop["uuid"] not in {i["uuid"] for i in list_items(client, member.headers)}
    assert client.get(f"{API}/catalog/items/{b_uuid}", headers=member.headers).status_code == 404
    # the direct grant on the bundle is untouched
    assert b_uuid in bundle_catalog_uuids(client, direct_member.headers)
    # the publisher's own and other grants stay
    assert b_uuid in bundle_catalog_uuids(client, publisher.headers)
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"), role_grant("agent-user", "use"),
    ])  # still editable after the revoke


def test_revoke_artifact_and_knowledge_shares_keep_direct_access(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    su = superuser_token_headers
    _, owner = create_random_user_with_headers(client)
    plain, plain_h = create_random_user_with_headers(client)
    both, both_h = create_random_user_with_headers(client)
    gid = create_group(client, su, member_ids=[plain["id"], both["id"]])["id"]

    art = publish(client, owner, archive=_site())
    _put_artifact_grants(client, owner, art["id"], [{"group_id": gid}, {"user_id": both["id"]}])
    assert _shared_ids(client, plain_h) == [art["id"]]

    source, article_id = _connected_source_with_article(client, db, su, "shared")
    plain_k = _agent_headers_for_owner(db, uuid.UUID(plain["id"]), "p5-ks-plain")
    both_k = _agent_headers_for_owner(db, uuid.UUID(both["id"]), "p5-ks-both")
    _share_group(client, su, source["id"], gid)
    add_shared_user(client, su, source["id"], both["id"])
    assert article_id in _query_ids(client, plain_k)

    assert revoke_share(client, su, gid, "artifact", art["id"]).status_code == 200
    assert revoke_share(client, su, gid, "knowledge_source", source["id"]).status_code == 200

    assert _shared_ids(client, plain_h) == []
    assert _shared_ids(client, both_h) == [art["id"]], "direct grant stays"
    assert article_id not in _query_ids(client, plain_k)
    assert article_id in _query_ids(client, both_k), "direct share stays"
    assert list_shares(client, su, gid)["count"] == 0


# ── Scenario 3: credentials ───────────────────────────────────────────────


def test_credential_impact_numbers_revoke_and_group_delete_release_only_this_groups_links(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh = make_person_headers(client, su)
    lost, lh = make_person_headers(client, su)
    direct, dh = make_person_headers(client, su)
    idle, _ = make_person_headers(client, su)
    gid = create_group(client, su, member_ids=[lost["id"], direct["id"], idle["id"]])["id"]
    cred = _shareable(client, oh)
    unrelated = _shareable(client, oh)
    share_credential_via_api(client, oh, cred["id"], direct["email"])
    share_credential_via_api(client, oh, unrelated["id"], lost["email"])
    _group_share(client, oh, cred["id"], gid)

    l_agent, l_adapter = _agent_with_adapter(client, lh, patch_environment_adapter, "P5-Lost")
    d_agent, d_adapter = _agent_with_adapter(client, dh, patch_environment_adapter, "P5-Direct")
    link_credential_to_agent(client, lh, l_agent, cred["id"])
    link_credential_to_agent(client, lh, l_agent, unrelated["id"])
    link_credential_to_agent(client, dh, d_agent, cred["id"])
    drain_tasks()
    assert {cred["id"], unrelated["id"]} <= _synced(l_adapter)

    expected = {
        "member_count": 3, "shares_by_type": {"credential": 1},
        # two users hold no other path (lost + idle); only lost has a link
        "users_losing_credential_access": 2, "credential_links_removed": 1,
        "ai_environments_cleared": 0, "ai_environments_restarted": 0,
    }
    assert delete_impact(client, su, gid) == expected
    assert share_revoke_impact(client, su, gid, "credential", cred["id"]) == expected
    assert removal_impact(client, su, gid, lost["id"]) == {
        **expected, "member_count": 1, "users_losing_credential_access": 1,
    }
    impact = removal_impact(client, su, gid, idle["id"])
    assert impact["users_losing_credential_access"] == 1
    assert impact["credential_links_removed"] == 0
    impact = removal_impact(client, su, gid, direct["id"])
    assert impact["users_losing_credential_access"] == 0, "direct share is another path"
    assert impact["credential_links_removed"] == 0
    # previews are read-only
    assert cred["id"] in _linked(client, lh, l_agent)

    # revoke: only the member with no other path loses the link
    r = revoke_share(client, su, gid, "credential", cred["id"])
    assert r.status_code == 200, r.text
    drain_tasks()
    assert _linked(client, lh, l_agent) == {unrelated["id"]}
    assert cred["id"] not in _synced(l_adapter)
    assert cred["id"] in _linked(client, dh, d_agent)
    assert delete_impact(client, su, gid)["credential_links_removed"] == 0

    # group delete: re-share, relink, delete the group
    _group_share(client, oh, cred["id"], gid)
    link_credential_to_agent(client, lh, l_agent, cred["id"])
    drain_tasks()
    assert delete_impact(client, su, gid)["credential_links_removed"] == 1
    delete_group(client, su, gid)
    drain_tasks()

    assert client.get(f"{_ADMIN}/{gid}", headers=su).status_code == 404
    assert _linked(client, lh, l_agent) == {unrelated["id"]}, "unrelated direct share survives"
    assert unrelated["id"] in _synced(l_adapter)
    assert cred["id"] in _linked(client, dh, d_agent)


def make_person_headers(client, su):
    from tests.utils.skill_catalog import make_developer

    return make_developer(client, su)


# ── Scenario 4: AI credentials ────────────────────────────────────────────


def test_ai_credential_impact_numbers_revoke_and_delete_clear_pins(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    su = superuser_token_headers
    _, oh = make_person_headers(client, su)
    m1, h1 = make_person_headers(client, su)
    m2, h2 = make_person_headers(client, su)
    gid = create_group(client, su, member_ids=[m1["id"], m2["id"]])["id"]
    cred = create_random_ai_credential(client, oh, name="p5-impact-ai")
    _share_ai(client, oh, cred["id"], gid)

    running = _env_of(client, h1, "P5-AI-Running")
    stopped = _env_of(client, h1, "P5-AI-Stopped")
    link_ai_credential_to_environment(db, running, cred["id"], conversation=True)
    link_ai_credential_to_environment(db, stopped, cred["id"], building=True)
    set_environment_status(db, running, "running")
    set_environment_status(db, stopped, "stopped")

    expected = {
        "member_count": 2, "shares_by_type": {"ai_credential": 1},
        "users_losing_credential_access": 2, "credential_links_removed": 0,
        "ai_environments_cleared": 2, "ai_environments_restarted": 1,
    }
    assert delete_impact(client, su, gid) == expected
    assert share_revoke_impact(client, su, gid, "ai_credential", cred["id"]) == expected
    assert removal_impact(client, su, gid, m1["id"]) == {
        **expected, "member_count": 1, "users_losing_credential_access": 1,
    }
    idle = removal_impact(client, su, gid, m2["id"])
    assert idle["ai_environments_cleared"] == 0 and idle["users_losing_credential_access"] == 1

    restart = AsyncMock()
    with patch(RESTART_SEAM, new=restart):
        r = revoke_share(client, su, gid, "ai_credential", cred["id"])
        assert r.status_code == 200, r.text
        drain_tasks()
    assert _pins(client, h1, running) == (None, None)
    assert _pins(client, h1, stopped) == (None, None)
    assert [c.args[0] for c in restart.call_args_list] == [uuid.UUID(running)]

    # delete path: re-share and pin again, then delete the group
    _share_ai(client, oh, cred["id"], gid)
    link_ai_credential_to_environment(db, running, cred["id"], conversation=True)
    own = create_random_ai_credential(client, h1)
    link_ai_credential_to_environment(db, stopped, own["id"], building=True)
    restart = AsyncMock()
    with patch(RESTART_SEAM, new=restart):
        delete_group(client, su, gid)
        drain_tasks()
    assert _pins(client, h1, running) == (None, None)
    assert _pins(client, h1, stopped) == (None, own["id"]), "own key untouched"
    assert restart.call_count == 1


# ── Scenario 5: group delete across families ──────────────────────────────


def test_group_delete_removes_catalog_artifact_and_knowledge_access(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    gid = create_group(client, su, member_ids=[member.id])["id"]
    bundle = _share_bundle(client, publisher, gid, "p5-del-bundle")
    _, owner = create_random_user_with_headers(client)
    art = publish(client, owner, archive=_site())
    _put_artifact_grants(client, owner, art["id"], [{"group_id": gid}])
    source, article_id = _connected_source_with_article(client, db, su, "shared")
    _share_group(client, su, source["id"], gid)
    member_k = _agent_headers_for_owner(db, uuid.UUID(member.id), "p5-del-ks")
    assert bundle["bundle_uuid"] in bundle_catalog_uuids(client, member.headers)
    assert article_id in _query_ids(client, member_k)

    impact = delete_impact(client, su, gid)
    assert impact["member_count"] == 1
    assert impact["shares_by_type"] == {"agent_bundle": 1, "artifact": 1, "knowledge_source": 1}
    assert impact["credential_links_removed"] == 0

    assert delete_group(client, su, gid) == {"message": "User group deleted"}
    assert bundle["bundle_uuid"] not in bundle_catalog_uuids(client, member.headers)
    assert article_id not in _query_ids(client, member_k)
    # the artifact owner still reaches theirs; the member does not
    assert client.get(f"{API}/artifacts/{art['id']}", headers=owner).status_code == 200


# ── Scenario 6: active members only ───────────────────────────────────────


def test_impact_member_count_counts_active_members_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, owner = create_random_user_with_headers(client)
    users = [create_random_user(client) for _ in range(3)]
    gid = create_group(client, su, member_ids=[u["id"] for u in users])["id"]
    art = publish(client, owner, archive=_site())
    _put_artifact_grants(client, owner, art["id"], [{"group_id": gid}])
    assert delete_impact(client, su, gid)["member_count"] == 3

    r = client.patch(f"{API}/users/{users[0]['id']}", headers=su, json={"is_active": False})
    assert r.status_code == 200, r.text

    assert delete_impact(client, su, gid)["member_count"] == 2
    assert share_revoke_impact(client, su, gid, "artifact", art["id"])["member_count"] == 2
    assert removal_impact(client, su, gid, users[1]["id"])["member_count"] == 1
    assert removal_impact(client, su, gid, users[0]["id"])["member_count"] == 0


# ── Scenario: agent API scope grants (user_groups_scopes B1) ───────────────


def test_agent_api_scope_grant_share_listing_revoke_and_impact(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    from tests.utils.agent_api_keys import setup_producer

    su = superuser_token_headers
    owner, owner_h = make_person_headers(client, su)
    member, _ = create_random_user_with_headers(client)
    group = create_group(client, su, member_ids=[member["id"]])
    gid = group["id"]
    producer = setup_producer(client, owner_h, name="Share Producer", identity_enabled=True)
    pid = producer["id"]
    url = f"{settings.API_V1_STR}/agents/{pid}/agent-api/group-grants"
    r = client.post(url, headers=owner_h, json={"group_id": gid, "scopes": ["b.write", "a.read"]})
    assert r.status_code == 200, r.text

    shares = list_shares(client, su, gid)
    assert shares["count"] == 1
    row = shares["data"][0]
    assert row["resource_type"] == "agent_api_scopes"
    assert row["resource_id"] == pid
    assert row["resource_name"] == "Share Producer"
    assert row["level"] == "b.write, a.read"
    assert row["owner"]["user_id"] == owner["id"]

    assert delete_impact(client, su, gid)["shares_by_type"] == {"agent_api_scopes": 1}
    assert removal_impact(client, su, gid, member["id"])["shares_by_type"] == {
        "agent_api_scopes": 1
    }
    assert share_revoke_impact(client, su, gid, "agent_api_scopes", pid) is not None

    # non-superuser cannot revoke
    assert revoke_share(client, owner_h, gid, "agent_api_scopes", pid).status_code in (401, 403)

    assert revoke_share(client, su, gid, "agent_api_scopes", pid).status_code == 200
    assert list_shares(client, su, gid) == {"data": [], "count": 0}
    assert client.get(url, headers=owner_h).json()["data"] == []
    # absent now -> 404
    assert revoke_share(client, su, gid, "agent_api_scopes", pid).status_code == 404
