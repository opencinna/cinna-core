"""Bug 1 — an owner's unshare is final until the owner shares again.

The rule (drafts/catalog_unification/next-session-core.md, 2026-10-11): a
credential core shares with someone who installs a skill (provision-local,
share source ``skill_local_install``) stays shared until the credential's
owner changes the credential's own sharing — turning sharing off, or removing
that person's share. ``DELETE /credentials/{id}/shares/{share_id}`` records a
revocation for that (credential, person), whatever the share's source; only
the owner's ``POST /credentials/{id}/shares`` to that person clears it.
A later skill version that stops providing the credential does NOT revoke.

Scenarios:
  1. T4.8 (ii): provision-local shares → owner unshares → provision-local
     answers ``not_shareable`` (twice, no share row, off the Desktop) → other
     grantees are unaffected → owner re-shares → ``already_shared``.
  2. Sharing off → everyone ``not_shareable``; sharing on again re-shares
     people without a revocation, but not people the owner unshared (a
     ``skill_local_install`` share or a direct one).
  3. A later version that stops providing the credential keeps the share;
     the owner's share list shows the recipient's Desktop installs
     (``local_skill_usages``, any revision; host agent name only for an
     agent the recipient published).
  4. The owner's share list names the recipient's cloud agents linking the
     credential through the owner's skills (``agent_usages``) and counts the
     rest (``direct_agent_use_count``).
  5. Deletion impact counts people who hold a share and installed one of
     the owner's providing skills on their Desktop: tier 2 (409 unforced).

Cloud installs after an unshare live in
``tests/api/agents/skill_credentials/agents_skill_credentials_revoked_share_test.py``
and ``tests/api/agents/bundles_install/agents_bundles_install_revoked_share_test.py``.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person as make_developer
from tests.api.catalog.catalog_skill_provision_local_test import (
    _cred,
    _for_users,
    _listed,
    _named,
    _ok,
    _outcomes,
    _provision,
    _publish,
    _shared_with_me,
    _shares_of,
    _skill_tgz,
)
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.catalog import (
    create_desktop_agent,
    make_person,
    manifest_id_of,
    user_grant,
)
from tests.utils.credential import (
    get_agent_credentials,
    link_credential_to_agent,
    set_credential_sharing,
    share_credential_via_api,
)
from tests.utils.desktop_credentials import desktop_session
from tests.utils.skill_catalog import (
    install_skill,
    make_agent_with_env,
    publish_skill,
    write_skill_with_credentials,
)

API = settings.API_V1_STR


# ── Helpers ────────────────────────────────────────────────────────────────


def _shareable(client, pub, slot: str) -> dict:
    """A credential of ``pub`` for ``slot``: shareable, local use allowed."""
    return _cred(client, pub.headers, slot, allow_sharing=True, local=True)


def _publish_providing(client, pub, cred: dict, name: str, *grantees) -> str:
    """Desktop-upload a skill whose slot ``cred`` provides, usable by
    ``grantees``; returns the package uuid."""
    slot = cred["service_uri"]
    return _ok(_publish(
        client, pub.headers, _skill_tgz(name, [(slot, "api_token")]),
        {**_for_users(*(user_grant(g.id, "use") for g in grantees)),
         "publisher_credentials": _named((slot, cred["id"]))},
    ))["package_id"]


def _unshare(client, owner_headers, cid: str, share_id: str) -> None:
    r = client.delete(f"{API}/credentials/{cid}/shares/{share_id}", headers=owner_headers)
    assert r.status_code == 200, r.text


def _share_of(client, owner_headers, cid: str, user_id: str) -> dict:
    rows = [s for s in _shares_of(client, owner_headers, cid)
            if s["shared_with_user_id"] == user_id]
    assert len(rows) == 1, rows
    return rows[0]


def _recipients(client, owner_headers, cid: str) -> set[str]:
    return {s["shared_with_user_id"] for s in _shares_of(client, owner_headers, cid)}


def _record_local_install(client, headers, pkg: str, revision: int, host: str,
                          device: str = "laptop") -> None:
    r = client.post(
        f"{API}/catalog/items/{pkg}/local-installs",
        headers=headers,
        json={"device_id": device, "revision_number": revision, "host_manifest_id": host},
    )
    assert r.status_code == 200, r.text


def _impact(client, owner_headers, cid: str) -> dict:
    r = client.get(f"{API}/credentials/{cid}/deletion-impact", headers=owner_headers)
    assert r.status_code == 200, r.text
    return r.json()


def _package(client, headers, pkg: str) -> dict:
    return _ok(client.get(f"{API}/skills/packages/{pkg}", headers=headers))


# ── Scenario 1: T4.8 (ii) ──────────────────────────────────────────────────


def test_owner_unshare_is_not_undone_by_provision_local_until_the_owner_shares_again(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publisher's skill provides a shareable credential; the grantee's
       provision-local shares it (``skill_local_install``), the Desktop lists it
    2. Owner removes the grantee's share → share list empty, off the
       grantee's Desktop and web list
    3. provision-local again (Finish setup reopened, twice) →
       ``not_shareable``, no id, no share row
    4. Another grantee is unaffected → ``shared``
    5. A stranger cannot remove the other grantee's share
    6. Owner shares with the grantee again → provision-local
       ``already_shared``; one row (direct); back on the Desktop
    7. share → unshare → share → unshare: the second unshare succeeds and
       provision-local is refused again
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    other = make_person(client, su)
    stranger = make_person(client, su)
    cred = _shareable(client, pub, "svc-t48")
    pkg = _publish_providing(client, pub, cred, "t48-tool", grantee, other)
    gd, _ = desktop_session(client, grantee.headers)

    # ── Phase 1: provision-local shares it ───────────────────────────
    assert _outcomes(_provision(client, grantee.headers, pkg)) == {
        "svc-t48": ("shared", cred["id"])
    }
    share = _share_of(client, pub.headers, cred["id"], grantee.id)
    assert share["source"] == "skill_local_install"
    assert cred["id"] in _listed(client, gd)

    # ── Phase 2: the owner unshares ──────────────────────────────────
    _unshare(client, pub.headers, cred["id"], share["id"])
    assert _shares_of(client, pub.headers, cred["id"]) == []
    assert cred["id"] not in _listed(client, gd)
    assert cred["id"] not in _shared_with_me(client, grantee.headers)

    # ── Phase 3: provision-local no longer re-shares ─────────────────
    for _ in range(2):
        assert _outcomes(_provision(client, grantee.headers, pkg)) == {
            "svc-t48": ("not_shareable", None)
        }
        assert _shares_of(client, pub.headers, cred["id"]) == []
    assert cred["id"] not in _listed(client, gd)
    assert cred["id"] not in _shared_with_me(client, grantee.headers)

    # ── Phase 4: the revocation is per person ────────────────────────
    assert _outcomes(_provision(client, other.headers, pkg)) == {
        "svc-t48": ("shared", cred["id"])
    }
    assert _recipients(client, pub.headers, cred["id"]) == {other.id}

    # ── Phase 5: only the owner removes shares ───────────────────────
    other_share = _share_of(client, pub.headers, cred["id"], other.id)
    r = client.delete(
        f"{API}/credentials/{cred['id']}/shares/{other_share['id']}",
        headers=stranger.headers,
    )
    # The route answers 400 "Not enough permissions" (pre-existing contract).
    assert r.status_code in (400, 403, 404), r.text
    assert _recipients(client, pub.headers, cred["id"]) == {other.id}

    # ── Phase 6: the owner shares again ──────────────────────────────
    share_credential_via_api(client, pub.headers, cred["id"], grantee.email)
    assert _outcomes(_provision(client, grantee.headers, pkg)) == {
        "svc-t48": ("already_shared", cred["id"])
    }
    again = _share_of(client, pub.headers, cred["id"], grantee.id)
    assert again["source"] in (None, "direct")
    assert _recipients(client, pub.headers, cred["id"]) == {grantee.id, other.id}
    assert cred["id"] in _listed(client, gd)

    # ── Phase 7: unsharing a second time is fine ─────────────────────
    _unshare(client, pub.headers, cred["id"], again["id"])
    assert _recipients(client, pub.headers, cred["id"]) == {other.id}
    assert _outcomes(_provision(client, grantee.headers, pkg)) == {
        "svc-t48": ("not_shareable", None)
    }
    assert _recipients(client, pub.headers, cred["id"]) == {other.id}


# ── Scenario 2: sharing off and on ─────────────────────────────────────────


def test_sharing_off_then_on_reshares_everyone_except_people_the_owner_unshared(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A and B provision-local (shared); D holds a direct share
       (already_shared)
    2. Owner unshares A (skill_local_install) and D (direct) → both
       not_shareable; B still already_shared
    3. Sharing off → A, B, D and a newcomer C all not_shareable; no shares
    4. Sharing on → B and C shared again; A and D stay not_shareable
       (off/on clears no individual revocation)
    5. Owner shares with A directly → A already_shared; D still refused
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    a, b, c, d = (make_person(client, su) for _ in range(4))
    cred = _shareable(client, pub, "svc-toggle")
    cid = cred["id"]
    pkg = _publish_providing(client, pub, cred, "toggle-tool", a, b, c, d)

    def outcome(person) -> tuple[str, str | None]:
        return _outcomes(_provision(client, person.headers, pkg))["svc-toggle"]

    # ── Phase 1 ───────────────────────────────────────────────────────
    assert outcome(a) == ("shared", cid)
    assert outcome(b) == ("shared", cid)
    share_credential_via_api(client, pub.headers, cid, d.email)
    assert outcome(d) == ("already_shared", cid)
    assert _recipients(client, pub.headers, cid) == {a.id, b.id, d.id}

    # ── Phase 2: the owner unshares A and D ──────────────────────────
    _unshare(client, pub.headers, cid, _share_of(client, pub.headers, cid, a.id)["id"])
    _unshare(client, pub.headers, cid, _share_of(client, pub.headers, cid, d.id)["id"])
    assert outcome(a) == ("not_shareable", None)
    assert outcome(d) == ("not_shareable", None)
    assert outcome(b) == ("already_shared", cid)
    assert _recipients(client, pub.headers, cid) == {b.id}

    # ── Phase 3: sharing off ─────────────────────────────────────────
    set_credential_sharing(client, pub.headers, cid, False)
    for person in (a, b, c, d):
        assert outcome(person) == ("not_shareable", None)
    assert _shares_of(client, pub.headers, cid) == []

    # ── Phase 4: sharing on ──────────────────────────────────────────
    set_credential_sharing(client, pub.headers, cid, True)
    assert outcome(b) == ("shared", cid)
    assert outcome(c) == ("shared", cid)
    assert outcome(a) == ("not_shareable", None)
    assert outcome(d) == ("not_shareable", None)
    assert _recipients(client, pub.headers, cid) == {b.id, c.id}

    # ── Phase 5: the owner shares with A again ───────────────────────
    share_credential_via_api(client, pub.headers, cid, a.email)
    assert outcome(a) == ("already_shared", cid)
    assert outcome(d) == ("not_shareable", None)
    assert _recipients(client, pub.headers, cid) == {a.id, b.id, c.id}


# ── Scenario 3: a later version stops providing it ────────────────────────


def test_later_version_without_the_credential_keeps_the_share_and_lists_desktop_installs(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. G provision-locals revision 1 (shared) and records Desktop installs
       on three hosts: a desktop agent G published through core, one the
       publisher published, and an unknown local agent
    2. The owner's share list shows all three under G's share
       (``local_skill_usages``, no ``host_manifest_id``); the host name only
       for G's own published agent, never for someone else's; no
       ``agent_usages`` and ``direct_agent_use_count`` 0
    3. The publisher republishes without providing the credential (rev 2:
       ``user``) → rev 2 is ``user_provides``, rev 1 still ``already_shared``;
       G keeps the share and the Desktop still lists it
    4. G moves one install to rev 2 → still listed (any revision counts);
       deletion impact stays tier 2
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    g = make_developer(client, su)
    cred = _shareable(client, pub, "svc-later")
    cid = cred["id"]
    archive_name = f"later-tool-{uuid.uuid4().hex[:6]}"
    pkg = _publish_providing(client, pub, cred, archive_name, g)
    package = _package(client, pub.headers, pkg)
    host = manifest_id_of(
        create_desktop_agent(client, g.headers, display_name="G Laptop Agent")
    )
    foreign_host = manifest_id_of(
        create_desktop_agent(client, pub.headers, display_name="Publisher Agent")
    )
    unknown_host = str(uuid.uuid4())
    gd, _ = desktop_session(client, g.headers)

    # ── Phase 1 ───────────────────────────────────────────────────────
    assert _outcomes(_provision(client, g.headers, pkg)) == {"svc-later": ("shared", cid)}
    _record_local_install(client, g.headers, pkg, 1, host)
    _record_local_install(client, g.headers, pkg, 1, foreign_host)
    _record_local_install(client, g.headers, pkg, 1, unknown_host)

    def usage(revision: int, host_name: str | None) -> dict:
        return {
            "package_uuid": pkg,
            "package_id": package["package_id"],
            "display_name": package["display_name"],
            "revision_number": revision,
            "host_agent_name": host_name,
        }

    def usages(rows: list[dict]) -> list[dict]:
        return sorted(rows, key=lambda u: (u["revision_number"], u["host_agent_name"] or ""))

    # ── Phase 2: the share list shows what the share is used for ─────
    share = _share_of(client, pub.headers, cid, g.id)
    assert share["agent_usages"] == []
    assert share["direct_agent_use_count"] == 0
    assert usages(share["local_skill_usages"]) == usages(
        [usage(1, "G Laptop Agent"), usage(1, None), usage(1, None)]
    )

    # ── Phase 3: rev 2 stops providing the credential ────────────────
    rev2 = _ok(_publish(
        client, pub.headers, _skill_tgz(archive_name, [("svc-later", "api_token")], body="Two."),
        {"package_uuid": pkg},
    ))
    assert rev2["revision_number"] == 2
    spec = rev2["required_credentials"][0]
    assert (spec["provided_by"], spec["publisher_credential_id"]) == ("user", None)
    assert _outcomes(_provision(client, g.headers, pkg, 2)) == {
        "svc-later": ("user_provides", None)
    }
    assert _outcomes(_provision(client, g.headers, pkg, 1)) == {
        "svc-later": ("already_shared", cid)
    }
    assert _share_of(client, pub.headers, cid, g.id)["source"] == "skill_local_install"
    assert cid in _listed(client, gd)

    # ── Phase 4: an install on rev 2 still counts ────────────────────
    _record_local_install(client, g.headers, pkg, 2, host)
    share = _share_of(client, pub.headers, cid, g.id)
    assert usages(share["local_skill_usages"]) == usages(
        [usage(2, "G Laptop Agent"), usage(1, None), usage(1, None)]
    )
    impact = _impact(client, pub.headers, cid)
    assert impact["tier"] == 2
    assert impact["active_local_skill_install_count"] == 1
    assert [u["package_uuid"] for u in impact["local_install_skill_usages"]] == [pkg]


# ── Scenario 4: the recipient's cloud agents ───────────────────────────────


def test_share_list_names_the_recipients_agents_and_the_owners_skills_on_them(
    client: TestClient, superuser_token_headers: dict[str, str], patch_environment_adapter
) -> None:
    """
    1. The publisher's cloud skill provides the credential; B installs it on
       agent A (``skill_install`` share) → ``agent_usages`` names A and the
       skill; ``direct_agent_use_count`` 0
    2. B links the same credential by hand to agents B and C → still only A
       in ``agent_usages``; ``direct_agent_use_count`` 2; no local usages
    3. The owner unshares B → all three agents lose the credential; the
       list is empty
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    b = make_developer(client, su)
    suffix = uuid.uuid4().hex[:6]
    pub_agent, pub_env = make_agent_with_env(client, pub.headers, f"Usage-Pub-{suffix}")
    cred = _cred(client, pub.headers, "svc-usage", allow_sharing=True, local=True)
    cid = cred["id"]
    link_credential_to_agent(client, pub.headers, pub_agent, cid)
    write_skill_with_credentials(pub_env, "usage-skill", [{"slot": "svc-usage", "type": "api_token"}])
    cloud = publish_skill(client, pub.headers, pub_agent, "usage-skill", visibility="public")
    display = _package(client, pub.headers, cloud["package_id"])["display_name"]
    alpha, _ = make_agent_with_env(client, b.headers, f"Usage-A-{suffix}")
    beta, _ = make_agent_with_env(client, b.headers, f"Usage-B-{suffix}")
    gamma, _ = make_agent_with_env(client, b.headers, f"Usage-C-{suffix}")
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    patch_environment_adapter.get_adapter = lambda env: adapter

    # ── Phase 1 ───────────────────────────────────────────────────────
    result = install_skill(client, b.headers, alpha, cloud["package_id"])
    assert result["credential_provisioning"][0]["outcome"] == "linked_publisher"
    skill_agent = {"id": alpha, "name": f"Usage-A-{suffix}", "skill_display_names": [display]}
    share = _share_of(client, pub.headers, cid, b.id)
    assert share["source"] == "skill_install"
    assert share["agent_usages"] == [skill_agent]
    assert share["direct_agent_use_count"] == 0

    # ── Phase 2: two agents use it with none of the owner's skills ───
    link_credential_to_agent(client, b.headers, beta, cid)
    link_credential_to_agent(client, b.headers, gamma, cid)
    share = _share_of(client, pub.headers, cid, b.id)
    assert share["agent_usages"] == [skill_agent]
    assert share["direct_agent_use_count"] == 2
    assert share["local_skill_usages"] == []

    # ── Phase 3: unshare ─────────────────────────────────────────────
    _unshare(client, pub.headers, cid, share["id"])
    assert _shares_of(client, pub.headers, cid) == []
    for agent_id in (alpha, beta, gamma):
        linked = {c["id"] for c in get_agent_credentials(client, b.headers, agent_id)["data"]}
        assert cid not in linked


