"""Credential provisioning on bundle apply-update (F2).

An update re-runs install-time credential provisioning for the new revision,
idempotently: answered slots stay, new specs get their slot.

Scenarios:
  A. A revision that adds a publisher-provided credential links it on update;
     a revision that adds a user-provided credential gets exactly one
     placeholder, and later updates neither duplicate nor drop either slot.
  B. The publisher turns sharing off: the install's slot becomes one
     placeholder, an update that cannot re-link it is ``degraded``; the
     installer fills and renames the placeholder, and a later update keeps
     it without linking the publisher credential alongside.
  C. A non-bundle agent losing a shared credential gets no placeholder.
  D. A user spec answered by the installer's own pick (type match) does not
     also answer a publisher spec of the same type added later.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_api_keys import make_developer
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    apply_install_update,
    create_bundle_credential,
    install_bundle,
    link_bundle_credential_to_agent,
    publish_bundle,
    publish_bundle_and_make_public,
)
from tests.utils.credential import (
    get_agent_credentials,
    set_credential_sharing,
    share_credential_via_api,
    update_credential,
)

API = settings.API_V1_STR


def _linked(client, headers, agent_id) -> list[dict]:
    return get_agent_credentials(client, headers, agent_id)["data"]


def test_update_provisions_credential_specs_added_by_the_new_revision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. P publishes a bundle with no credentials; U installs it
    2. P links an owned shareable credential (publisher-provided) and a
       private one (user-provided), republishes
    3. U applies the update: the publisher credential is linked and the user
       spec has one placeholder
    4. P republishes; U applies again: still exactly those two links
    """
    su = superuser_token_headers
    p, ph = make_developer(client, su)
    u, uh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    agent = create_agent_via_api(client, ph, name="UpdCred-Publisher")
    drain_tasks()
    publish_bundle_and_make_public(client, ph, agent["id"])
    bundle_id = client.get(f"{API}/agents/{agent['id']}", headers=ph).json()["bundle_id"]
    install = install_bundle(client, uh, bundle_id)
    assert _linked(client, uh, install["id"]) == []

    # ── Phase 2 ──────────────────────────────────────────────────────
    shared = create_bundle_credential(client, ph, name="updcred-shared", allow_sharing=True)
    private = create_bundle_credential(client, ph, name="updcred-private")
    link_bundle_credential_to_agent(client, ph, agent["id"], shared["id"])
    link_bundle_credential_to_agent(client, ph, agent["id"], private["id"])
    publish_bundle(client, ph, agent["id"])

    # ── Phase 3 ──────────────────────────────────────────────────────
    apply_install_update(client, uh, install["id"])
    linked = _linked(client, uh, install["id"])
    by_id = {c["id"]: c for c in linked}
    assert shared["id"] in by_id and by_id[shared["id"]]["is_placeholder"] is False
    assert private["id"] not in by_id
    placeholders = [c for c in linked if c["is_placeholder"]]
    assert [c["name"] for c in placeholders] == ["updcred-private (placeholder)"]
    assert len(linked) == 2, linked

    # ── Phase 4 ──────────────────────────────────────────────────────
    publish_bundle(client, ph, agent["id"])
    apply_install_update(client, uh, install["id"])
    again = _linked(client, uh, install["id"])
    assert sorted(c["id"] for c in again) == sorted(c["id"] for c in linked)


