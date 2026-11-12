"""Bundle install of a credential its publisher only holds a share of (P1, plan §2).

S owns an ``agent_api`` connection C and shares it with T; T embeds it as
publisher-provided in T's own bundle once S allows re-use; U installs T's
bundle and gets a share of C created by T (``shared_by == T``).

Scenarios:
  1. Re-use lifecycle: refused while off, U links via T with ``reshared_via``
     provenance, S revoking T cascades to U, re-sharing to T restores it for a
     fresh install, S turning re-use off releases U and degrades the install.
  2. Group path: T holds C through a group; T leaving the group releases U,
     unless U is also a group member.
  3. Revocations: a re-sharer's revocation blocks only that path; an owner
     revocation blocks every path; T installing their own bundle gets no
     self-share; U (re-share recipient) cannot re-share onward.
  4. Updates (F2): a release leaves a placeholder on U's install; re-sharing,
     republishing and applying the update restores C and drops the
     placeholder, with no placeholders piling up across cycles; an owner
     revocation of U still blocks the re-link on update.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    apply_install_update,
    install_bundle,
    link_bundle_credential_to_agent,
    make_bundle_public,
    publish_bundle,
)
from tests.utils.credential import (
    get_agent_credentials,
    share_credential_via_api,
    update_credential,
)
from tests.utils.user_group import create_group, remove_member

API = settings.API_V1_STR


def _shares(client, headers, cid) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=headers)
    assert r.status_code == 200, r.text
    return {s["shared_with_user_id"]: s for s in r.json()["data"]}


def _linked(client, headers, agent_id) -> dict[str, dict]:
    return {c["id"]: c for c in get_agent_credentials(client, headers, agent_id)["data"]}


def _placeholders(linked: dict[str, dict]) -> list[dict]:
    return [c for c in linked.values() if c["is_placeholder"]]


def _owned_placeholders(client, headers, owner_id) -> list[dict]:
    r = client.get(f"{API}/credentials/", headers=headers)
    assert r.status_code == 200, r.text
    return [
        c for c in r.json()["data"] if c["is_placeholder"] and c["owner_id"] == owner_id
    ]


def _uninstall(client, headers, agent_id) -> None:
    r = client.post(f"{API}/agents/{agent_id}/uninstall", headers=headers)
    assert r.status_code == 200, r.text
    drain_tasks()


def _publish_raw(client, headers, agent_id):
    return client.post(f"{API}/agents/{agent_id}/publish", headers=headers, json={})


def _reuse(client, headers, cid, on: bool) -> None:
    r = client.put(f"{API}/credentials/{cid}", headers=headers, json={"allow_bundle_reuse": on})
    assert r.status_code == 200, r.text
    drain_tasks()


def _setup(client, su, *, group_for_t: bool = False):
    """S owns a shareable connection; T holds it (direct share or group) and
    has a publisher install linking it with an explicit ``publisher`` override."""
    s, sh = make_developer(client, su)
    t, th = make_developer(client, su)
    producer = setup_producer(client, sh, name=f"Reshare-Producer-{uuid.uuid4().hex[:4]}")
    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect", headers=sh, json={})
    assert r.status_code == 200, r.text
    cid = r.json()["credential_id"]
    update_credential(client, sh, cid, allow_sharing=True)
    cred = client.get(f"{API}/credentials/{cid}", headers=sh).json()
    group = None
    if group_for_t:
        group = create_group(client, su, member_ids=[t["id"]])
        r = client.post(
            f"{API}/credentials/{cid}/group-shares", headers=sh, json={"group_id": group["id"]}
        )
        assert r.status_code == 200, r.text
    else:
        share_credential_via_api(client, sh, cid, t["email"])
    agent = create_agent_via_api(client, th, name="Reshare-T-Agent")
    drain_tasks()
    assert _publish_raw(client, th, agent["id"]).status_code == 200  # becomes publisher install
    drain_tasks()
    link_bundle_credential_to_agent(client, th, agent["id"], cid)
    r = client.patch(
        f"{API}/agents/{agent['id']}/publish-settings",
        headers=th,
        json={"credential_overrides": {cred["name"]: {"provided_by": "publisher"}}},
    )
    assert r.status_code == 200, r.text
    return s, sh, t, th, cid, agent["id"], group


def _publish_public(client, th, agent_id) -> str:
    fresh = publish_bundle(client, th, agent_id)
    make_bundle_public(client, th, fresh["bundle_uuid"])
    return fresh["bundle_id"]


def test_reshared_connection_lifecycle_through_a_recipients_bundle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Re-use off: T's explicit publisher override cannot publish (400)
    2. S enables re-use; T publishes; U installs and links C; S's share list
       shows U shared by T, ``reshared_via`` names T and the bundle
    3. S revokes T's share: U's link and share are gone
    4. S shares with T again; a fresh install by U links C again
    5. S turns re-use off: U's share is released, the next install gets a placeholder
    """
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, _ = _setup(client, su)
    u, uh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    r = _publish_raw(client, th, t_agent)
    assert r.status_code == 400, r.text

    # ── Phase 2 ──────────────────────────────────────────────────────
    _reuse(client, sh, cid, True)
    bundle_id = _publish_public(client, th, t_agent)
    first = install_bundle(client, uh, bundle_id)
    assert _linked(client, uh, first["id"])[cid]["is_placeholder"] is False
    shares = _shares(client, sh, cid)
    u_share = shares[u["id"]]
    assert u_share["shared_by_user_id"] == t["id"]
    assert u_share["shared_by_email"] == t["email"]
    assert u_share["source"] == "bundle_install"
    via = u_share["reshared_via"]
    assert via["user_id"] == t["id"] and via["package_kind"] == "bundle"
    assert via["package_name"]
    assert shares[t["id"]]["reshared_via"] is None
    r = client.get(f"{API}/credentials/{cid}/reshares", headers=th)
    assert [x["shared_with_user_id"] for x in r.json()["data"]] == [u["id"]]

    # ── Phase 3: owner revokes T ─────────────────────────────────────
    r = client.delete(f"{API}/credentials/{cid}/shares/{shares[t['id']]['id']}", headers=sh)
    assert r.status_code == 200, r.text
    drain_tasks()
    assert cid not in _linked(client, uh, first["id"])
    assert _shares(client, sh, cid) == {}

    # ── Phase 4: share again, fresh install links again ──────────────
    _uninstall(client, uh, first["id"])
    share_credential_via_api(client, sh, cid, t["email"])
    second = install_bundle(client, uh, bundle_id)
    assert cid in _linked(client, uh, second["id"])

    # ── Phase 5: re-use off releases U ───────────────────────────────
    _reuse(client, sh, cid, False)
    assert cid not in _linked(client, uh, second["id"])
    assert set(_shares(client, sh, cid)) == {t["id"]}
    _uninstall(client, uh, second["id"])
    third = install_bundle(client, uh, bundle_id)
    linked = _linked(client, uh, third["id"])
    assert cid not in linked
    assert any(c["is_placeholder"] for c in linked.values())


