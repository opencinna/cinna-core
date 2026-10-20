"""Bundle install credentials when bundle access comes ONLY from a user group.

The installer holds no direct catalog grant: ``use`` is granted to a user group
(User Groups P2) and the bundle is ``users``-visible, so the group is the sole
path. Credential handling at install must behave exactly as for a directly
granted installer, and must not depend on the group afterwards.

Scenarios:
  1. Member installs a bundle carrying a PBP ``agent_api`` credential, a PBP
     ``api_token`` credential and a template-mode ``api_token`` credential:
     publisher shares reach the member, the template placeholder is
     materialised and owned by the member, setup-status goes needs_setup ->
     ready after the template is filled, both shared creds and the filled
     template reach the member's env, and the shared agent_api token
     authenticates a consumer proxy call.
  2. An outsider is refused (403).
  3. Removing the member from the group, or revoking the group's bundle grant,
     leaves the install working (install-time shares are direct rows).
  4. PBU spec + ``use_existing`` naming a credential shared with the member's
     GROUP (not directly) links it (``linked_existing``) rather than creating a
     placeholder.
"""
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    make_person,
    put_item_grants,
    set_bundle_visibility,
    user_grant,
)
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.agent_api_keys import setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential,
    install_bundle,
    link_bundle_credential_to_agent,
    publish_bundle,
)
from tests.utils.credential import (
    get_agent_credentials,
    get_credential,
    get_credential_with_data,
    link_credential_to_agent,
    real_credentials_json,
    unlink_credential_from_agent,
    update_credential,
)
from tests.utils.user_group import create_group, remove_member

API = settings.API_V1_STR


# ── Helpers ─────────────────────────────────────────────────────────────────


def _group_grant(group_id: str, access: str = "use") -> dict:
    return {"principal": {"group_id": group_id}, "access": access}


def _grant_group_only(client, publisher, bundle_uuid, group_id) -> None:
    """``users`` visibility + a ``use`` grant to the group; no grant for anyone else."""
    set_bundle_visibility(client, publisher.headers, bundle_uuid, visibility="users")
    put_item_grants(client, publisher.headers, bundle_uuid, [
        user_grant(publisher.id, "maintain"), _group_grant(group_id),
    ])


def _publish_with(client, publisher, name, credential_ids) -> dict:
    agent = create_agent_via_api(client, publisher.headers, name=name)
    drain_tasks()
    for cid in credential_ids:
        link_bundle_credential_to_agent(client, publisher.headers, agent["id"], cid)
    fresh = publish_bundle(client, publisher.headers, agent["id"])
    bundle = client.get(f"{API}/bundles/{fresh['bundle_uuid']}", headers=publisher.headers)
    assert bundle.status_code == 200, bundle.text
    return {
        "bundle_uuid": fresh["bundle_uuid"],
        "bundle_id": bundle.json()["bundle_id"],
    }


def _capture_env(client, headers, install_id, patch_environment_adapter) -> EnvironmentTestAdapter:
    agent = get_agent(client, headers, install_id)
    adapter = EnvironmentTestAdapter()
    previous = patch_environment_adapter.get_adapter
    env_id = agent["active_environment_id"]
    patch_environment_adapter.get_adapter = lambda env: (
        adapter if str(env.id) == env_id else previous(env)
    )
    return adapter


def _synced(adapter) -> dict[str, dict]:
    return {e["id"]: e for e in real_credentials_json(adapter.credentials_set or {})}