# ── Scenario 5: deletion impact ────────────────────────────────────────────


def test_deletion_impact_blocks_while_someone_uses_the_skill_on_their_desktop(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. G provision-locals (one ``skill_local_install`` share), no install
       recorded yet → tier 1, local_install_share_count 1, active 0
    2. G records a Desktop install → tier 2, active 1, the package listed;
       an unforced DELETE is 409 carrying the same counts; it survives
    3. H holds a direct share and installs locally → active 2 (any share
       source), local_install_share_count still 1
    4. The publisher's own local install is not counted
    5. Sharing off → counts back to 0, tier 0; an unforced DELETE succeeds
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    g = make_person(client, su)
    h = make_person(client, su)
    cred = _shareable(client, pub, "svc-impact")
    cid = cred["id"]
    pkg = _publish_providing(client, pub, cred, "impact-tool", g, h)

    # ── Phase 1: shared, not installed ───────────────────────────────
    assert _outcomes(_provision(client, g.headers, pkg)) == {"svc-impact": ("shared", cid)}
    impact = _impact(client, pub.headers, cid)
    assert impact["tier"] == 1
    assert impact["local_install_share_count"] == 1
    assert impact["active_local_skill_install_count"] == 0
    assert impact["local_install_skill_usages"] == []

    # ── Phase 2: installed on G's Desktop ────────────────────────────
    _record_local_install(client, g.headers, pkg, 1, str(uuid.uuid4()))
    impact = _impact(client, pub.headers, cid)
    assert impact["tier"] == 2
    assert impact["local_install_share_count"] == 1
    assert impact["active_local_skill_install_count"] == 1
    [usage] = impact["local_install_skill_usages"]
    assert usage["package_uuid"] == pkg
    assert usage["revision_numbers"] == [1]
    r = client.delete(f"{API}/credentials/{cid}", headers=pub.headers)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["tier"] == 2
    assert detail["active_local_skill_install_count"] == 1
    assert client.get(f"{API}/credentials/{cid}", headers=pub.headers).status_code == 200

    # ── Phase 3: a direct-share holder installs too ──────────────────
    share_credential_via_api(client, pub.headers, cid, h.email)
    _record_local_install(client, h.headers, pkg, 1, str(uuid.uuid4()))
    impact = _impact(client, pub.headers, cid)
    assert impact["active_local_skill_install_count"] == 2
    assert impact["local_install_share_count"] == 1

    # ── Phase 4: the publisher's own install does not count ──────────
    _record_local_install(client, pub.headers, pkg, 1, str(uuid.uuid4()))
    assert _impact(client, pub.headers, cid)["active_local_skill_install_count"] == 2

    # ── Phase 5: sharing off ─────────────────────────────────────────
    set_credential_sharing(client, pub.headers, cid, False)
    impact = _impact(client, pub.headers, cid)
    assert impact["local_install_share_count"] == 0
    assert impact["active_local_skill_install_count"] == 0
    assert impact["local_install_skill_usages"] == []
    assert impact["tier"] == 0
    r = client.delete(f"{API}/credentials/{cid}", headers=pub.headers)
    assert r.status_code == 200, r.text