def test_unprovidable_publisher_slot_degrades_and_a_filled_placeholder_is_kept(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. P publishes a shareable credential as publisher-provided; U installs
       and links it; P publishes a second revision
    2. P turns sharing off: U's link is gone and one placeholder takes the slot
    3. U applies the update: still one placeholder, status ``degraded``
    4. U fills the placeholder in and renames it
    5. P turns sharing back on and republishes; U applies the update: only
       U's own credential is linked, no new placeholder, status ``synced``
    """
    su = superuser_token_headers
    p, ph = make_developer(client, su)
    u, uh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    agent = create_agent_via_api(client, ph, name="UpdCred-Degrade-Publisher")
    drain_tasks()
    shared = create_bundle_credential(client, ph, name="updcred-deg", allow_sharing=True)
    link_bundle_credential_to_agent(client, ph, agent["id"], shared["id"])
    publish_bundle_and_make_public(client, ph, agent["id"])
    bundle_id = client.get(f"{API}/agents/{agent['id']}", headers=ph).json()["bundle_id"]
    install = install_bundle(client, uh, bundle_id)
    assert [c["id"] for c in _linked(client, uh, install["id"])] == [shared["id"]]
    publish_bundle(client, ph, agent["id"])

    # ── Phase 2 ──────────────────────────────────────────────────────
    set_credential_sharing(client, ph, shared["id"], False)
    drain_tasks()
    linked = _linked(client, uh, install["id"])
    assert [c["name"] for c in linked] == ["updcred-deg (placeholder)"], linked
    assert linked[0]["is_placeholder"] is True
    placeholder_id = linked[0]["id"]

    # ── Phase 3 ──────────────────────────────────────────────────────
    updated = apply_install_update(client, uh, install["id"])
    assert updated["last_update_status"] == "degraded"
    assert [c["id"] for c in _linked(client, uh, install["id"])] == [placeholder_id]

    # ── Phase 4 ──────────────────────────────────────────────────────
    update_credential(
        client,
        uh,
        placeholder_id,
        name="my own token",
        credential_data={
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": "installer-token",
        },
    )
    linked = _linked(client, uh, install["id"])
    assert linked[0]["is_placeholder"] is False, linked

    # ── Phase 5 ──────────────────────────────────────────────────────
    set_credential_sharing(client, ph, shared["id"], True)
    drain_tasks()
    publish_bundle(client, ph, agent["id"])
    updated = apply_install_update(client, uh, install["id"])
    assert updated["last_update_status"] == "synced"
    linked = _linked(client, uh, install["id"])
    assert [c["id"] for c in linked] == [placeholder_id], linked
    assert linked[0]["is_placeholder"] is False


def test_non_bundle_agent_losing_a_shared_credential_gets_no_placeholder(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """S shares a credential with U, U links it to a plain agent, S revokes:
    the link is gone and nothing takes its place."""
    su = superuser_token_headers
    s, sh = make_developer(client, su)
    u, uh = make_developer(client, su)
    cred = create_bundle_credential(client, sh, name="updcred-direct", allow_sharing=True)
    share = share_credential_via_api(client, sh, cred["id"], u["email"])
    agent = create_agent_via_api(client, uh, name="UpdCred-Plain-Agent")
    drain_tasks()
    link_bundle_credential_to_agent(client, uh, agent["id"], cred["id"])
    assert [c["id"] for c in _linked(client, uh, agent["id"])] == [cred["id"]]

    r = client.delete(f"{API}/credentials/{cred['id']}/shares/{share['id']}", headers=sh)
    assert r.status_code == 200, r.text
    drain_tasks()
    assert _linked(client, uh, agent["id"]) == []


def test_installer_pick_for_user_spec_does_not_answer_a_new_publisher_spec(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. P publishes a user-provided ``api_token`` spec; U installs picking an
       own credential under another name
    2. P adds a publisher-provided ``api_token`` credential and republishes
    3. U applies the update: U's pick stays and the publisher credential is
       linked too, with no placeholder
    """
    su = superuser_token_headers
    p, ph = make_developer(client, su)
    u, uh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    agent = create_agent_via_api(client, ph, name="UpdCred-Pick-Publisher")
    drain_tasks()
    private = create_bundle_credential(client, ph, name="updcred-pick-user")
    link_bundle_credential_to_agent(client, ph, agent["id"], private["id"])
    publish_bundle_and_make_public(client, ph, agent["id"])
    bundle_id = client.get(f"{API}/agents/{agent['id']}", headers=ph).json()["bundle_id"]
    own = create_bundle_credential(client, uh, name="u-own-token")
    install = install_bundle(
        client,
        uh,
        bundle_id,
        request_body={
            "credentials": {
                "updcred-pick-user": {"mode": "use_existing", "credential_id": own["id"]}
            }
        },
    )
    assert [c["id"] for c in _linked(client, uh, install["id"])] == [own["id"]]

    # ── Phase 2 ──────────────────────────────────────────────────────
    shared = create_bundle_credential(client, ph, name="updcred-pick-pub", allow_sharing=True)
    link_bundle_credential_to_agent(client, ph, agent["id"], shared["id"])
    publish_bundle(client, ph, agent["id"])

    # ── Phase 3 ──────────────────────────────────────────────────────
    apply_install_update(client, uh, install["id"])
    linked = _linked(client, uh, install["id"])
    assert sorted(c["id"] for c in linked) == sorted([own["id"], shared["id"]]), linked
    assert not any(c["is_placeholder"] for c in linked)