def test_reshared_connection_released_when_publisher_leaves_group(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. T holds C only through a group; U installs T's bundle and links C
    2. T is removed from the group: U is released
    3. Variant: U is also a group member -> U keeps C after T leaves
    """
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, group = _setup(client, su, group_for_t=True)
    _reuse(client, sh, cid, True)
    bundle_id = _publish_public(client, th, t_agent)
    u, uh = make_developer(client, su)
    v, vh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    u_install = install_bundle(client, uh, bundle_id)
    assert cid in _linked(client, uh, u_install["id"])
    assert _shares(client, sh, cid)[u["id"]]["shared_by_user_id"] == t["id"]

    # ── Phase 3 (set up before T leaves): V is a group member too ────
    r = client.post(
        f"{API}/admin/user-groups/{group['id']}/members",
        headers=su,
        json={"user_ids": [v["id"]]},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    v_install = install_bundle(client, vh, bundle_id)
    assert cid in _linked(client, vh, v_install["id"])

    # ── Phase 2: T leaves ────────────────────────────────────────────
    remove_member(client, su, group["id"], t["id"])
    drain_tasks()
    assert cid not in _linked(client, uh, u_install["id"])
    assert u["id"] not in _shares(client, sh, cid)
    assert cid in _linked(client, vh, v_install["id"])


def test_revocations_by_resharer_and_owner_and_no_self_share(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. T installs their own bundle: no self-share row
    2. T (re-sharer) revokes U's re-share: U's reinstall does not re-add it
    3. S shares with U directly: allowed; the link works again
    4. S revokes U: T's bundle does not re-add U on reinstall
    5. A non-owner cannot revoke a share they did not author (403)
    """
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, _ = _setup(client, su)
    _reuse(client, sh, cid, True)
    bundle_id = _publish_public(client, th, t_agent)
    u, uh = make_developer(client, su)

    # ── Phase 1: self-install ────────────────────────────────────────
    t_consumer = install_bundle(client, th, bundle_id)
    assert cid in _linked(client, th, t_consumer["id"])
    for row in _shares(client, sh, cid).values():
        assert row["shared_by_user_id"] != row["shared_with_user_id"]

    # ── Phase 2: T revokes U ─────────────────────────────────────────
    first = install_bundle(client, uh, bundle_id)
    u_share = _shares(client, sh, cid)[u["id"]]
    other, other_h = make_developer(client, su)
    r = client.delete(f"{API}/credentials/{cid}/shares/{u_share['id']}", headers=other_h)
    assert r.status_code == 403, r.text  # review fix 4: no permission -> 403
    r = client.delete(f"{API}/credentials/{cid}/shares/{uuid.uuid4()}", headers=th)
    assert r.status_code == 404, r.text  # unknown share
    r = client.delete(f"{API}/credentials/{cid}/shares/{u_share['id']}", headers=th)
    assert r.status_code == 200, r.text
    drain_tasks()
    assert cid not in _linked(client, uh, first["id"])
    _uninstall(client, uh, first["id"])
    second = install_bundle(client, uh, bundle_id)
    assert cid not in _linked(client, uh, second["id"])
    assert u["id"] not in _shares(client, sh, cid)

    # ── Phase 3: owner shares directly ───────────────────────────────
    share_credential_via_api(client, sh, cid, u["email"])
    _uninstall(client, uh, second["id"])
    third = install_bundle(client, uh, bundle_id)
    assert cid in _linked(client, uh, third["id"])

    # ── Phase 4: owner revokes U -> no re-add through T ──────────────
    direct = _shares(client, sh, cid)[u["id"]]
    r = client.delete(f"{API}/credentials/{cid}/shares/{direct['id']}", headers=sh)
    assert r.status_code == 200, r.text
    drain_tasks()
    _uninstall(client, uh, third["id"])
    fourth = install_bundle(client, uh, bundle_id)
    assert cid not in _linked(client, uh, fourth["id"])
    assert u["id"] not in _shares(client, sh, cid)


def test_reshare_recipient_cannot_reshare_onward(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """U got C through T's bundle; U cannot embed it in U's own bundle."""
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, _ = _setup(client, su)
    _reuse(client, sh, cid, True)
    bundle_id = _publish_public(client, th, t_agent)
    u, uh = make_developer(client, su)
    install_bundle(client, uh, bundle_id)
    assert u["id"] in _shares(client, sh, cid)

    u_agent = create_agent_via_api(client, uh, name="Reshare-U-Agent")
    drain_tasks()
    assert _publish_raw(client, uh, u_agent["id"]).status_code == 200
    drain_tasks()
    link_bundle_credential_to_agent(client, uh, u_agent["id"], cid)
    r = client.get(f"{API}/agents/{u_agent['id']}/bundle-credential-drift", headers=uh)
    assert r.status_code == 200, r.text
    live = {o["credential_id"]: o for o in r.json()["live"]}
    assert "publisher" not in live[cid]["can_provide_as"]
    assert live[cid]["blocked_reason"]
    cred = client.get(f"{API}/credentials/{cid}", headers=uh).json()
    r = client.patch(
        f"{API}/agents/{u_agent['id']}/publish-settings",
        headers=uh,
        json={"credential_overrides": {cred["name"]: {"provided_by": "publisher"}}},
    )
    assert r.status_code == 200, r.text
    assert _publish_raw(client, uh, u_agent["id"]).status_code == 400


def test_mutual_resharers_are_both_released_when_the_owner_removes_the_group(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """T and V hold C through one group and install each other's bundle; they
    cannot keep each other's access alive after the owner removes the group."""
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, group = _setup(client, su, group_for_t=True)
    v, vh = make_developer(client, su)
    r = client.post(
        f"{API}/admin/user-groups/{group['id']}/members", headers=su, json={"user_ids": [v["id"]]}
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    _reuse(client, sh, cid, True)
    t_bundle = _publish_public(client, th, t_agent)

    v_agent = create_agent_via_api(client, vh, name="Reshare-V-Agent")
    drain_tasks()
    assert _publish_raw(client, vh, v_agent["id"]).status_code == 200
    drain_tasks()
    link_bundle_credential_to_agent(client, vh, v_agent["id"], cid)
    cred = client.get(f"{API}/credentials/{cid}", headers=vh).json()
    r = client.patch(
        f"{API}/agents/{v_agent['id']}/publish-settings",
        headers=vh,
        json={"credential_overrides": {cred["name"]: {"provided_by": "publisher"}}},
    )
    assert r.status_code == 200, r.text
    v_bundle = _publish_public(client, vh, v_agent["id"])

    t_inst = install_bundle(client, th, v_bundle)
    v_inst = install_bundle(client, vh, t_bundle)
    assert cid in _linked(client, th, t_inst["id"])
    assert cid in _linked(client, vh, v_inst["id"])

    r = client.delete(f"{API}/credentials/{cid}/group-shares/{group['id']}", headers=sh)
    assert r.status_code == 200, r.text
    drain_tasks()
    assert cid not in _linked(client, th, t_inst["id"])
    assert cid not in _linked(client, vh, v_inst["id"])
    assert _shares(client, sh, cid) == {}


def test_used_in_bundle_flag_counts_other_publishers_only_when_publisher_provided(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """T's bundle that merely links C does not flag it; once T's bundle embeds
    C as publisher-provided, S's list flags it."""
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, _ = _setup(client, su)
    cred = client.get(f"{API}/credentials/{cid}", headers=sh).json()

    def flag() -> bool:
        r = client.get(f"{API}/credentials/", headers=sh)
        assert r.status_code == 200, r.text
        return next(c for c in r.json()["data"] if c["id"] == cid)["used_in_bundle"]

    r = client.patch(
        f"{API}/agents/{t_agent}/publish-settings", headers=th, json={"credential_overrides": {}}
    )
    assert r.status_code == 200, r.text
    assert _publish_raw(client, th, t_agent).status_code == 200  # provided_by user
    drain_tasks()
    assert flag() is False

    _reuse(client, sh, cid, True)
    r = client.patch(
        f"{API}/agents/{t_agent}/publish-settings",
        headers=th,
        json={"credential_overrides": {cred["name"]: {"provided_by": "publisher"}}},
    )
    assert r.status_code == 200, r.text
    assert _publish_raw(client, th, t_agent).status_code == 200
    drain_tasks()
    assert flag() is True


def test_resharer_losing_group_path_releases_recipients_despite_another_reshare(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. T holds C only via group G and publishes B1; V (direct share from S)
       publishes B2 and T installs it, so T also holds a re-share from V
    2. U installs B1
    3. S removes T from G: U's re-share from T is released and unlinked,
       although T still has the re-share from V
    """
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, group = _setup(client, su, group_for_t=True)
    _reuse(client, sh, cid, True)
    b1 = _publish_public(client, th, t_agent)
    v, vh = make_developer(client, su)
    u, uh = make_developer(client, su)
    share_credential_via_api(client, sh, cid, v["email"])
    v_agent = create_agent_via_api(client, vh, name="Reshare-V2-Agent")
    drain_tasks()
    assert _publish_raw(client, vh, v_agent["id"]).status_code == 200
    drain_tasks()
    link_bundle_credential_to_agent(client, vh, v_agent["id"], cid)
    cred = client.get(f"{API}/credentials/{cid}", headers=vh).json()
    r = client.patch(
        f"{API}/agents/{v_agent['id']}/publish-settings",
        headers=vh,
        json={"credential_overrides": {cred["name"]: {"provided_by": "publisher"}}},
    )
    assert r.status_code == 200, r.text
    b2 = _publish_public(client, vh, v_agent["id"])

    install_bundle(client, th, b2)
    u_install = install_bundle(client, uh, b1)
    assert cid in _linked(client, uh, u_install["id"])

    remove_member(client, su, group["id"], t["id"])
    drain_tasks()
    assert cid not in _linked(client, uh, u_install["id"])
    assert u["id"] not in _shares(client, sh, cid)


def test_release_leaves_placeholder_and_update_restores_the_reshared_link(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. U installs T's bundle and links C through T
    2. S revokes T: C is unlinked from U's install and one placeholder takes
       its slot (the agent shows "needs credential")
    3. S shares with T again, T republishes, U applies the update: C is linked
       again through T and the placeholder is gone (deleted, not orphaned)
    4. A second revoke / re-share / republish / update cycle ends the same
       way: never more than one placeholder
    """
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, _ = _setup(client, su)
    _reuse(client, sh, cid, True)
    bundle_id = _publish_public(client, th, t_agent)
    u, uh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    install = install_bundle(client, uh, bundle_id)
    assert cid in _linked(client, uh, install["id"])
    assert _placeholders(_linked(client, uh, install["id"])) == []

    for cycle in range(2):
        # ── Phase 2: owner revokes T ────────────────────────────────
        t_share = _shares(client, sh, cid)[t["id"]]
        r = client.delete(f"{API}/credentials/{cid}/shares/{t_share['id']}", headers=sh)
        assert r.status_code == 200, r.text
        drain_tasks()
        linked = _linked(client, uh, install["id"])
        assert cid not in linked, f"cycle {cycle}"
        placeholders = _placeholders(linked)
        assert len(placeholders) == 1, f"cycle {cycle}: {linked}"
        assert placeholders[0]["name"].endswith("(placeholder)")

        # ── Phase 3: re-share, republish, apply ─────────────────────
        share_credential_via_api(client, sh, cid, t["email"])
        # Losing the share also unlinked C from T's working copy.
        link_bundle_credential_to_agent(client, th, t_agent, cid)
        _publish_public(client, th, t_agent)
        apply_install_update(client, uh, install["id"])
        linked = _linked(client, uh, install["id"])
        assert cid in linked, f"cycle {cycle}: {linked}"
        assert _placeholders(linked) == [], f"cycle {cycle}: {linked}"
        assert _shares(client, sh, cid)[u["id"]]["shared_by_user_id"] == t["id"]
        assert _owned_placeholders(client, uh, u["id"]) == []


def test_owner_revocation_still_blocks_relink_on_update(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. U installs T's bundle and links C through T
    2. S revokes U's share: C is unlinked and a placeholder takes the slot
    3. T republishes; U applies the update: C stays unlinked (owner
       revocation blocks every path) and the one placeholder stays
    """
    su = superuser_token_headers
    s, sh, t, th, cid, t_agent, _ = _setup(client, su)
    _reuse(client, sh, cid, True)
    bundle_id = _publish_public(client, th, t_agent)
    u, uh = make_developer(client, su)

    # ── Phase 1 ──────────────────────────────────────────────────────
    install = install_bundle(client, uh, bundle_id)
    assert cid in _linked(client, uh, install["id"])

    # ── Phase 2 ──────────────────────────────────────────────────────
    u_share = _shares(client, sh, cid)[u["id"]]
    r = client.delete(f"{API}/credentials/{cid}/shares/{u_share['id']}", headers=sh)
    assert r.status_code == 200, r.text
    drain_tasks()
    linked = _linked(client, uh, install["id"])
    assert cid not in linked
    assert len(_placeholders(linked)) == 1

    # ── Phase 3 ──────────────────────────────────────────────────────
    _publish_public(client, th, t_agent)
    apply_install_update(client, uh, install["id"])
    linked = _linked(client, uh, install["id"])
    assert cid not in linked
    assert len(_placeholders(linked)) == 1, linked
    assert u["id"] not in _shares(client, sh, cid)
