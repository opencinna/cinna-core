"""Cloud skill install after the credential owner removed the installer's share.

Bug 1 (drafts/catalog_unification/next-session-core.md): the owner's
``DELETE /credentials/{id}/shares/{share_id}`` records a revocation for that
(credential, person). A later cloud skill install by that person must not
share the publisher credential again: the slot falls back to
``publisher_unavailable`` with a placeholder. Other installers are
unaffected, and the owner sharing again directly lifts it.

The provision-local side of the rule is covered in
``tests/api/catalog/catalog_skill_credential_revocation_test.py``.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    get_credential,
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

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


def _shares(client: TestClient, owner_headers: dict[str, str], cid: str) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=owner_headers)
    assert r.status_code == 200, r.text
    return {s["shared_with_user_id"]: s for s in r.json()["data"]}


def _linked(client: TestClient, headers: dict[str, str], agent_id: str) -> set[str]:
    return {c["id"] for c in get_agent_credentials(client, headers, agent_id)["data"]}


def test_owner_unshare_makes_the_publisher_credential_unavailable_to_that_installer(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. Publisher's cloud skill provides a shareable credential; B installs
       it on agent 1 → ``linked_publisher``, share ``skill_install``
    2. The owner removes B's share → agent 1 loses the link
    3. B previews and installs on agent 2 → ``publisher_unavailable``, a
       placeholder B owns; no share for B is created
    4. C installs → ``linked_publisher`` (the revocation is B's alone)
    5. The owner shares with B directly → B installs on agent 3 →
       ``linked_publisher``; B's one share keeps the direct source
    """
    su = superuser_token_headers
    _, pub_headers = make_developer(client, su)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "RevokedShare-Publisher")
    cred = create_random_credential(client, pub_headers, credential_type="api_token")
    cid = cred["id"]
    update_credential(client, pub_headers, cid, service_uri="slot-revoked", allow_sharing=True)
    link_credential_to_agent(client, pub_headers, pub_agent, cid)
    write_skill_with_credentials(
        pub_env, "revoked-skill", [{"slot": "slot-revoked", "type": "api_token"}],
    )
    revision = publish_skill(client, pub_headers, pub_agent, "revoked-skill", visibility="public")
    package_uuid = revision["package_id"]
    assert revision["required_credentials"][0]["provided_by"] == "publisher"

    b, b_headers = make_developer(client, su)
    c, c_headers = make_developer(client, su)
    b_email = b["email"]
    agent_1, _ = make_agent_with_env(client, b_headers, "RevokedShare-B1")
    agent_2, _ = make_agent_with_env(client, b_headers, "RevokedShare-B2")
    agent_3, _ = make_agent_with_env(client, b_headers, "RevokedShare-B3")
    c_agent, _ = make_agent_with_env(client, c_headers, "RevokedShare-C")
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    patch_environment_adapter.get_adapter = lambda env: adapter

    # ── Phase 1: B installs ──────────────────────────────────────────
    first = install_skill(client, b_headers, agent_1, package_uuid)
    assert first["credential_provisioning"][0]["outcome"] == "linked_publisher"
    assert cid in _linked(client, b_headers, agent_1)
    share = _shares(client, pub_headers, cid)[b["id"]]
    assert share["source"] == "skill_install"

    # ── Phase 2: the owner removes B's share ─────────────────────────
    r = client.delete(f"{API}/credentials/{cid}/shares/{share['id']}", headers=pub_headers)
    assert r.status_code == 200, r.text
    assert _shares(client, pub_headers, cid) == {}
    assert cid not in _linked(client, b_headers, agent_1)

    # ── Phase 3: a new install does not bring it back ────────────────
    preview = get_skill_install_preview(client, b_headers, agent_2, package_uuid)
    assert preview["credentials"][0]["outcome"] == "publisher_unavailable"
    second = install_skill(client, b_headers, agent_2, package_uuid)
    prov = second["credential_provisioning"][0]
    assert prov["outcome"] == "publisher_unavailable"
    assert prov["credential_id"] != cid
    placeholder = get_credential(client, b_headers, prov["credential_id"])
    assert placeholder["is_placeholder"] is True
    assert _linked(client, b_headers, agent_2) == {prov["credential_id"]}
    assert _shares(client, pub_headers, cid) == {}

    # ── Phase 4: another installer is unaffected ─────────────────────
    other = install_skill(client, c_headers, c_agent, package_uuid)
    assert other["credential_provisioning"][0]["outcome"] == "linked_publisher"
    assert set(_shares(client, pub_headers, cid)) == {c["id"]}

    # ── Phase 5: the owner shares with B again ───────────────────────
    share_credential_via_api(client, pub_headers, cid, b_email)
    preview = get_skill_install_preview(client, b_headers, agent_3, package_uuid)
    assert preview["credentials"][0]["outcome"] == "linked_publisher"
    third = install_skill(client, b_headers, agent_3, package_uuid)
    prov = third["credential_provisioning"][0]
    assert (prov["outcome"], prov["credential_id"]) == ("linked_publisher", cid)
    assert cid in _linked(client, b_headers, agent_3)
    shares = _shares(client, pub_headers, cid)
    assert set(shares) == {b["id"], c["id"]}
    assert shares[b["id"]]["source"] in (None, "direct")
