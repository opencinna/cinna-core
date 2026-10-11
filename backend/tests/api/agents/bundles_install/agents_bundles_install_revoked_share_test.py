"""Cloud bundle install after the credential owner removed the installer's share.

Bug 1 (drafts/catalog_unification/next-session-core.md): the owner's
``DELETE /credentials/{id}/shares/{share_id}`` records a revocation for that
(credential, person). A later install of a bundle whose spec is
publisher-provided by that credential must not share it with that person
again: the new agent gets a placeholder instead. Other installers are
unaffected, and the owner sharing again directly lifts it.

The provision-local side of the rule is covered in
``tests/api/catalog/catalog_skill_credential_revocation_test.py``; the cloud
skill install side in
``tests/api/agents/skill_credentials/agents_skill_credentials_revoked_share_test.py``.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential,
    install_bundle,
    link_bundle_credential_to_agent,
    make_bundle_public,
    publish_bundle,
)
from tests.utils.credential import get_agent_credentials, share_credential_via_api
from tests.utils.skill_catalog import make_developer

API = settings.API_V1_STR


def _shares(client: TestClient, owner_headers: dict[str, str], cid: str) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=owner_headers)
    assert r.status_code == 200, r.text
    return {s["shared_with_user_id"]: s for s in r.json()["data"]}


def _linked(client: TestClient, headers: dict[str, str], agent_id: str) -> dict[str, dict]:
    return {c["id"]: c for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _uninstall(client: TestClient, headers: dict[str, str], agent_id: str) -> None:
    """Installing the same bundle again is idempotent (same agent); a fresh
    provisioning run needs the install removed first."""
    r = client.post(f"{API}/agents/{agent_id}/uninstall", headers=headers)
    assert r.status_code == 200, r.text
    drain_tasks()


def test_owner_unshare_gives_that_installers_next_bundle_install_a_placeholder(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A public bundle carries a publisher-provided credential; the
       installer's first install links it (share ``bundle_install``)
    2. The owner removes the installer's share → the installed agent loses it
    3. The installer uninstalls and installs again → the new agent links a
       placeholder, not the publisher credential; no share is created
    4. Another installer still gets the publisher credential
    5. The owner shares with the installer directly → uninstall and a third
       install links
       the publisher credential; the share keeps the direct source
    """
    su = superuser_token_headers
    _, pub_headers = make_developer(client, su)
    publisher_agent = create_agent_via_api(client, pub_headers, name="Revoked Bundle Publisher")
    drain_tasks()
    cred = create_bundle_credential(client, pub_headers, name="revoked-bundle-cred", allow_sharing=True)
    cid = cred["id"]
    link_bundle_credential_to_agent(client, pub_headers, publisher_agent["id"], cid)
    fresh = publish_bundle(client, pub_headers, publisher_agent["id"])
    make_bundle_public(client, pub_headers, fresh["bundle_uuid"])
    bundle_id = fresh["bundle_id"]
    installer, installer_headers = make_developer(client, su)
    other, other_headers = make_developer(client, su)

    # ── Phase 1: first install links the publisher credential ────────
    first = install_bundle(client, installer_headers, bundle_id)
    assert cid in _linked(client, installer_headers, first["id"])
    share = _shares(client, pub_headers, cid)[installer["id"]]
    assert share["source"] == "bundle_install"

    # ── Phase 2: the owner removes the installer's share ─────────────
    r = client.delete(f"{API}/credentials/{cid}/shares/{share['id']}", headers=pub_headers)
    assert r.status_code == 200, r.text
    assert _shares(client, pub_headers, cid) == {}
    assert cid not in _linked(client, installer_headers, first["id"])

    # ── Phase 3: a new install gets a placeholder ────────────────────
    _uninstall(client, installer_headers, first["id"])
    second = install_bundle(client, installer_headers, bundle_id)
    linked = _linked(client, installer_headers, second["id"])
    assert cid not in linked
    placeholders = [c for c in linked.values() if c["type"] == "api_token"]
    assert len(placeholders) == 1, linked
    assert placeholders[0]["is_placeholder"] is True
    assert _shares(client, pub_headers, cid) == {}

    # ── Phase 4: another installer is unaffected ─────────────────────
    theirs = install_bundle(client, other_headers, bundle_id)
    assert cid in _linked(client, other_headers, theirs["id"])
    assert set(_shares(client, pub_headers, cid)) == {other["id"]}

    # ── Phase 5: the owner shares with the installer again ───────────
    share_credential_via_api(client, pub_headers, cid, installer["email"])
    _uninstall(client, installer_headers, second["id"])
    third = install_bundle(client, installer_headers, bundle_id)
    assert cid in _linked(client, installer_headers, third["id"])
    shares = _shares(client, pub_headers, cid)
    assert set(shares) == {installer["id"], other["id"]}
    assert shares[installer["id"]]["source"] in (None, "direct")
