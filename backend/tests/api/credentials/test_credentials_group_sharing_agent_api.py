"""Group credential shares for ``agent_api`` and custom ``api_token`` credentials.

Complements ``test_credentials_group_sharing.py`` (generic mechanics) with the
two credential types whose env payload and proxy behaviour differ:

  * ``agent_api`` -- a connection token minted for a producer agent. The env
    payload carries ``{base_url, token}`` with the URL rewritten to the internal
    backend origin; a consumer proxy call authenticates on the token.
  * ``api_token`` -- a plain custom token credential.

Scenarios:
  1. Owner group-shares both; a member sees them in shared-with-me, links them
     to their own agent and both reach the env; a non-member cannot link.
  2. A member's agent holds the group-shared agent_api token and the consumer
     proxy call authenticates; it keeps authenticating after member removal
     (the token is a bearer secret; the group path gates linking, not the
     producer).
  3. Member removal drops links + re-syncs the env; a member with a direct
     share keeps both.
  4. Revoking the group share does the same; re-sharing restores.
"""
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.agent_api_keys import setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    get_credential_with_data,
    link_credential_to_agent,
    real_credentials_json,
    share_credential_via_api,
    update_credential,
)
from tests.utils.skill_catalog import make_developer
from tests.utils.user_group import create_group, remove_member

API = settings.API_V1_STR


# ── Helpers ─────────────────────────────────────────────────────────────────


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


def _synced_entries(adapter: EnvironmentTestAdapter) -> dict[str, dict]:
    return {e["id"]: e for e in real_credentials_json(adapter.credentials_set or {})}


