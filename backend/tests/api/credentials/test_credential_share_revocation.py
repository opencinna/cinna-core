"""Revoking a share also unlinks the recipient's agents (and re-syncs them).

A ``CredentialShare`` is the recipient's *right* to use a credential, but
nothing downstream re-checks it: ``get_agent_credentials`` joins
``AgentCredentialLink`` alone, so an agent linked to a shared credential keeps
receiving its value into every running container for as long as the link
exists. Deleting only the share row therefore revoked access on the credential
page while the recipient's container went on holding a working secret — found
by running the skill-credential scenario end to end on a dev stack
(docs/plans/skill_credential_requirements_plan.md §15, "Manual scenario run").

All revocation entry points now delete the recipient's links and sync their
environments:
  1. ``DELETE /credentials/{id}/shares/{share_id}`` — one recipient.
  2. ``PATCH /credentials/{id}/sharing`` with ``allow_sharing=false`` — all of
     them, while the owner's own links stay untouched.
  3. ``PUT /credentials/{id}`` with ``allow_sharing=false`` — the generic
     update path has the same revocation semantics as the dedicated control.

The env assertions use the adapter-capture pattern of
``test_credential_service_uri_env_sync.py``.
Legacy shares surviving an already-disabled flag are covered in
``tests/unit/test_credential_revocation_legacy.py``.
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

API = settings.API_V1_STR


def _agent_with_adapter(
    client: TestClient, headers: dict[str, str], patch_environment_adapter, name: str,
) -> tuple[str, EnvironmentTestAdapter]:
    """An agent whose env-sync payloads land in a per-agent adapter."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None
    adapter = EnvironmentTestAdapter()
    previous = patch_environment_adapter.get_adapter
    env_id = agent["active_environment_id"]
    patch_environment_adapter.get_adapter = lambda env: (
        adapter if str(env.id) == env_id else previous(env)
    )
    return agent["id"], adapter


def _synced_credential_ids(adapter: EnvironmentTestAdapter) -> set[str]:
    return {
        entry["id"] for entry in real_credentials_json(adapter.credentials_set or {})
    }


def _linked_credential_ids(
    client: TestClient, headers: dict[str, str], agent_id: str,
) -> set[str]:
    return {
        cred["id"] for cred in get_agent_credentials(client, headers, agent_id)["data"]
    }


def _shareable_credential(client: TestClient, headers: dict[str, str]) -> dict:
    cred = create_random_credential(client, headers, credential_type="api_token")
    return update_credential(client, headers, cred["id"], allow_sharing=True)


def test_revoking_one_share_unlinks_and_resyncs_that_recipient(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    owner, owner_headers = make_developer(client, superuser_token_headers)
    recipient, recipient_headers = make_developer(client, superuser_token_headers)

    cred = _shareable_credential(client, owner_headers)
    share = share_credential_via_api(
        client, owner_headers, cred["id"], recipient["email"],
    )

    agent_id, adapter = _agent_with_adapter(
        client, recipient_headers, patch_environment_adapter, "Revocation-Recipient",
    )
    link_credential_to_agent(client, recipient_headers, agent_id, cred["id"])
    drain_tasks()
    assert cred["id"] in _synced_credential_ids(adapter)

    r = client.delete(
        f"{API}/credentials/{cred['id']}/shares/{share['id']}",
        headers=owner_headers,
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    assert _linked_credential_ids(client, recipient_headers, agent_id) == set()
    assert cred["id"] not in _synced_credential_ids(adapter), (
        "the recipient's container must lose the credential on revocation"
    )


def test_disabling_sharing_unlinks_every_recipient_but_not_the_owner(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    owner, owner_headers = make_developer(client, superuser_token_headers)
    recipient, recipient_headers = make_developer(client, superuser_token_headers)

    cred = _shareable_credential(client, owner_headers)
    share_credential_via_api(client, owner_headers, cred["id"], recipient["email"])

    owner_agent, owner_adapter = _agent_with_adapter(
        client, owner_headers, patch_environment_adapter, "Revocation-Owner",
    )
    link_credential_to_agent(client, owner_headers, owner_agent, cred["id"])
    recipient_agent, recipient_adapter = _agent_with_adapter(
        client, recipient_headers, patch_environment_adapter, "Revocation-Consumer",
    )
    link_credential_to_agent(client, recipient_headers, recipient_agent, cred["id"])
    drain_tasks()

    set_credential_sharing(client, owner_headers, cred["id"], False)
    drain_tasks()

    assert _linked_credential_ids(client, recipient_headers, recipient_agent) == set()
    assert cred["id"] not in _synced_credential_ids(recipient_adapter)

    assert cred["id"] in _linked_credential_ids(client, owner_headers, owner_agent), (
        "the owner's own link is not a share and must survive"
    )
    assert cred["id"] in _synced_credential_ids(owner_adapter)


def test_generic_update_disabling_sharing_unlinks_and_resyncs_recipients(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """The generic update cannot leave a stale, usable shared credential."""
    owner, owner_headers = make_developer(client, superuser_token_headers)
    recipient, recipient_headers = make_developer(client, superuser_token_headers)

    cred = _shareable_credential(client, owner_headers)
    share_credential_via_api(
        client, owner_headers, cred["id"], recipient["email"],
    )
    recipient_agent, recipient_adapter = _agent_with_adapter(
        client, recipient_headers, patch_environment_adapter, "Generic-Revocation-Consumer",
    )
    link_credential_to_agent(client, recipient_headers, recipient_agent, cred["id"])
    drain_tasks()
    assert cred["id"] in _synced_credential_ids(recipient_adapter)

    update_credential(client, owner_headers, cred["id"], allow_sharing=False)
    drain_tasks()

    assert _linked_credential_ids(client, recipient_headers, recipient_agent) == set()
    assert cred["id"] not in _synced_credential_ids(recipient_adapter)
    shares = client.get(
        f"{API}/credentials/{cred['id']}/shares", headers=owner_headers,
    )
    assert shares.status_code == 200, shares.text
    assert shares.json()["count"] == 0


def test_revoking_a_direct_share_keeps_the_link_while_a_group_still_covers_the_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """D8: loss of access is computed. A group share still covers the recipient,
    so revoking the direct share neither unlinks nor drops the credential from
    the container; removing the member afterwards does."""
    from tests.utils.user_group import create_group, remove_member

    su = superuser_token_headers
    owner, owner_headers = make_developer(client, su)
    recipient, recipient_headers = make_developer(client, su)
    group = create_group(client, su, member_ids=[recipient["id"]])

    cred = _shareable_credential(client, owner_headers)
    share = share_credential_via_api(client, owner_headers, cred["id"], recipient["email"])
    r = client.post(
        f"{API}/credentials/{cred['id']}/group-shares",
        headers=owner_headers, json={"group_id": group["id"]},
    )
    assert r.status_code == 200, r.text

    agent_id, adapter = _agent_with_adapter(
        client, recipient_headers, patch_environment_adapter, "Revocation-GroupCovered",
    )
    link_credential_to_agent(client, recipient_headers, agent_id, cred["id"])
    drain_tasks()

    r = client.delete(
        f"{API}/credentials/{cred['id']}/shares/{share['id']}", headers=owner_headers,
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    assert cred["id"] in _linked_credential_ids(client, recipient_headers, agent_id)
    assert cred["id"] in _synced_credential_ids(adapter)

    remove_member(client, su, group["id"], recipient["id"])
    drain_tasks()
    assert _linked_credential_ids(client, recipient_headers, agent_id) == set()
    assert cred["id"] not in _synced_credential_ids(adapter)
