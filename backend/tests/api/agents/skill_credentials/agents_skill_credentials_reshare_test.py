"""Cloud skill publish/install of a connection the publisher only holds a share of (P1, plan §2.6).

S owns an ``agent_api`` connection and shares it with T. Once S allows re-use,
T's skill can ship it as publisher-provided: the publish preview offers it,
U's install links it (share authored by T), and S turning re-use off releases it.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent_api_keys import setup_producer
from tests.utils.credential import (
    get_agent_credentials,
    link_credential_to_agent,
    share_credential_via_api,
    update_credential,
)
from tests.utils.skill_catalog import (
    install_skill,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    preview_skill_publish,
    publish_skill,
    write_skill_with_credentials,
)

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


def _linked(client, headers, agent_id) -> set[str]:
    return {c["id"] for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _shares(client, headers, cid) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=headers)
    assert r.status_code == 200, r.text
    return {s["shared_with_user_id"]: s for s in r.json()["data"]}


def test_reshared_connection_skill_publish_install_and_release(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. S shares a connection with T; re-use off: the skill publish preview
       does not offer it as publisher-provided
    2. S enables re-use: T publishes; the revision records ``publisher``
    3. U installs: the connection is linked, share authored by T, ``skill_install``
    4. S turns re-use off: U's link and share are released
    """
    su = superuser_token_headers
    s, sh = make_developer(client, su)
    t, th = make_developer(client, su)
    u, uh = make_developer(client, su)
    producer = setup_producer(client, sh, name="SkillReshare-Producer")
    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect", headers=sh, json={})
    assert r.status_code == 200, r.text
    cid = r.json()["credential_id"]
    update_credential(client, sh, cid, allow_sharing=True, service_uri="slot-reshare")
    share_credential_via_api(client, sh, cid, t["email"])

    t_agent, t_env = make_agent_with_env(client, th, "SkillReshare-T")
    link_credential_to_agent(client, th, t_agent, cid)
    write_skill_with_credentials(
        t_env, "reshare-skill", [{"slot": "slot-reshare", "type": "agent_api"}]
    )

    # ── Phase 1: re-use off ──────────────────────────────────────────
    preview = preview_skill_publish(client, th, t_agent, "reshare-skill")
    slot = preview["credentials"][0]
    assert slot["provided_by"] == "user", preview

    # ── Phase 2: re-use on ───────────────────────────────────────────
    r = client.put(f"{API}/credentials/{cid}", headers=sh, json={"allow_bundle_reuse": True})
    assert r.status_code == 200, r.text
    slot = preview_skill_publish(client, th, t_agent, "reshare-skill")["credentials"][0]
    assert slot["provided_by"] == "publisher" and slot["credential_id"] == cid, slot
    revision = publish_skill(client, th, t_agent, "reshare-skill", visibility="public")
    assert revision["required_credentials"][0]["provided_by"] == "publisher"

    # ── Phase 3: U installs ──────────────────────────────────────────
    u_agent, _ = make_agent_with_env(client, uh, "SkillReshare-U")
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    patch_environment_adapter.get_adapter = lambda env: adapter
    result = install_skill(client, uh, u_agent, revision["package_id"])
    assert result["credential_provisioning"][0]["outcome"] == "linked_publisher"
    assert cid in _linked(client, uh, u_agent)
    share = _shares(client, sh, cid)[u["id"]]
    assert share["shared_by_user_id"] == t["id"]
    assert share["source"] == "skill_install"
    assert share["reshared_via"]["package_kind"] == "skill"

    # ── Phase 4: re-use off releases U ───────────────────────────────
    r = client.put(f"{API}/credentials/{cid}", headers=sh, json={"allow_bundle_reuse": False})
    assert r.status_code == 200, r.text
    assert cid not in _linked(client, uh, u_agent)
    assert u["id"] not in _shares(client, sh, cid)


def test_deleting_a_skill_resharer_unlinks_their_recipients(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """A re-share authored through T's catalog skill dies with T: deleting T
    returns 200 (no 500) and U's share and link are gone."""
    su = superuser_token_headers
    s, sh = make_developer(client, su)
    t, th = make_developer(client, su)
    u, uh = make_developer(client, su)
    producer = setup_producer(client, sh, name="SkillReshareDel-Producer")
    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect", headers=sh, json={})
    assert r.status_code == 200, r.text
    cid = r.json()["credential_id"]
    update_credential(client, sh, cid, allow_sharing=True, service_uri="slot-del")
    share_credential_via_api(client, sh, cid, t["email"])
    r = client.put(f"{API}/credentials/{cid}", headers=sh, json={"allow_bundle_reuse": True})
    assert r.status_code == 200, r.text

    t_agent, t_env = make_agent_with_env(client, th, "SkillReshareDel-T")
    link_credential_to_agent(client, th, t_agent, cid)
    write_skill_with_credentials(t_env, "del-skill", [{"slot": "slot-del", "type": "agent_api"}])
    revision = publish_skill(client, th, t_agent, "del-skill", visibility="public")

    u_agent, _ = make_agent_with_env(client, uh, "SkillReshareDel-U")
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    patch_environment_adapter.get_adapter = lambda env: adapter
    install_skill(client, uh, u_agent, revision["package_id"])
    assert cid in _linked(client, uh, u_agent)
    assert _shares(client, sh, cid)[u["id"]]["shared_by_user_id"] == t["id"]

    r = client.delete(f"{API}/users/{t['id']}", headers=su)
    assert r.status_code == 200, r.text
    assert cid not in _linked(client, uh, u_agent)
    assert u["id"] not in _shares(client, sh, cid)
