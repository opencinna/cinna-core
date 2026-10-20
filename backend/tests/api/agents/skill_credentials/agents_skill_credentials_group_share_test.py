"""Cloud skill install when the publisher credential reaches the installer via a group.

The installer's direct share was removed by the credential owner (a
revocation), but a user-group share remains: the install links the publisher
credential through the group, writes no direct share, and the link goes away
with the group membership / group share. A non-member stays on
``publisher_unavailable``.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    link_credential_to_agent,
    share_credential_via_api,
    update_credential,
)
from tests.utils.skill_catalog import (
    get_skill_install_preview,
    install_skill,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    publish_skill,
    write_skill_with_credentials,
)
from tests.utils.user_group import create_group, remove_member

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


def _shares(client, headers, cid) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=headers)
    assert r.status_code == 200, r.text
    return {s["shared_with_user_id"]: s for s in r.json()["data"]}


def _linked(client, headers, agent_id) -> set[str]:
    return {c["id"] for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _setup(client, su, patch_environment_adapter):
    _, pub_headers = make_developer(client, su)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "SkGrp-Publisher")
    cred = create_random_credential(client, pub_headers, credential_type="api_token")
    cid = cred["id"]
    update_credential(client, pub_headers, cid, service_uri="slot-grp", allow_sharing=True)
    link_credential_to_agent(client, pub_headers, pub_agent, cid)
    write_skill_with_credentials(pub_env, "grp-skill", [{"slot": "slot-grp", "type": "api_token"}])
    package_uuid = publish_skill(
        client, pub_headers, pub_agent, "grp-skill", visibility="public"
    )["package_id"]
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    patch_environment_adapter.get_adapter = lambda env: adapter
    return pub_headers, cid, package_uuid


def _revoke_direct(client, pub_headers, cid, user) -> None:
    share_credential_via_api(client, pub_headers, cid, user["email"])
    share = _shares(client, pub_headers, cid)[user["id"]]
    r = client.delete(f"{API}/credentials/{cid}/shares/{share['id']}", headers=pub_headers)
    assert r.status_code == 200, r.text


def _group_share(client, headers, cid, group_id) -> None:
    r = client.post(
        f"{API}/credentials/{cid}/group-shares", headers=headers, json={"group_id": group_id}
    )
    assert r.status_code == 200, r.text


def test_revoked_installer_with_group_share_links_publisher_credential_via_group(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. B's direct share revoked; B's group also holds a share -> preview and
       install report ``linked_publisher``; no direct share for B
    2. Non-member C (also revoked, no group share) -> ``publisher_unavailable``
    3. B leaves the group -> the link is dropped
    """
    su = superuser_token_headers
    pub_headers, cid, package_uuid = _setup(client, su, patch_environment_adapter)
    b, b_headers = make_developer(client, su)
    c, c_headers = make_developer(client, su)
    group = create_group(client, su, member_ids=[b["id"]])
    _revoke_direct(client, pub_headers, cid, b)
    _revoke_direct(client, pub_headers, cid, c)
    _group_share(client, pub_headers, cid, group["id"])
    b_agent, _ = make_agent_with_env(client, b_headers, "SkGrp-B")
    c_agent, _ = make_agent_with_env(client, c_headers, "SkGrp-C")

    preview = get_skill_install_preview(client, b_headers, b_agent, package_uuid)
    assert preview["credentials"][0]["outcome"] == "linked_publisher"
    prov = install_skill(client, b_headers, b_agent, package_uuid)["credential_provisioning"][0]
    assert (prov["outcome"], prov["credential_id"]) == ("linked_publisher", cid)
    assert cid in _linked(client, b_headers, b_agent)
    assert _shares(client, pub_headers, cid) == {}

    c_prov = install_skill(client, c_headers, c_agent, package_uuid)["credential_provisioning"][0]
    assert c_prov["outcome"] == "publisher_unavailable"
    assert c_prov["credential_id"] != cid

    remove_member(client, su, group["id"], b["id"])
    drain_tasks()
    assert cid not in _linked(client, b_headers, b_agent)
    assert _shares(client, pub_headers, cid) == {}
