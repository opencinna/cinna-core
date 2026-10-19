"""Credentials shared with a user group (User Groups plan P3a, D7/D8).

A group share is an owner's explicit, direct-like share with every current and
future member. Removing a member or revoking the group share unlinks the
credential only for users left with no path (owner, direct share, another
group), re-syncs their environments, and writes no revocation record (D7).

Env assertions use the adapter-capture pattern of
``test_credential_share_revocation.py``.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    link_credential_to_agent,
    real_credentials_json,
    set_credential_sharing,
    share_credential_via_api,
    update_credential,
)
from tests.utils.skill_catalog import make_developer
from tests.utils.user_group import create_group, remove_member

API = settings.API_V1_STR


def _agent_with_adapter(client, headers, patch_environment_adapter, name):
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    adapter = EnvironmentTestAdapter()
    previous = patch_environment_adapter.get_adapter
    env_id = agent["active_environment_id"]
    patch_environment_adapter.get_adapter = lambda env: (
        adapter if str(env.id) == env_id else previous(env)
    )
    return agent["id"], adapter


def _synced(adapter: EnvironmentTestAdapter) -> set[str]:
    return {e["id"] for e in real_credentials_json(adapter.credentials_set or {})}


def _linked(client, headers, agent_id) -> set[str]:
    return {c["id"] for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _shareable(client, headers) -> dict:
    cred = create_random_credential(client, headers, credential_type="api_token")
    return update_credential(client, headers, cred["id"], allow_sharing=True)


def _group_share(client, headers, cred_id, group_id, expect=200):
    r = client.post(
        f"{API}/credentials/{cred_id}/group-shares",
        headers=headers,
        json={"group_id": group_id},
    )
    assert r.status_code == expect, r.text
    return r.json()


def _revoke_group_share(client, headers, cred_id, group_id):
    r = client.delete(
        f"{API}/credentials/{cred_id}/group-shares/{group_id}", headers=headers
    )
    assert r.status_code == 200, r.text
    drain_tasks()


def _shared_with_me(client, headers) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/shared-with-me", headers=headers)
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["data"]}


def _try_link(client, headers, agent_id, cred_id):
    return client.post(
        f"{API}/agents/{agent_id}/credentials",
        headers=headers,
        json={"credential_id": cred_id},
    )


# ── Share operations ────────────────────────────────────────────────────────


def test_group_share_validation_and_listing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner, oh = make_developer(client, su)
    member, mh = make_developer(client, su)
    member2, _ = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"], member2["id"]])
    cred = _shareable(client, oh)

    # Owner only
    assert client.post(
        f"{API}/credentials/{cred['id']}/group-shares",
        headers=mh, json={"group_id": group["id"]},
    ).status_code in (403, 404)

    # Unknown group
    _group_share(client, oh, cred["id"], "00000000-0000-0000-0000-000000000000", 404)

    row = _group_share(client, oh, cred["id"], group["id"])
    assert row["group_id"] == group["id"]
    assert row["group_name"] == group["name"]
    assert row["member_count"] == 2
    assert row["shared_by_user_id"] == owner["id"]

    # Duplicate
    _group_share(client, oh, cred["id"], group["id"], 409)

    r = client.get(f"{API}/credentials/{cred['id']}/group-shares", headers=oh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["data"][0]["group_id"] == group["id"]
    assert body["data"][0]["links_at_risk"] == 0

    # Non-owners cannot list
    assert client.get(
        f"{API}/credentials/{cred['id']}/group-shares", headers=mh
    ).status_code in (403, 404)


def test_group_share_refused_when_sharing_is_off(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    group = create_group(client, su)
    cred = create_random_credential(client, oh, credential_type="api_token")
    assert cred["allow_sharing"] is False
    _group_share(client, oh, cred["id"], group["id"], 400)


# ── Member access ───────────────────────────────────────────────────────────


def test_member_sees_links_and_non_member_cannot(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    member, mh = make_developer(client, su)
    _, outsider_h = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    cred = _shareable(client, oh)
    _group_share(client, oh, cred["id"], group["id"])

    shared = _shared_with_me(client, mh)
    assert cred["id"] in shared
    assert [g["id"] for g in shared[cred["id"]]["via_groups"]] == [group["id"]]
    assert cred["id"] not in _shared_with_me(client, outsider_h)

    m_agent, adapter = _agent_with_adapter(client, mh, patch_environment_adapter, "GS-Member")
    link_credential_to_agent(client, mh, m_agent, cred["id"])
    drain_tasks()
    assert cred["id"] in _linked(client, mh, m_agent)
    assert cred["id"] in _synced(adapter)

    o_agent, _ = _agent_with_adapter(client, outsider_h, patch_environment_adapter, "GS-Outsider")
    r = _try_link(client, outsider_h, o_agent, cred["id"])
    assert r.status_code == 400, r.text
    assert "Not enough permissions" in r.json()["detail"]
    assert cred["id"] not in _linked(client, outsider_h, o_agent)


def test_member_added_later_gets_access_without_new_share(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    from tests.utils.user_group import add_members

    su = superuser_token_headers
    _, oh = make_developer(client, su)
    late, lh = make_developer(client, su)
    group = create_group(client, su)
    cred = _shareable(client, oh)
    _group_share(client, oh, cred["id"], group["id"])
    assert cred["id"] not in _shared_with_me(client, lh)

    add_members(client, su, group["id"], [late["id"]])
    assert cred["id"] in _shared_with_me(client, lh)


# ── Loss of access ──────────────────────────────────────────────────────────


def test_member_removal_unlinks_and_resyncs_only_users_without_other_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    plain, ph = make_developer(client, su)
    direct, dh = make_developer(client, su)
    group = create_group(client, su, member_ids=[plain["id"], direct["id"]])
    cred = _shareable(client, oh)
    share_credential_via_api(client, oh, cred["id"], direct["email"])
    _group_share(client, oh, cred["id"], group["id"])

    p_agent, p_adapter = _agent_with_adapter(client, ph, patch_environment_adapter, "GS-Plain")
    d_agent, d_adapter = _agent_with_adapter(client, dh, patch_environment_adapter, "GS-Direct")
    link_credential_to_agent(client, ph, p_agent, cred["id"])
    link_credential_to_agent(client, dh, d_agent, cred["id"])
    drain_tasks()
    assert cred["id"] in _synced(p_adapter)

    # links_at_risk counts only the member with no other path
    r = client.get(f"{API}/credentials/{cred['id']}/group-shares", headers=oh)
    assert r.json()["data"][0]["links_at_risk"] == 1

    remove_member(client, su, group["id"], plain["id"])
    remove_member(client, su, group["id"], direct["id"])
    drain_tasks()

    assert _linked(client, ph, p_agent) == set()
    assert cred["id"] not in _synced(p_adapter)
    assert cred["id"] not in _shared_with_me(client, ph)

    assert cred["id"] in _linked(client, dh, d_agent), "direct share keeps the link"
    assert cred["id"] in _shared_with_me(client, dh)


def test_revoking_group_share_unlinks_members_but_keeps_other_group_coverage(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    only, oh1 = make_developer(client, su)
    both, oh2 = make_developer(client, su)
    g1 = create_group(client, su, member_ids=[only["id"], both["id"]])
    g2 = create_group(client, su, member_ids=[both["id"]])
    cred = _shareable(client, oh)
    _group_share(client, oh, cred["id"], g1["id"])
    _group_share(client, oh, cred["id"], g2["id"])

    only_agent, only_adapter = _agent_with_adapter(client, oh1, patch_environment_adapter, "GS-Only")
    both_agent, _ = _agent_with_adapter(client, oh2, patch_environment_adapter, "GS-Both")
    link_credential_to_agent(client, oh1, only_agent, cred["id"])
    link_credential_to_agent(client, oh2, both_agent, cred["id"])
    drain_tasks()
    both_listing = _shared_with_me(client, oh2)[cred["id"]]
    assert {g["id"] for g in both_listing["via_groups"]} == {g1["id"], g2["id"]}

    _revoke_group_share(client, oh, cred["id"], g1["id"])

    assert _linked(client, oh1, only_agent) == set()
    assert cred["id"] not in _synced(only_adapter)
    assert cred["id"] in _linked(client, oh2, both_agent)
    assert [g["id"] for g in _shared_with_me(client, oh2)[cred["id"]]["via_groups"]] == [g2["id"]]

    r = client.get(f"{API}/credentials/{cred['id']}/group-shares", headers=oh)
    assert [s["group_id"] for s in r.json()["data"]] == [g2["id"]]


def test_group_revoke_and_member_removal_write_no_revocation_so_resharing_restores_access(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """D7: no ``credential_share_revocation`` row, so nothing blocks re-linking."""
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    member, mh = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    cred = _shareable(client, oh)
    _group_share(client, oh, cred["id"], group["id"])
    agent, _ = _agent_with_adapter(client, mh, patch_environment_adapter, "GS-Rev")
    link_credential_to_agent(client, mh, agent, cred["id"])
    drain_tasks()

    _revoke_group_share(client, oh, cred["id"], group["id"])
    assert _linked(client, mh, agent) == set()

    _group_share(client, oh, cred["id"], group["id"])
    link_credential_to_agent(client, mh, agent, cred["id"])
    assert cred["id"] in _linked(client, mh, agent)

    remove_member(client, su, group["id"], member["id"])
    drain_tasks()
    assert _linked(client, mh, agent) == set()
    from tests.utils.user_group import add_members

    add_members(client, su, group["id"], [member["id"]])
    link_credential_to_agent(client, mh, agent, cred["id"])
    assert cred["id"] in _linked(client, mh, agent)


def test_disabling_sharing_drops_group_shares_and_unlinks_members(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    member, mh = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    cred = _shareable(client, oh)
    _group_share(client, oh, cred["id"], group["id"])
    agent, adapter = _agent_with_adapter(client, mh, patch_environment_adapter, "GS-Off")
    link_credential_to_agent(client, mh, agent, cred["id"])
    drain_tasks()
    assert cred["id"] in _synced(adapter)

    set_credential_sharing(client, oh, cred["id"], False)
    drain_tasks()

    assert _linked(client, mh, agent) == set()
    assert cred["id"] not in _synced(adapter)
    assert cred["id"] not in _shared_with_me(client, mh)
    r = client.get(f"{API}/credentials/{cred['id']}/group-shares", headers=oh)
    assert r.json()["count"] == 0


# ── Deletion impact ─────────────────────────────────────────────────────────


def test_deletion_impact_counts_group_shares_and_members(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    m1, _ = make_developer(client, su)
    m2, _ = make_developer(client, su)
    group = create_group(client, su, member_ids=[m1["id"], m2["id"]])
    cred = _shareable(client, oh)

    r = client.get(f"{API}/credentials/{cred['id']}/deletion-impact", headers=oh)
    assert r.status_code == 200, r.text
    assert r.json()["group_share_count"] == 0

    _group_share(client, oh, cred["id"], group["id"])
    r = client.get(f"{API}/credentials/{cred['id']}/deletion-impact", headers=oh)
    assert r.status_code == 200, r.text
    impact = r.json()
    assert impact["group_share_count"] == 1
    assert impact["group_member_count"] == 2
    assert impact["tier"] == 1