def _linked(client, headers, agent_id) -> dict[str, dict]:
    return {c["id"]: c for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _setup_status(client, headers, install_id) -> dict:
    r = client.get(f"{API}/agents/{install_id}/setup-status", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _setup_credentials(client, headers, install_id) -> list[dict]:
    r = client.get(f"{API}/agents/{install_id}/setup-credentials", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _shared_with_me_ids(client, headers) -> set[str]:
    r = client.get(f"{API}/credentials/shared-with-me", headers=headers)
    assert r.status_code == 200, r.text
    return {c["id"] for c in r.json()["data"]}


def _ping(client, producer_id, token):
    return client.get(
        f"{API}/agent-api/{producer_id}/ping",
        headers={"Authorization": f"Bearer {token}"},
    )


class _Published:
    """Everything a publisher-side scenario needs, built through the API."""

    def __init__(self, client, publisher):
        h = publisher.headers
        producer = setup_producer(client, h, name="GrpInst-Producer")
        self.producer_id = producer["id"]
        r = client.post(f"{API}/agents/{self.producer_id}/agent-api/connect", headers=h, json={})
        assert r.status_code == 200, r.text
        self.api_id = r.json()["credential_id"]
        self.token = get_credential_with_data(client, h, self.api_id)["credential_data"]["token"]
        update_credential(client, h, self.api_id, allow_sharing=True)

        tok = create_bundle_credential(client, h, name="grp-pbp-token", allow_sharing=True)
        self.token_id = tok["id"]

        tpl = create_bundle_credential(client, h, name="grp-template-token")
        update_credential(
            client, h, tpl["id"],
            allow_template_sharing=True, template_private_fields=["api_token"],
        )
        self.template_id = tpl["id"]

        pub = _publish_with(
            client, publisher, "GrpInst-Bundle",
            [self.api_id, self.token_id, self.template_id],
        )
        self.bundle_uuid, self.bundle_id = pub["bundle_uuid"], pub["bundle_id"]


# ── Scenario 1: group-only installer, full credential pipeline ──────────────


def test_group_member_install_shares_links_template_and_syncs_credentials(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pub = _Published(client, publisher)
    _grant_group_only(client, publisher, pub.bundle_uuid, group["id"])

    install = install_bundle(client, member.headers, pub.bundle_id)
    install_id = install["id"]

    # Shares publisher -> member for the PBP creds; template placeholder owned by member.
    linked = _linked(client, member.headers, install_id)
    assert {pub.api_id, pub.token_id} <= set(linked)
    assert len(linked) == 3
    placeholder = next(c for cid, c in linked.items() if cid not in (pub.api_id, pub.token_id))
    assert placeholder["is_placeholder"] is True
    assert placeholder["owner_id"] == member.id
    assert placeholder["id"] != pub.template_id
    assert linked[pub.api_id]["owner_id"] == publisher.id
    assert {pub.api_id, pub.token_id} <= _shared_with_me_ids(client, member.headers)
    assert client.get(f"{API}/credentials/{pub.api_id}", headers=member.headers).status_code == 200

    # Readiness: needs_setup until the template placeholder is filled.
    status = _setup_status(client, member.headers, install_id)
    assert status["status"] == "needs_setup"
    assert "placeholder_empty" in [m["reason"] for m in status["missing"]]
    entries = _setup_credentials(client, member.headers, install_id)
    assert [e["id"] for e in entries] == [placeholder["id"]]

    r = client.put(
        f"{API}/agents/{install_id}/setup-credentials/{placeholder['id']}",
        headers=member.headers,
        json={"credential_data": {
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": "member-own-token",
        }},
    )
    assert r.status_code == 200, r.text
    assert get_credential(client, member.headers, placeholder["id"])["is_placeholder"] is False
    assert _setup_status(client, member.headers, install_id)["status"] == "ready"

    # Env sync: agent_api {base_url, token}, PBP api_token, and the member's template.
    adapter = _capture_env(client, member.headers, install_id, patch_environment_adapter)
    unlink_credential_from_agent(client, member.headers, install_id, pub.token_id)
    drain_tasks()
    link_credential_to_agent(client, member.headers, install_id, pub.token_id)
    drain_tasks()
    synced = _synced(adapter)
    assert {pub.api_id, pub.token_id, placeholder["id"]} <= set(synced)
    api_data = synced[pub.api_id]["credential_data"]
    assert api_data["token"] == pub.token
    assert urlsplit(api_data["base_url"]).netloc == urlsplit(settings.AGENT_ENV_BACKEND_URL).netloc
    assert pub.producer_id in api_data["base_url"]
    assert synced[pub.token_id]["type"] == "api_token"
    assert "member-own-token" in str(synced[placeholder["id"]]["credential_data"])

    # Consumer proxy call with the shared agent_api token authenticates.
    r = _ping(client, pub.producer_id, api_data["token"])
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True


# ── Scenario 2: outsider ────────────────────────────────────────────────────


def test_non_member_cannot_install_group_only_bundle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    outsider = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pub = _Published(client, publisher)
    _grant_group_only(client, publisher, pub.bundle_uuid, group["id"])

    install_bundle(client, outsider.headers, pub.bundle_id, expected_status=403)
    # nothing leaked to the outsider
    assert not ({pub.api_id, pub.token_id} & _shared_with_me_ids(client, outsider.headers))
    assert client.get(f"{API}/credentials/{pub.api_id}", headers=outsider.headers).status_code in (400, 403, 404)


# ── Scenario 3: install outlives group membership / group grant ─────────────


def _assert_install_still_healthy(client, member, install_id, pub, adapter) -> None:
    linked = _linked(client, member.headers, install_id)
    assert {pub.api_id, pub.token_id} <= set(linked)
    assert len(linked) == 3
    assert _setup_status(client, member.headers, install_id)["status"] == "ready"
    assert {pub.api_id, pub.token_id} <= _shared_with_me_ids(client, member.headers)
    assert client.get(f"{API}/agents/{install_id}", headers=member.headers).status_code == 200

    # A fresh sync still carries every credential.
    unlink_credential_from_agent(client, member.headers, install_id, pub.token_id)
    drain_tasks()
    link_credential_to_agent(client, member.headers, install_id, pub.token_id)
    drain_tasks()
    synced = _synced(adapter)
    assert {pub.api_id, pub.token_id} <= set(synced)
    assert synced[pub.api_id]["credential_data"]["token"] == pub.token


def _install_ready(client, member, pub):
    install = install_bundle(client, member.headers, pub.bundle_id)
    placeholder_id = _setup_credentials(client, member.headers, install["id"])[0]["id"]
    r = client.put(
        f"{API}/agents/{install['id']}/setup-credentials/{placeholder_id}",
        headers=member.headers,
        json={"credential_data": {
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": "member-own-token",
        }},
    )
    assert r.status_code == 200, r.text
    assert _setup_status(client, member.headers, install["id"])["status"] == "ready"
    return install["id"]


def test_install_keeps_working_after_member_removed_from_group(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pub = _Published(client, publisher)
    _grant_group_only(client, publisher, pub.bundle_uuid, group["id"])
    install_id = _install_ready(client, member, pub)
    adapter = _capture_env(client, member.headers, install_id, patch_environment_adapter)

    remove_member(client, su, group["id"], member.id)
    drain_tasks()

    # The bundle itself is gone for the member ...
    assert client.get(f"{API}/bundles/{pub.bundle_uuid}", headers=member.headers).status_code != 200
    # ... but the install and its credentials are not.
    _assert_install_still_healthy(client, member, install_id, pub, adapter)


def test_install_keeps_working_after_group_bundle_grant_revoked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])
    pub = _Published(client, publisher)
    _grant_group_only(client, publisher, pub.bundle_uuid, group["id"])
    install_id = _install_ready(client, member, pub)
    adapter = _capture_env(client, member.headers, install_id, patch_environment_adapter)

    put_item_grants(client, publisher.headers, pub.bundle_uuid, [
        user_grant(publisher.id, "maintain"),
    ])
    drain_tasks()

    install_bundle(client, member.headers, pub.bundle_id, expected_status=403)
    _assert_install_still_healthy(client, member, install_id, pub, adapter)


# ── Scenario 4: use_existing with a group-shared credential ─────────────────


def test_use_existing_links_credential_shared_with_members_group(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """KNOWN BUG until ``CredentialProvisioner._installer_can_see`` honours group shares:
    a credential the member can reach through a CredentialGroupShare is treated as
    invisible and a placeholder is created instead of ``linked_existing``."""
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su)
    third = make_person(client, su)
    group = create_group(client, su, member_ids=[member.id])

    # PBU spec: the publisher's credential is not shareable.
    spec_name = "grp-pbu-token"
    pbu = create_bundle_credential(client, publisher.headers, name=spec_name, allow_sharing=False)
    pub = _publish_with(client, publisher, "GrpInst-PBU", [pbu["id"]])
    set_bundle_visibility(client, publisher.headers, pub["bundle_uuid"], visibility="users")
    put_item_grants(client, publisher.headers, pub["bundle_uuid"], [
        user_grant(publisher.id, "maintain"), _group_grant(group["id"]),
    ])

    # A third user's credential, reachable by the member ONLY via the group.
    shared = create_bundle_credential(client, third.headers, name=spec_name, allow_sharing=True)
    r = client.post(
        f"{API}/credentials/{shared['id']}/group-shares",
        headers=third.headers, json={"group_id": group["id"]},
    )
    assert r.status_code == 200, r.text
    assert shared["id"] in _shared_with_me_ids(client, member.headers)

    install = install_bundle(
        client, member.headers, pub["bundle_id"],
        request_body={"credentials": {
            spec_name: {"mode": "use_existing", "credential_id": shared["id"]},
        }},
    )

    linked = _linked(client, member.headers, install["id"])
    assert list(linked) == [shared["id"]], (
        f"expected the group-shared credential to be linked, got {linked}"
    )
    assert linked[shared["id"]]["is_placeholder"] is False
    assert linked[shared["id"]]["owner_id"] == third.id
    assert _setup_status(client, member.headers, install["id"])["status"] == "ready"