def _linked(client, headers, agent_id) -> set[str]:
    return {c["id"] for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _group_share(client, headers, cred_id, group_id, expect=200):
    r = client.post(
        f"{API}/credentials/{cred_id}/group-shares",
        headers=headers,
        json={"group_id": group_id},
    )
    assert r.status_code == expect, r.text
    return r.json()


def _revoke_group_share(client, headers, cred_id, group_id):
    r = client.delete(f"{API}/credentials/{cred_id}/group-shares/{group_id}", headers=headers)
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


def _owner_with_credentials(client, su):
    """Owner, a producer agent, a shareable agent_api credential and a shareable api_token."""
    owner, oh = make_developer(client, su)
    producer = setup_producer(client, oh, name="GS-AgentApi-Producer")
    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect", headers=oh, json={})
    assert r.status_code == 200, r.text
    conn = r.json()
    api_cred_id = conn["credential_id"]
    token = get_credential_with_data(client, oh, api_cred_id)["credential_data"]["token"]
    update_credential(client, oh, api_cred_id, allow_sharing=True)
    tok = create_random_credential(client, oh, credential_type="api_token")
    update_credential(client, oh, tok["id"], allow_sharing=True)
    return owner, oh, producer["id"], api_cred_id, token, tok["id"]


def _ping(client, producer_id, token):
    return client.get(
        f"{API}/agent-api/{producer_id}/ping",
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Scenario 1: member access, env sync, non-member refusal ─────────────────


def test_group_shared_agent_api_and_api_token_link_and_sync_for_member_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh, producer_id, api_id, token, tok_id = _owner_with_credentials(client, su)
    member, mh = make_developer(client, su)
    _, outsider_h = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    _group_share(client, oh, api_id, group["id"])
    _group_share(client, oh, tok_id, group["id"])

    shared = _shared_with_me(client, mh)
    assert {api_id, tok_id} <= set(shared)
    assert shared[api_id]["type"] == "agent_api"
    assert [g["id"] for g in shared[api_id]["via_groups"]] == [group["id"]]
    assert not ({api_id, tok_id} & set(_shared_with_me(client, outsider_h)))

    m_agent, adapter = _agent_with_adapter(client, mh, patch_environment_adapter, "GS-API-Member")
    link_credential_to_agent(client, mh, m_agent, api_id)
    link_credential_to_agent(client, mh, m_agent, tok_id)
    drain_tasks()
    assert _linked(client, mh, m_agent) == {api_id, tok_id}

    synced = _synced_entries(adapter)
    assert set(synced) == {api_id, tok_id}
    api_data = synced[api_id]["credential_data"]
    assert api_data["token"] == token
    assert urlsplit(api_data["base_url"]).netloc == urlsplit(settings.AGENT_ENV_BACKEND_URL).netloc
    assert producer_id in api_data["base_url"]
    assert synced[tok_id]["type"] == "api_token"

    o_agent, _ = _agent_with_adapter(client, outsider_h, patch_environment_adapter, "GS-API-Outsider")
    for cid in (api_id, tok_id):
        r = _try_link(client, outsider_h, o_agent, cid)
        assert r.status_code == 400, r.text
        assert "Not enough permissions" in r.json()["detail"]
    assert _linked(client, outsider_h, o_agent) == set()


# ── Scenario 2: consumer proxy call ─────────────────────────────────────────


def test_group_shared_agent_api_token_authenticates_consumer_proxy_call(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh, producer_id, api_id, _token, _ = _owner_with_credentials(client, su)
    member, mh = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    _group_share(client, oh, api_id, group["id"])

    m_agent, adapter = _agent_with_adapter(client, mh, patch_environment_adapter, "GS-API-Proxy")
    link_credential_to_agent(client, mh, m_agent, api_id)
    drain_tasks()

    # The token the member's env received (not the owner's copy) is what calls the producer.
    env_token = _synced_entries(adapter)[api_id]["credential_data"]["token"]
    r = _ping(client, producer_id, env_token)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    # A tampered token is rejected.
    assert _ping(client, producer_id, env_token + "x").status_code in (401, 403)

    # Linking is gated by the group; the bearer token itself is not.
    remove_member(client, su, group["id"], member["id"])
    drain_tasks()
    assert _linked(client, mh, m_agent) == set()
    assert _ping(client, producer_id, env_token).status_code == 200


# ── Scenario 3: member removal ──────────────────────────────────────────────


def test_member_removal_drops_links_and_resyncs_env_but_direct_share_keeps_both(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh, _pid, api_id, _tok, tok_id = _owner_with_credentials(client, su)
    plain, ph = make_developer(client, su)
    direct, dh = make_developer(client, su)
    group = create_group(client, su, member_ids=[plain["id"], direct["id"]])
    share_credential_via_api(client, oh, api_id, direct["email"])
    share_credential_via_api(client, oh, tok_id, direct["email"])
    _group_share(client, oh, api_id, group["id"])
    _group_share(client, oh, tok_id, group["id"])

    p_agent, p_adapter = _agent_with_adapter(client, ph, patch_environment_adapter, "GS-API-Plain")
    d_agent, d_adapter = _agent_with_adapter(client, dh, patch_environment_adapter, "GS-API-Direct")
    for h, a in ((ph, p_agent), (dh, d_agent)):
        link_credential_to_agent(client, h, a, api_id)
        link_credential_to_agent(client, h, a, tok_id)
    drain_tasks()
    assert set(_synced_entries(p_adapter)) == {api_id, tok_id}

    remove_member(client, su, group["id"], plain["id"])
    remove_member(client, su, group["id"], direct["id"])
    drain_tasks()

    assert _linked(client, ph, p_agent) == set()
    assert _synced_entries(p_adapter) == {}
    assert not ({api_id, tok_id} & set(_shared_with_me(client, ph)))

    assert _linked(client, dh, d_agent) == {api_id, tok_id}
    assert set(_synced_entries(d_adapter)) == {api_id, tok_id}
    assert {api_id, tok_id} <= set(_shared_with_me(client, dh))


# ── Scenario 4: group-share revoke and re-share ─────────────────────────────


def test_group_share_revoke_unlinks_and_resharing_restores(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    su = superuser_token_headers
    _, oh, _pid, api_id, _tok, tok_id = _owner_with_credentials(client, su)
    member, mh = make_developer(client, su)
    direct, dh = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"], direct["id"]])
    share_credential_via_api(client, oh, api_id, direct["email"])
    share_credential_via_api(client, oh, tok_id, direct["email"])
    _group_share(client, oh, api_id, group["id"])
    _group_share(client, oh, tok_id, group["id"])

    m_agent, m_adapter = _agent_with_adapter(client, mh, patch_environment_adapter, "GS-API-Rev-M")
    d_agent, d_adapter = _agent_with_adapter(client, dh, patch_environment_adapter, "GS-API-Rev-D")
    for h, a in ((mh, m_agent), (dh, d_agent)):
        link_credential_to_agent(client, h, a, api_id)
        link_credential_to_agent(client, h, a, tok_id)
    drain_tasks()

    _revoke_group_share(client, oh, api_id, group["id"])
    _revoke_group_share(client, oh, tok_id, group["id"])

    assert _linked(client, mh, m_agent) == set()
    assert _synced_entries(m_adapter) == {}
    assert _linked(client, dh, d_agent) == {api_id, tok_id}
    assert set(_synced_entries(d_adapter)) == {api_id, tok_id}

    # D7: no revocation record, so re-sharing restores the ability to link.
    _group_share(client, oh, api_id, group["id"])
    _group_share(client, oh, tok_id, group["id"])
    link_credential_to_agent(client, mh, m_agent, api_id)
    link_credential_to_agent(client, mh, m_agent, tok_id)
    drain_tasks()
    assert _linked(client, mh, m_agent) == {api_id, tok_id}
    assert set(_synced_entries(m_adapter)) == {api_id, tok_id}
