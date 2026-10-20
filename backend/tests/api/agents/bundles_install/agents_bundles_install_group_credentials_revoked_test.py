"""Bundle install credentials: revoked direct share, group share still held.

Complements ``agents_bundles_install_group_credentials_test.py``.

Scenarios:
  1. The publisher revoked the installer's direct share but a group share
     remains: the install links the publisher credential through the group and
     writes NO direct share; removing the member from the group, or removing
     the group share, drops the link.
  2. ``use_existing`` naming a group-shared credential: a non-member of that
     group gets a placeholder, never the credential, and no share is created.
  3. An issued ``agent_api`` key is identity-bound and cannot be group-shared.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.agents.bundles_install.agents_bundles_install_group_credentials_test import (
    _Published,
    _group_grant,
    _grant_group_only,
    _linked,
    _publish_with,
    _shared_with_me_ids,
)
from tests.api.catalog.catalog_helpers import (
    make_person,
    put_item_grants,
    set_bundle_visibility,
    user_grant,
)
from tests.utils.agent_api_keys import setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import create_bundle_credential, install_bundle
from tests.utils.credential import share_credential_via_api
from tests.utils.user_group import create_group, remove_member

API = settings.API_V1_STR


def _direct_shares(client, headers, cid) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=headers)
    assert r.status_code == 200, r.text
    return {s["shared_with_user_id"]: s for s in r.json()["data"]}


def _group_share(client, headers, cid, group_id) -> None:
    r = client.post(
        f"{API}/credentials/{cid}/group-shares", headers=headers, json={"group_id": group_id}
    )
    assert r.status_code == 200, r.text


def _revoke_direct_share(client, owner_headers, cid, member) -> None:
    """Direct-share then remove it: leaves a revocation record for the member."""
    share_credential_via_api(client, owner_headers, cid, member.email)
    share = _direct_shares(client, owner_headers, cid)[member.id]
    r = client.delete(f"{API}/credentials/{cid}/shares/{share['id']}", headers=owner_headers)
    assert r.status_code == 200, r.text


def _setup_revoked_plus_group(client, su):
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pub = _Published(client, publisher)
    _grant_group_only(client, publisher, pub.bundle_uuid, group["id"])
    _revoke_direct_share(client, publisher.headers, pub.token_id, member)
    _group_share(client, publisher.headers, pub.token_id, group["id"])
    return publisher, member, group, pub


# ── Scenario 1: revoked direct share + remaining group share ────────────────


def test_revoked_direct_share_with_group_share_links_via_group_without_direct_share(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher, member, group, pub = _setup_revoked_plus_group(client, su)

    install = install_bundle(client, member.headers, pub.bundle_id)

    linked = _linked(client, member.headers, install["id"])
    assert pub.token_id in linked
    assert linked[pub.token_id]["is_placeholder"] is False
    assert linked[pub.token_id]["owner_id"] == publisher.id
    # No durable direct row for the revoked credential; others still get one.
    assert member.id not in _direct_shares(client, publisher.headers, pub.token_id)
    assert member.id in _direct_shares(client, publisher.headers, pub.api_id)


def test_group_linked_credential_dropped_when_member_leaves_group(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher, member, group, pub = _setup_revoked_plus_group(client, su)
    install = install_bundle(client, member.headers, pub.bundle_id)
    assert pub.token_id in _linked(client, member.headers, install["id"])

    remove_member(client, su, group["id"], member.id)
    drain_tasks()

    assert pub.token_id not in _linked(client, member.headers, install["id"])
    assert pub.token_id not in _shared_with_me_ids(client, member.headers)
    assert member.id not in _direct_shares(client, publisher.headers, pub.token_id)
    # The non-revoked credential keeps its durable direct share and link.
    assert pub.api_id in _linked(client, member.headers, install["id"])


def test_group_linked_credential_dropped_when_group_share_removed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher, member, group, pub = _setup_revoked_plus_group(client, su)
    install = install_bundle(client, member.headers, pub.bundle_id)
    assert pub.token_id in _linked(client, member.headers, install["id"])

    r = client.delete(
        f"{API}/credentials/{pub.token_id}/group-shares/{group['id']}",
        headers=publisher.headers,
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    assert pub.token_id not in _linked(client, member.headers, install["id"])
    assert member.id not in _direct_shares(client, publisher.headers, pub.token_id)


def test_revoked_direct_share_without_group_share_still_refuses_install_link(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pub = _Published(client, publisher)
    _grant_group_only(client, publisher, pub.bundle_uuid, group["id"])
    _revoke_direct_share(client, publisher.headers, pub.token_id, member)

    install = install_bundle(client, member.headers, pub.bundle_id)

    assert pub.token_id not in _linked(client, member.headers, install["id"])
    assert member.id not in _direct_shares(client, publisher.headers, pub.token_id)


# ── Scenario 2: non-member cannot use_existing a group-shared credential ────


def test_non_member_use_existing_of_group_shared_credential_gets_placeholder(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    owner = make_person(client, su)
    member = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    other_group = create_group(client, su, member_ids=[outsider.id])

    spec_name = "grp-neg-token"
    pbu = create_bundle_credential(client, publisher.headers, name=spec_name, allow_sharing=False)
    pub = _publish_with(client, publisher, "GrpNeg-PBU", [pbu["id"]])
    set_bundle_visibility(client, publisher.headers, pub["bundle_uuid"], visibility="users")
    put_item_grants(client, publisher.headers, pub["bundle_uuid"], [
        user_grant(publisher.id, "maintain"),
        _group_grant(group["id"]), _group_grant(other_group["id"]),
    ])
    shared = create_bundle_credential(client, owner.headers, name=spec_name, allow_sharing=True)
    _group_share(client, owner.headers, shared["id"], group["id"])
    body = {"credentials": {spec_name: {"mode": "use_existing", "credential_id": shared["id"]}}}

    ok = install_bundle(client, member.headers, pub["bundle_id"], request_body=body)
    assert list(_linked(client, member.headers, ok["id"])) == [shared["id"]]

    r = client.post(f"{API}/catalog/{pub['bundle_id']}/install", headers=outsider.headers, json=body)
    if r.status_code == 200:
        drain_tasks()
        linked = _linked(client, outsider.headers, r.json()["id"])
        assert shared["id"] not in linked
        assert all(c["is_placeholder"] for c in linked.values())
    else:
        assert r.status_code in (400, 403, 404), r.text
    assert shared["id"] not in _shared_with_me_ids(client, outsider.headers)
    assert outsider.id not in _direct_shares(client, owner.headers, shared["id"])


# ── Scenario 3: issued agent_api key can never be group-shared ──────────────


def test_issued_agent_api_key_is_identity_bound_and_cannot_be_group_shared(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Issued keys are identity-bound: sharing is refused, so a group member can
    neither see nor ``use_existing`` one."""
    su = superuser_token_headers
    recipient = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    producer = setup_producer(client, su, name="GrpIssued-Producer")
    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/keys",
        headers=su,
        json={"issued_to_user_id": recipient.id, "label": "grp-key"},
    )
    assert r.status_code == 200, r.text
    key_cid = r.json()["credential_id"]

    r = client.put(
        f"{API}/credentials/{key_cid}", headers=recipient.headers, json={"allow_sharing": True}
    )
    assert r.status_code == 400, r.text
    r = client.post(
        f"{API}/credentials/{key_cid}/group-shares",
        headers=recipient.headers, json={"group_id": group["id"]},
    )
    assert r.status_code >= 400, r.text
    assert key_cid not in _shared_with_me_ids(client, member.headers)
